#!/usr/bin/env python3
"""Unit tests for ziv_adapter (stdlib only, no network).

Run: python3 -m unittest toolkit/lib/python/test_ziv_adapter.py -v
     (from the repo root; the file adds its own directory to sys.path)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ziv_adapter as za  # noqa: E402

#: A tiny server exercising every path of the runtime. Behaviour is selected
#: through the tool name so one process covers all cases.
SERVER = textwrap.dedent(
    """
    import os, sys, time
    import ziv_adapter as za

    def echo(args):
        return " ".join(f"{k}={v}" for k, v in sorted(args.items()))

    def fail(args):
        raise za.AdapterError("explained failure")

    def boom(args):
        raise ValueError("unhandled")

    def sleepy(args):
        time.sleep(3)
        return "too late"

    def spill(args):
        return "x" * 20000

    def weird(args):
        return {"not", "serializable"}  # json.dumps cannot encode a set

    def arm(args):
        za.on_exit(lambda: open(os.environ["ZIV_ADAPTER_HOME"] + "/hook-ran", "w").write("ran"))
        return "armed"

    tools = (
        za.Tool("echo", "Echo the arguments", {"type": "object", "properties": {
            "name": {"type": "string"}, "count": {"type": "integer"},
            "kind": {"type": "string", "enum": ["a", "b"]}}, "required": ["name"]}, echo, read_only=True),
        za.Tool("fail", "Explained failure", {"type": "object"}, fail),
        za.Tool("boom", "Unhandled failure", {"type": "object"}, boom),
        za.Tool("sleepy", "Overruns its deadline", {"type": "object"}, sleepy, timeout_s=0.3),
        za.Tool("spill", "Oversized output", {"type": "object"}, spill),
        za.Tool("weird", "A result that cannot become a reply", {"type": "object"}, weird),
        za.Tool("arm", "Arm the exit hook", {"type": "object"}, arm),
    )
    za.Server("probe", tools=tools).serve()
    """
)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    return not alive(pid)


class RuntimeProtocol(unittest.TestCase):
    """The server, driven over stdin/stdout exactly like ziv drives it."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="ziv-adapter-test-")
        self.addCleanup(self.home.cleanup)

    def env(self, *extra_env: str) -> dict[str, str]:
        env = dict(os.environ, ZIV_ADAPTER_HOME=self.home.name, ZIV_ADAPTER_NO_PARENT_WATCH="1")
        for item in extra_env:
            key, value = item.split("=", 1)
            env[key] = value
        return env

    def spawn(self, *extra_env: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(HERE), env=self.env(*extra_env),
        )

    def talk(self, requests, *extra_env: str) -> list[dict]:
        proc = subprocess.run(
            [sys.executable, "-c", SERVER],
            input="".join(json.dumps(r) + "\n" for r in requests),
            capture_output=True, text=True, timeout=30, cwd=str(HERE), env=self.env(*extra_env),
        )
        self.assertEqual(0, proc.returncode, f"stderr:\n{proc.stderr}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    @staticmethod
    def call(name, args=None, rid=10):
        return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}

    @staticmethod
    def text_of(reply):
        return reply["result"]["content"][0]["text"]

    def test_handshake_echoes_protocol_version_and_lists_tools(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ziv", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual([1, 2], [r["id"] for r in replies], "notifications must not be answered")
        self.assertEqual("2025-06-18", replies[0]["result"]["protocolVersion"])
        self.assertEqual("probe", replies[0]["result"]["serverInfo"]["name"])
        tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
        self.assertEqual({"echo", "fail", "boom", "sleepy", "spill", "weird", "arm"}, set(tools))
        self.assertTrue(tools["echo"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["fail"]["annotations"]["readOnlyHint"])
        self.assertEqual(["name"], tools["echo"]["inputSchema"]["required"])

    def test_call_returns_text_and_is_not_an_error(self):
        reply = self.talk([self.call("echo", {"name": "zig", "count": 3, "kind": "b"})])[0]
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual("count=3 kind=b name=zig", self.text_of(reply))

    def test_failures_answer_with_iserror(self):
        replies = self.talk([self.call("echo", {}, 1), self.call("echo", {"name": "x", "count": "3"}, 2),
                             self.call("echo", {"name": "x", "kind": "z"}, 3), self.call("echo", {"name": "x", "count": True}, 4),
                             self.call("fail", {}, 5), self.call("boom", {}, 6), self.call("nope", {}, 7),
                             {"jsonrpc": "2.0", "id": 8, "method": "resources/list"}])
        by_id = {r["id"]: r for r in replies}
        self.assertIn("missing required field 'name'", self.text_of(by_id[1]))
        self.assertIn("'count' must be of type integer", self.text_of(by_id[2]))
        self.assertIn("'kind' must be one of: a, b", self.text_of(by_id[3]))
        self.assertIn("'count' must be of type integer", self.text_of(by_id[4]), "bool is not an integer")
        for rid in (1, 2, 3, 4, 5, 6):
            self.assertTrue(by_id[rid]["result"]["isError"], f"id {rid}")
        self.assertIn("explained failure", self.text_of(by_id[5]))
        self.assertIn("ValueError: unhandled", self.text_of(by_id[6]))
        self.assertEqual(-32602, by_id[7]["error"]["code"], "unknown tool is a JSON-RPC error")
        self.assertEqual(-32601, by_id[8]["error"]["code"], "unknown method is a JSON-RPC error")

    def test_call_that_overruns_its_deadline_still_answers(self):
        started = time.monotonic()
        reply = self.talk([self.call("sleepy")])[0]
        elapsed = time.monotonic() - started
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("timed out after", self.text_of(reply))
        self.assertLess(elapsed, 2.5, "the reply must not wait for the handler (3s sleep)")

    def test_oversized_output_spills_to_a_file(self):
        reply = self.talk([self.call("spill")])[0]
        text = self.text_of(reply)
        self.assertFalse(reply["result"]["isError"])
        self.assertLessEqual(len(text.encode("utf-8")), za.OUTPUT_CAP)
        self.assertIn("完整内容已存", text)
        spilled = list((Path(self.home.name) / "spill").glob("probe-*.txt"))
        self.assertEqual(1, len(spilled))
        self.assertEqual("x" * 20000, spilled[0].read_text(encoding="utf-8"))

    def test_a_spill_that_cannot_be_written_still_returns_the_head(self):
        # A file where the spill directory should be: mkdir raises, the disk
        # could equally be full. The reply must not be lost over it.
        (Path(self.home.name) / "spill").write_text("in the way", encoding="utf-8")
        reply = self.talk([self.call("spill")])[0]
        text = self.text_of(reply)
        self.assertFalse(reply["result"]["isError"])
        self.assertLessEqual(len(text.encode("utf-8")), za.OUTPUT_CAP)
        self.assertIn("未能落盘", text)
        self.assertNotIn("完整内容已存", text)

    def test_a_reply_that_cannot_be_built_does_not_kill_the_server(self):
        replies = self.talk([self.call("weird", {}, 1), self.call("echo", {"name": "after"}, 2)])
        by_id = {r["id"]: r for r in replies}
        self.assertTrue(by_id[1]["result"]["isError"])
        self.assertIn("could not build the reply (TypeError)", self.text_of(by_id[1]))
        self.assertEqual("name=after", self.text_of(by_id[2]), "the next call is served as usual")

    def test_pid_file_is_removed_on_clean_exit(self):
        self.talk([self.call("echo", {"name": "x"})])
        self.assertFalse((Path(self.home.name) / "probe.pid").exists())

    def test_an_unwritable_home_does_not_stop_serving(self):
        # Logs, pid file and spill are amenities; none is part of the protocol.
        blocked = Path(self.home.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        replies = self.talk([self.call("echo", {"name": "x"})], f"ZIV_ADAPTER_HOME={blocked}")
        self.assertEqual("name=x", self.text_of(replies[0]))

    def test_exit_hooks_run_on_clean_eof(self):
        self.talk([self.call("arm")])
        self.assertTrue((Path(self.home.name) / "hook-ran").exists(), "the hook did not run at EOF")

    def test_exit_hooks_run_on_sigterm(self):
        proc = self.spawn()
        try:
            proc.stdin.write(json.dumps(self.call("arm")) + "\n")
            proc.stdin.flush()
            reply = json.loads(proc.stdout.readline())
            self.assertEqual("armed", self.text_of(reply))
            proc.terminate()
            self.assertEqual(0, proc.wait(timeout=10), proc.stderr.read())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                stream.close()
        self.assertTrue((Path(self.home.name) / "hook-ran").exists(), "the hook did not run on SIGTERM")

    def test_exits_by_itself_when_the_parent_dies(self):
        # ziv's stdio spawn leaks both pipe ends into the server, so stdin
        # never sees EOF when ziv dies without running its exit path. Reproduce
        # that shape exactly: the parent hands the server its stdin and,
        # through pass_fds, a copy of the pipe's WRITE end. Then the parent is
        # killed.
        home = Path(self.home.name)
        server_pid = None
        middle = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(
                f"""
                import os, subprocess, sys, time
                read_end, write_end = os.pipe()
                subprocess.Popen([sys.executable, "-c", {SERVER!r}], stdin=read_end, pass_fds=(write_end,))
                time.sleep(60)
                """)],
            cwd=str(HERE), env=dict(os.environ, ZIV_ADAPTER_HOME=str(home)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            pid_file = home / "probe.pid"
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "server never started")
            server_pid = int(pid_file.read_text())
            time.sleep(0.5)  # let the server block on its (never-ending) stdin
            self.assertIsNone(middle.poll())
            os.kill(middle.pid, 9)  # the parent dies without any exit path
            middle.wait()
            self.assertTrue(gone_within(server_pid, 6), f"server {server_pid} survived its parent")
        finally:
            middle.kill()
            middle.wait()
            if server_pid is not None and alive(server_pid):
                os.kill(server_pid, 9)

    def test_exits_when_orphaned_before_it_even_started(self):
        # The other ordering of the same event: the parent dies while the
        # server is still starting, so the server's baseline ppid is already 1
        # (reparented to launchd/init). Waiting for the ppid to change from 1
        # would wait forever.
        pid_file = Path(self.home.name) / "probe.pid"
        subprocess.run(
            [sys.executable, "-c", textwrap.dedent(
                f"""
                import os, subprocess, sys
                read_end, write_end = os.pipe()
                subprocess.Popen([sys.executable, "-c", {SERVER!r}], stdin=read_end, pass_fds=(write_end,))
                sys.exit(0)  # the parent leaves at once
                """)],
            cwd=str(HERE), env=dict(os.environ, ZIV_ADAPTER_HOME=self.home.name),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        server_pid = None
        try:
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "server never started")
            server_pid = int(pid_file.read_text())
            self.assertTrue(gone_within(server_pid, 6), f"orphaned server {server_pid} is still alive")
        finally:
            if server_pid is not None and alive(server_pid):
                os.kill(server_pid, 9)


if __name__ == "__main__":
    unittest.main()
