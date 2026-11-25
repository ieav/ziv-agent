#!/usr/bin/env python3
"""Zero-dependency MCP stdio runtime for ziv tool adapters (stdlib only).

ziv speaks a small subset of MCP over stdio (newline-delimited JSON-RPC 2.0):
initialize, notifications/initialized, tools/list, tools/call. This module
implements exactly that subset and encodes the adapter contract so an adapter
author cannot forget it (see toolkit/README.md):

  R1  the handshake needs nothing but the stdlib — import the wrapped tool
      lazily, inside a handler (ziv's autostart handshake budget is 3 s)
  R2  every tools/call answers, even when the handler times out or raises
      (ziv has no per-call timeout: a silent call freezes the REPL)
  R3  tool output is text and capped; anything larger spills to a file and the
      reply carries the path (ziv shows the model at most 16 KiB)
  R4  logs go to a rotating file — stdout is the protocol channel and ziv
      discards the server's stderr
  R5  the process leaves on stdin EOF or when its parent goes away, running
      the exit hooks first (on_exit)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
#: ziv keeps at most 16 KiB of a tool result for the model (agent/src/tools/
#: registry.zig, HISTORY_OUTPUT_CAP). Stay below it so ziv never cuts a reply.
OUTPUT_CAP = 12 * 1024
DEFAULT_CALL_TIMEOUT_S = 60.0
_POLL_PARENT_S = 2.0

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}

_EXIT_HOOKS: list[Callable[[], None]] = []


class AdapterError(Exception):
    """A handler failure meant for the model: reported with isError=true."""


class RpcError(Exception):
    """A JSON-RPC level failure (malformed params, unknown method or tool)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    #: Last line of defence for R2. Handlers should enforce their own deadline
    #: (a subprocess kill is the only reliable one) — a Python thread cannot be
    #: stopped, so a handler that overruns this keeps a thread alive until the
    #: process exits.
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S
    #: annotations.readOnlyHint. ziv only honours it when the user sets
    #: mcp_trust_readonly_hint; declare it truthfully anyway.
    read_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only},
        }


def on_exit(hook: Callable[[], None]) -> None:
    """Run `hook` wherever this process leaves on its own: stdin EOF, parent
    gone, stdout closed, SIGTERM. Adapters use it to stop subprocesses a
    still-running handler is waiting on.

    Nothing runs after a SIGKILL — and that is how ziv takes its servers down
    on a normal exit — so such subprocesses must also be short-lived by design.
    Hooks may run more than once; make them idempotent."""
    _EXIT_HOOKS.append(hook)


def run_exit_hooks() -> None:
    for hook in _EXIT_HOOKS:
        try:
            hook()
        except Exception:  # noqa: BLE001 — one failing cleanup must not stop the rest
            logging.getLogger("ziv_adapter").exception("exit hook failed")


def adapter_home() -> Path:
    """Where logs, the pid file and spilled output live. ZIV_ADAPTER_HOME
    overrides it (the e2e probe points it at a temp dir)."""
    override = os.environ.get("ZIV_ADAPTER_HOME")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "ziv" / "adapters"


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Return a human-readable problem, or None when `args` satisfy `schema`.

    Checks `required`, basic `type`s and `enum`. Anything deeper (ranges,
    formats) is the handler's job — it knows the field semantics.
    """
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required field '{key}'"
    for key, value in args.items():
        prop = props.get(key)
        if not isinstance(prop, dict):
            continue  # undeclared field: passed through, the handler decides
        expected = prop.get("type")
        if isinstance(expected, str) and expected in _TYPE_CHECKS:
            ok = isinstance(value, _TYPE_CHECKS[expected])
            if expected in ("integer", "number") and isinstance(value, bool):
                ok = False  # bool is an int subclass; the model never means it
            if not ok:
                return f"'{key}' must be of type {expected}"
        enum = prop.get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"'{key}' must be one of: {', '.join(map(str, enum))}"
    return None


def cap_output(text: str, spill_path: Path | None) -> str:
    """R3: keep a reply under OUTPUT_CAP bytes. Oversized text is written whole
    to `spill_path` and the reply keeps its head plus the path. If it cannot be
    written, the head alone goes back — a full disk must not cost the reply."""
    size = len(text.encode("utf-8"))
    if size <= OUTPUT_CAP:
        return text
    head = text.encode("utf-8")[: OUTPUT_CAP - 600].decode("utf-8", errors="ignore")
    shown = len(head.encode("utf-8"))
    if spill_path is not None:
        try:
            spill_path.parent.mkdir(parents=True, exist_ok=True)
            spill_path.write_text(text, encoding="utf-8")
        except OSError:
            spill_path = None
    if spill_path is None:
        return f"{head}\n[输出已截断：显示前 {shown}/{size} 字节；完整内容未能落盘]"
    return (
        f"{head}\n[输出已截断：显示前 {shown}/{size} 字节。"
        f"完整内容已存：{spill_path} — 用 FileRead 的 offset/limit 分页读取]"
    )


def _logger(name: str, home: Path) -> logging.Logger:
    log = logging.getLogger(f"ziv_adapter.{name}")
    log.setLevel(logging.DEBUG if os.environ.get("ZIV_ADAPTER_DEBUG") else logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler: logging.Handler
        try:
            home.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(home / f"{name}.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        except OSError as exc:
            # The log is an amenity; losing it must not keep the tool from serving.
            log.warning("cannot open the log under %s (%s); logging to stderr", home, exc)
            handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
    return log


class Server:
    """Serves `tools` to ziv on stdin/stdout until EOF or parent exit."""

    def __init__(self, name: str, version: str = "0.1.0", tools: tuple[Tool, ...] | list[Tool] = ()):
        self.name = name
        self.version = version
        self._tools: dict[str, Tool] = {tool.name: tool for tool in tools}
        self.home = adapter_home()
        self.log = _logger(name, self.home)

    # ── protocol ────────────────────────────────────────────────────

    def serve(self) -> None:
        # Locale-independent UTF-8 on the protocol channel: a C/POSIX locale
        # (GUI launch, cron) would otherwise make non-ASCII output raise.
        for stream in (sys.stdin, sys.stdout):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

        pid_file = self._prepare_home()
        stop = threading.Event()
        if not os.environ.get("ZIV_ADAPTER_NO_PARENT_WATCH"):
            threading.Thread(target=self._watch_parent, args=(os.getppid(), stop), daemon=True, name="parent-watch").start()
        try:  # only the main thread may install handlers; serve() is written for it
            signal.signal(signal.SIGTERM, lambda *_: self._leave("SIGTERM; exiting"))
        except ValueError:
            pass
        self.log.info("%s v%s serving (pid %d, parent %d, python %s)", self.name, self.version, os.getpid(), os.getppid(), sys.version.split()[0])
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                reply = self.handle_line(line)
                if reply is not None:
                    self._send(reply)
        finally:
            stop.set()
            run_exit_hooks()
            if pid_file is not None:
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass
            self.log.info("%s stopped", self.name)

    def handle_line(self, line: str) -> dict[str, Any] | None:
        """One request line in, one response line out (None for notifications)."""
        try:
            msg = json.loads(line)
        except ValueError:
            self.log.warning("dropping non-JSON line: %.200r", line)
            return None
        if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
            return None  # notification (notifications/initialized) or a response
        try:
            result = self._dispatch(msg["method"], msg.get("params"), msg["id"])
            return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except RpcError as exc:
            return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": exc.code, "message": exc.message}}

    def _dispatch(self, method: str, params: Any, request_id: Any) -> dict[str, Any]:
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            # The subset above is stable across protocol versions, so echo
            # whatever the client asks for (ziv sends 2025-06-18).
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.to_dict() for tool in self._tools.values()]}
        if method == "tools/call":
            return self._call(params, request_id)
        raise RpcError(-32601, f"method not found: {method}")

    def _call(self, params: dict[str, Any], request_id: Any) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RpcError(-32602, "tools/call: 'name' must be a string")
        tool = self._tools.get(name)
        if tool is None:
            raise RpcError(-32602, f"unknown tool: {name} (known: {', '.join(self._tools) or 'none'})")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise RpcError(-32602, f"tools/call: 'arguments' must be an object, got {type(args).__name__}")
        problem = validate_args(tool.input_schema, args)
        if problem:
            return _error(f"invalid arguments for {name}: {problem}")

        # R2: the handler runs off the protocol thread; the reply is sent at
        # the deadline even if the handler is still running.
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = tool.handler(args)
            except BaseException as exc:  # noqa: BLE001 — the contract is: never leave a call unanswered
                box["error"] = exc

        started = time.monotonic()
        worker = threading.Thread(target=run, daemon=True, name=f"call:{name}")
        worker.start()
        worker.join(tool.timeout_s)
        if worker.is_alive():
            self.log.error("%s overran its %.0fs deadline; replying with a timeout", name, tool.timeout_s)
            return _error(
                f"{name}: timed out after {tool.timeout_s:.0f}s — the tool did not answer. "
                "Narrow the request (fewer results, another engine) or raise timeout_s in the adapter config."
            )
        elapsed = time.monotonic() - started

        if "error" in box:
            error = box["error"]
            if isinstance(error, AdapterError):
                self.log.warning("%s failed in %.1fs: %s", name, elapsed, error)
                return _error(str(error))
            self.log.error("%s raised in %.1fs:\n%s", name, elapsed, _traceback(error))
            return _error(f"{name}: {type(error).__name__}: {error}")
        try:
            return self._reply(name, box.get("result"), elapsed, request_id)
        except Exception as exc:  # noqa: BLE001 — a broken reply must not take the server down
            self.log.error("%s: could not build the reply:\n%s", name, _traceback(exc))
            return _error(f"{name}: the adapter could not build the reply ({type(exc).__name__}); see the adapter log")

    def _reply(self, name: str, result: Any, elapsed: float, request_id: Any) -> dict[str, Any]:
        text = result
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        self.log.info("%s ok in %.1fs (%d bytes)", name, elapsed, len(text.encode("utf-8")))
        spill = self.home / "spill" / f"{self.name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}-{request_id}.txt"
        return {"content": [{"type": "text", "text": cap_output(text, spill)}], "isError": False}

    # ── plumbing ────────────────────────────────────────────────────

    def _prepare_home(self) -> Path | None:
        """Logs, the pid file and spilled output live under self.home. None of
        them is part of the protocol, so an unwritable home only costs them."""
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            pid_file = self.home / f"{self.name}.pid"
            pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
            return pid_file
        except OSError as exc:
            self.log.warning("cannot write under %s (%s): running without pid file and spill", self.home, exc)
            return None

    def _send(self, reply: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError, ValueError):
            # ziv is gone; nothing left to answer.
            self._leave("stdout closed; exiting")

    def _leave(self, reason: str) -> None:
        run_exit_hooks()
        self.log.info("%s: %s", self.name, reason)
        os._exit(0)

    def _watch_parent(self, original_ppid: int, stop: threading.Event) -> None:
        # R5: ziv's stdio spawn leaks both pipe ends into this process, so
        # stdin never reaches EOF when ziv dies without running its exit path
        # (crash, kill -9). A changed ppid means the parent is gone — and so
        # does a ppid of 1 at startup: ziv may die before this process is far
        # enough along to take the baseline (seen live: an adapter that
        # recorded "parent 1" and then waited forever for it to change).
        while not stop.wait(_POLL_PARENT_S):
            ppid = os.getppid()
            if ppid == 1 or ppid != original_ppid:
                self.log.warning("parent gone (ppid %d -> %d)", original_ppid, ppid)
                self._leave("parent gone; exiting")


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _traceback(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
