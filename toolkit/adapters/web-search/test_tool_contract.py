#!/usr/bin/env python3
"""Contract test: the command line this adapter builds is what the REAL search
tool accepts, and maps to the calls it makes.

fake_scraper.py takes any `--name=value`, so the unit tests cannot notice a
renamed option; only the tool's own argparse can. The tool's search functions
are replaced, so nothing touches the network, and bytecode caching is off, so
nothing is written into the tool's directory.

Run: python3 -m unittest toolkit/adapters/web-search/test_tool_contract.py -v
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server as srv  # noqa: E402

try:
    CFG = srv.load_config(HERE / "config.json")
except srv.ConfigError:
    CFG = None
TOOL_DIR = Path(CFG.scraper).parent if CFG and Path(CFG.scraper).is_file() else None
CANNED = [{"title": "t", "link": "https://example.com/", "snippet": "s"}]


@unittest.skipUnless(TOOL_DIR, "search_scraper.py from config.json is not on this machine")
class CliContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True  # leave no __pycache__ in the tool's directory
        sys.path.insert(0, str(TOOL_DIR))  # the tool's vendored packages sit next to it
        try:
            cls.tool = importlib.import_module("search_scraper")
        except ImportError as exc:
            raise unittest.SkipTest(f"the tool's dependencies are not importable here: {exc}")
        finally:
            sys.dont_write_bytecode = previous

    def run_tool_main(self, argv: list[str]) -> tuple[str, mock.Mock, mock.Mock]:
        """The tool's real main() on `argv`, with its search functions replaced."""
        out = io.StringIO()
        with mock.patch.object(self.tool, "search", return_value=CANNED) as search, \
                mock.patch.object(self.tool, "search_all", return_value={"bing": CANNED}) as search_all, \
                mock.patch.object(sys, "argv", ["search_scraper.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.tool.main()
        return out.getvalue(), search, search_all

    def adapter_argv(self, proxy: str | None = "http://127.0.0.1:8234", **overrides: object) -> list[str]:
        cfg = dataclasses.replace(CFG, proxy=proxy)
        args = dict(query="zig comptime", engine="bing", num=3, time_range="week", lang="en", region="US")
        args.update(overrides)
        return srv.build_command(cfg, args["query"], args["engine"], args["num"],  # type: ignore[arg-type]
                                 time_range=args["time_range"], lang=args["lang"], region=args["region"])[2:]

    def test_every_option_reaches_the_tool_as_intended(self):
        out, search, search_all = self.run_tool_main(self.adapter_argv())
        search.assert_called_once_with("zig comptime", "bing", 3, proxy="http://127.0.0.1:8234", timeout=15, delay=1.5,
                                        time_range="week", lang="en", region="US")
        self.assertFalse(search_all.called)
        self.assertEqual(CANNED, json.loads(out), "--json puts exactly the result list on stdout")

    def test_engine_all_uses_search_all_with_the_tool_defaults(self):
        _, _, search_all = self.run_tool_main(self.adapter_argv(proxy=None, engine="all", num=7, time_range=None, lang=None, region=None))
        search_all.assert_called_once_with("zig comptime", 7, proxy=None, timeout=15, delay=1.5,
                                           time_range=None, lang="zh-CN", region=None)

    def test_a_query_starting_with_a_dash_stays_one_value(self):
        _, search, _ = self.run_tool_main(self.adapter_argv(query="-n forced"))
        self.assertEqual("-n forced", search.call_args.args[0])

    def test_custom_date_range_is_passed_through(self):
        _, search, _ = self.run_tool_main(self.adapter_argv(time_range="2026-01-01..2026-11-24"))
        self.assertEqual("2026-01-01..2026-11-24", search.call_args.kwargs["time_range"])

    def test_an_unknown_option_makes_the_tool_exit_2(self):
        # The exit code the adapter reports as "search_scraper exited with code 2".
        with self.assertRaises(SystemExit) as caught:
            self.run_tool_main(["--query=x", "--enginex=bing", "--num=3", "--json"])
        self.assertEqual(2, caught.exception.code)

    def test_the_tool_accepts_option_prefixes(self):
        # Worth knowing when reading a recorded argv: argparse's default
        # allow_abbrev takes "--eng=bing" as "--engine". The adapter always
        # writes full option names; this pins that the tool is the lenient one.
        _, search, _ = self.run_tool_main(["--query=x", "--eng=duckduckgo", "--num=3", "--json"])
        self.assertEqual("duckduckgo", search.call_args.args[1])

    def test_the_adapters_vocabulary_matches_the_tool(self):
        self.assertEqual({"google", "bing", "duckduckgo"}, set(self.tool.ENGINES))
        self.assertTrue(set(CFG.engines) <= set(self.tool.ENGINES), CFG.engines)
        self.assertEqual({"day", "week", "month", "year"}, set(self.tool.TIME_FILTERS))
        self.assertEqual(self.tool._DATE_RANGE_RE.pattern, srv._DATE_RANGE.pattern)

    def test_the_tool_reads_lang_and_region_with_a_dash(self):
        # Why the adapter rejects "zh_CN": these helpers split on "-" alone.
        self.assertEqual("zh-CN", self.tool._bing_mkt(None, "zh-CN"))
        self.assertEqual("en-US", self.tool._bing_mkt("US", "en"))
        self.assertEqual("us-en", self.tool._ddg_kl("US", "en-US"))


if __name__ == "__main__":
    unittest.main()
