#!/usr/bin/env python3
"""Real-ziv end-to-end probe for the web-search adapter (联调).

Drives the actual ziv binary on a pty — the technique of
agent/scripts/pty_suite.py — with an isolated HOME and project directory, so
the user's ~/.config/ziv is never touched. By default the adapter is pointed
at fake_scraper.py through a temp config: deterministic, no network, no
throttled search engine. --live adds one real search through the real tool.

Scenarios:
  A bypass mode   autostart bridges the adapter; /run returns the tool's
                  output; /quit leaves no adapter process behind
  B default mode  the read-only tool still asks; 'a' is one-time only
  C plan mode     the read-only tool is denied outright (fail-closed default)
  D plan + trust  mcp_trust_readonly_hint lets it through without a prompt
  E timeout       a call that overruns its deadline answers with an error and
                  ziv stays responsive; the scraper's process group is gone
  F crash         ziv killed with SIGKILL: the adapter leaves by itself
  G injection     escape sequences scraped from a page title/link never reach
                  the terminal (ziv prints tool output verbatim)

The ziv binary is driven in a temp project with an isolated HOME, so the
user's own sessions, memory and settings files are never touched.

usage: e2e_ziv.py [--ziv BIN] [--launch PATH] [--live]
exit code: 0 all green, 1 any failure.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLKIT = HERE.parent
sys.path.insert(0, str(TOOLKIT / "adapters" / "web-search"))
import server as websearch  # noqa: E402  (tool name + the real config for --live)

TOOL = f"mcp__websearch__{websearch.TOOL_NAME}"
CONNECTED = "websearch connected: 1 tools available as mcp__websearch__*"
CALL = '{"query":"zig e2e probe","num":3}'
OK_TEXT = "3 results for 'zig e2e probe'"
ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[A-Za-z]|[78=>])")  # CSI sequences plus DECSC/DECRC and keyboard-mode resets

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class Ziv:
    """ziv on a pty, with an isolated HOME (user config) and project dir."""

    def __init__(self, binary: str, launch: str, settings: dict, env: dict[str, str], adapter_home: str):
        self.adapter_home = adapter_home
        self.home = tempfile.mkdtemp(prefix="ziv-e2e-home-")
        self.project = tempfile.mkdtemp(prefix="ziv-e2e-proj-")
        os.makedirs(os.path.join(self.home, ".config", "ziv"))
        os.makedirs(os.path.join(self.project, ".ziv"))
        with open(os.path.join(self.home, ".config", "ziv", "mcp.json"), "w") as handle:
            json.dump({"mcpServers": {"websearch": {"command": launch}}}, handle)
        with open(os.path.join(self.project, ".ziv", "settings.json"), "w") as handle:
            json.dump(settings, handle)
        self.buf = b""
        self.pos = 0
        self.started = time.time()
        self.reaped = False
        self.pid, self.master = pty.fork()
        if self.pid == 0:
            os.chdir(self.project)
            child_env = {k: v for k, v in os.environ.items()
                         if not k.startswith(("OPENAI_", "ANTHROPIC_", "OLLAMA_", "BRAVE_", "ZIV_"))}
            child_env.update({"HOME": self.home, "TERM": "xterm-256color"})
            child_env.update(env)
            os.execve(binary, [binary], child_env)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 200, 0, 0))

    def expect(self, pattern: str, timeout: float = 25.0) -> float:
        """Wait for `pattern` after everything already consumed. Returns the
        seconds since ziv was spawned."""
        pat = pattern.encode()
        deadline = time.time() + timeout
        while True:
            index = self.buf.find(pat, self.pos)
            if index != -1:
                self.pos = index + len(pat)
                return time.time() - self.started
            if time.time() > deadline:
                tail = self.buf[max(self.pos - 400, 0):].decode("utf-8", "replace")
                raise TimeoutError(f"waiting for {pattern!r}; new output:\n{tail}")
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if self.master in ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    tail = self.buf[max(self.pos - 400, 0):].decode("utf-8", "replace")
                    raise EOFError(f"ziv exited while waiting for {pattern!r}; new output:\n{tail}")
                self.buf += chunk

    def transcript_since(self, mark: int) -> str:
        return ANSI.sub("", self.buf[mark:].decode("utf-8", "replace"))

    def send(self, data: str) -> None:
        os.write(self.master, data.replace("\n", "\r").encode())

    def run_tool(self, arguments: str) -> int:
        """`/run <tool> <json>`; returns a transcript mark for the output."""
        mark = len(self.buf)
        self.send(f"/run {TOOL} {arguments}\n")
        return mark

    def quit(self) -> None:
        self.send("/quit\n")
        self.expect("Goodbye", 15)

    def adapter_pid(self) -> int | None:
        pid_file = os.path.join(self.adapter_home, "websearch.pid")
        if not os.path.exists(pid_file):
            return None
        with open(pid_file) as handle:
            return int(handle.read())

    def kill_and_wait(self, timeout: float) -> bool:
        """SIGKILL ziv and wait — bounded — for it to be reaped, draining the
        pty meanwhile.

        The draining is not optional. ziv's exit restores the terminal with
        tcsetattr(TCSAFLUSH) and then closes the tty, and both wait for the
        tty's output queue to drain — which only happens if someone reads the
        master. A harness that stops reading there deadlocks ziv in the kernel
        (measured 2026-11-25: 8/8 without a reader, 0/8 with one), and a
        SIGKILL landing in that wait leaves it in the exiting state for good.
        A real terminal always reads, so only pty harnesses ever see this."""
        if self.reaped:
            return True
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._try_reap():
                return True
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    os.read(self.master, 65536)
                except OSError:
                    pass
        return self._try_reap()

    def _try_reap(self) -> bool:
        try:
            reaped, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            reaped = self.pid  # already reaped by an earlier quit()/kill
        self.reaped = reaped == self.pid
        return self.reaped

    def close(self) -> None:
        try:
            if not self.kill_and_wait(5):
                # Closing the master releases whatever the child is stuck on
                # in tty teardown; give it a moment, then let it go.
                os.close(self.master)
                self.master = -1
                deadline = time.time() + 3
                while time.time() < deadline and not self._try_reap():
                    time.sleep(0.05)
        finally:
            if self.master >= 0:
                os.close(self.master)
            shutil.rmtree(self.home, ignore_errors=True)
            shutil.rmtree(self.project, ignore_errors=True)


class Context:
    def __init__(self, binary: str, launch: str, env: dict[str, str], adapter_home: str, record: str):
        self.binary, self.launch, self.env = binary, launch, env
        self.adapter_home, self.record = adapter_home, record
        self.adapters: list[int] = []  # every adapter this probe started

    def scraper_child_pid(self) -> int | None:
        """Pid of the `sleep 60` grandchild fake_scraper.py spawns for 'hang'."""
        if not os.path.exists(self.record + ".child"):
            return None
        with open(self.record + ".child") as handle:
            return int(handle.read())


def alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int | None, seconds: float) -> bool:
    deadline = time.time() + seconds
    while alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    return not alive(pid)


def launch_ziv(ctx: Context, label: str, settings: dict, extra_env: dict[str, str] | None = None) -> Ziv:
    ziv = Ziv(ctx.binary, ctx.launch, settings, {**ctx.env, **(extra_env or {})}, ctx.adapter_home)
    connected = ziv.expect(CONNECTED)
    ziv.expect("Type your message")
    check(f"[{label}] autostart bridges the adapter inside ziv's 3s handshake budget", connected < 5.0,
          f"'connected' at +{connected:.2f}s after spawn")
    if (pid := ziv.adapter_pid()) is not None:
        ctx.adapters.append(pid)
    return ziv


def scenario_call(ctx: Context, label: str, settings: dict, arguments: str, expect: str,
                  extra_env: dict[str, str] | None = None, show: bool = False) -> None:
    """Start ziv, check the bridge, run one call, quit cleanly."""
    ziv = None
    try:
        ziv = launch_ziv(ctx, label, settings, extra_env)
        ziv.send("/mcp list\n")
        ziv.expect("websearch [connected]")
        check(f"[{label}] /mcp list reports the server connected", True)
        mark = ziv.run_tool(arguments)
        ziv.expect(expect)
        if show:
            text = ziv.transcript_since(mark)
            start = text.find("⏺")  # skip the TUI's echo of the typed command
            print("\n".join("  | " + line for line in text[start:].splitlines() if line.strip()))
        check(f"[{label}] /run returns the tool's output", True)
        pid = ziv.adapter_pid()
        ziv.quit()
        check(f"[{label}] /quit leaves no adapter process behind", gone_within(pid, 3))
    except (TimeoutError, EOFError) as exc:
        check(f"[{label}] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def scenario_default_ask_gate(ctx: Context) -> None:
    ziv = None
    try:
        ziv = launch_ziv(ctx, "default", {"permission_mode": "default"})
        ziv.run_tool(CALL)
        ziv.expect("Tool mcp__websearch__web_search: permission required. Allow? [y/N/a]")
        check("[default] the read-only tool still asks when mcp_trust_readonly_hint is off", True)
        ziv.send("a\n")
        ziv.expect("[no reusable pattern in this input — allowed once only]")
        ziv.expect(OK_TEXT)
        check("[default] 'a' (always allow) is one-time only for MCP tools", True)
        ziv.run_tool(CALL)
        ziv.expect("permission required. Allow? [y/N/a]")
        ziv.send("y\n")
        ziv.expect(OK_TEXT)
        check("[default] the identical call asks again (confirms 'a' was one-time)", True)
        ziv.quit()
    except (TimeoutError, EOFError) as exc:
        check("[default] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def scenario_plan_denied(ctx: Context) -> None:
    ziv = None
    try:
        ziv = launch_ziv(ctx, "plan", {"permission_mode": "plan"})
        ziv.run_tool(CALL)
        ziv.expect("[Permission denied: write operations are blocked in plan mode")
        check("[plan] the read-only tool is denied outright — no prompt (ziv's fail-closed default)", True)
        ziv.quit()
    except (TimeoutError, EOFError) as exc:
        check("[plan] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def scenario_timeout(ctx: Context) -> None:
    ziv = None
    try:
        ziv = launch_ziv(ctx, "timeout", {"permission_mode": "bypass"}, {"WEBSEARCH_TIMEOUT_S": "1"})
        mark = ziv.run_tool('{"query":"hang"}')
        answered = ziv.expect("no answer from the search tool within 1s", 10)
        check("[timeout] a tool call that never returns still answers with an error", True,
             f"error text at +{answered:.1f}s after spawn")
        print("\n".join("  | " + line for line in ziv.transcript_since(mark).splitlines() if "no answer" in line))
        ziv.quit()
        check("[timeout] ziv keeps processing input afterwards (no freeze)", True)
        check("[timeout] the hung scraper's whole process group is gone", gone_within(ctx.scraper_child_pid(), 3))
    except (TimeoutError, EOFError) as exc:
        check("[timeout] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def scenario_terminal_injection(ctx: Context) -> None:
    ziv = None
    try:
        ziv = launch_ziv(ctx, "injection", {"permission_mode": "bypass"})
        mark = len(ziv.buf)
        ziv.run_tool('{"query":"escape","num":1}')
        ziv.expect("Evil title gnidats")
        check("[injection] the cleaned title reaches the terminal", True)
        # ziv writes tool output to the pty verbatim, so these are the bytes
        # a hostile page title would have put there.
        raw = bytes(ziv.buf[mark:])
        leaked = [needle.decode() for needle in (b"pwned", b"evil.example", b"]8;;", b"]0;", b"]52;") if needle in raw]
        check("[injection] no OSC sequence from the scraped page reaches the terminal", not leaked,
              "window title / disguised link / clipboard payloads all absent" if not leaked else f"leaked: {leaked}")
        ziv.quit()
    except (TimeoutError, EOFError) as exc:
        check("[injection] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def scenario_ziv_killed(ctx: Context) -> None:
    ziv = None
    try:
        ziv = launch_ziv(ctx, "crash", {"permission_mode": "bypass"})
        pid = ziv.adapter_pid()
        ctx.adapters.append(pid)
        # ziv dies without running its exit path (crash, kill -9).
        if ziv.kill_and_wait(8):
            check("[crash] SIGKILLed ziv leaves no orphaned adapter", gone_within(pid, 6),
                  "the adapter left on its own" if not alive(pid) else f"adapter {pid} is still alive")
        else:
            # Not the adapter's doing: its parent has not actually gone away.
            check("[crash] ziv itself finishes exiting after SIGKILL", False,
                  f"ziv stayed in the exiting state for 8s (ps: {os.popen(f'ps -o stat= -p {ziv.pid}').read().strip()!r}) — orphan check inconclusive")
    except (TimeoutError, EOFError) as exc:
        check("[crash] scenario completed", False, str(exc)[-300:])
    finally:
        if ziv:
            ziv.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ziv", default="/usr/local/bin/ziv", help="ziv binary to drive (default: the installed one)")
    parser.add_argument("--launch", default=str(TOOLKIT / "adapters" / "web-search" / "launch"),
                        help="launcher that mcp.json points at (absolute path)")
    parser.add_argument("--live", action="store_true", help="one real search through the configured tool, no other scenario")
    args = parser.parse_args()

    adapter_home = tempfile.mkdtemp(prefix="ziv-e2e-adapter-")
    config_dir = tempfile.mkdtemp(prefix="ziv-e2e-config-")
    record = os.path.join(adapter_home, "argv.json")
    adapters: list[int] = []  # every adapter this probe starts, for the final cleanup
    try:
        if args.live:
            real = websearch.load_config()
            config = {"scraper": real.scraper, "python": real.python, "min_interval_s": 0, "timeout_s": 30}
        else:
            config = {"scraper": str(HERE / "fake_scraper.py"), "python": sys.executable, "min_interval_s": 0}
        config_path = os.path.join(config_dir, "config.json")
        with open(config_path, "w") as handle:
            json.dump(config, handle)
        env = {"WEBSEARCH_CONFIG": config_path, "ZIV_ADAPTER_HOME": adapter_home, "FAKE_SCRAPER_RECORD": record}
        ctx = Context(os.path.abspath(args.ziv), os.path.abspath(args.launch), env, adapter_home, record)
        ctx.adapters = adapters

        print(f"ziv:       {ctx.binary}\nlauncher:  {ctx.launch}\nscraper:   {config['scraper']}\n")
        if args.live:
            print("== live: one real search through ziv ==")
            scenario_call(ctx, "live", {"permission_mode": "bypass"},
                          '{"query":"zig programming language","engine":"bing","num":2}', "[websearch]", show=True)
        else:
            print("== A. bypass mode ==")
            scenario_call(ctx, "bypass", {"permission_mode": "bypass"}, CALL, OK_TEXT)
            print("\n== B. default mode ==")
            scenario_default_ask_gate(ctx)
            print("\n== C. plan mode, trust flag off ==")
            scenario_plan_denied(ctx)
            print("\n== D. plan mode + mcp_trust_readonly_hint ==")
            scenario_call(ctx, "plan+trust", {"permission_mode": "plan", "mcp_trust_readonly_hint": True}, CALL, OK_TEXT)
            print("\n== E. tool call overruns its deadline ==")
            scenario_timeout(ctx)
            print("\n== F. ziv killed with SIGKILL while connected ==")
            scenario_ziv_killed(ctx)
            print("\n== G. escape sequences scraped from a page ==")
            scenario_terminal_injection(ctx)
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        print(f"\n== e2e summary: {passed}/{len(RESULTS)} passed ==")
        return 0 if passed == len(RESULTS) else 1
    finally:
        # Whatever happened above, leave nothing behind.
        for pid in adapters:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(adapter_home, ignore_errors=True)
        shutil.rmtree(config_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
