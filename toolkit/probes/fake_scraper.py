#!/usr/bin/env python3
"""Stand-in for search_scraper.py: same CLI contract, no network.

Used by the unit tests (adapters/web-search/test_server.py) and the real-ziv
e2e probe (probes/e2e_ziv.py) so both are deterministic and never hit — or
throttle — a real search engine. The query selects the behaviour:

  hang     blocks for a minute (after spawning a `sleep 60` grandchild)
  empty    prints [] (and the tool's misleading "got 0/N" warning)
  crash    exits 3 with "boom" on stderr
  garbage  prints something that is not JSON
  warn     normal hits plus a shortfall WARNING on stderr
  decoy    hits that share no word with the query (what engines serve to bots)
  escape   hits whose title/link/snippet carry terminal escape sequences
  crowded  engine=all only: decoys from bing, an empty engine, error entries
           and three WARNING lines — more notes than a reply may show
  <other>  `num` hits that mention the query

Every run appends its argv to $FAKE_SCRAPER_RECORD when that variable is set.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def main() -> int:
    opts = {arg.split("=", 1)[0]: arg.split("=", 1)[1] for arg in sys.argv[1:] if "=" in arg}
    query = opts.get("--query", "")
    engine = opts.get("--engine", "bing")
    num = int(opts.get("--num", "10"))

    record = os.environ.get("FAKE_SCRAPER_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")

    if query == "hang":
        child = subprocess.Popen(["sleep", "60"])
        if record:
            with open(record + ".child", "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        time.sleep(60)
        return 0
    if query == "crash":
        print("boom", file=sys.stderr)
        return 3
    if query == "garbage":
        print("not json at all")
        return 0

    def hits(label: str, base: int) -> list[dict]:
        return [
            {"title": f"{label} — hit {i}", "link": f"https://example.com/{base}/{i}", "snippet": f"Snippet {i} for {label}"}
            for i in range(1, num + 1)
        ]

    decoys = [{"title": f"Unrelated page {i}", "link": f"https://decoy.example/{i}", "snippet": "Nothing to do with the query"} for i in range(1, num + 1)]
    if query == "decoy":
        data: object = decoys
    elif query == "escape":
        # What a hostile page could put into a title, a link and a snippet.
        data = [{
            "title": "Evil\x1b]0;pwned\x07 title\x1b[0m \u202egnidats\x1b[2J",
            "link": "https://example.com/\x1b]8;;http://evil.example/\x1b\\escape",
            "snippet": "line1\nline2\x00",
        }]
    elif query == "empty":
        data = []
    else:
        data = hits(query, 1)

    if engine == "all":
        payload: object = {
            "duckduckgo": [] if query in ("empty", "crowded") else hits(query + " ddg", 2),
            "bing": decoys if query == "crowded" else data,
            "google": [{"error": f"boom {i}"} for i in (1, 2, 3)] if query == "crowded" else [],
        }
    else:
        payload = data

    if query == "warn":
        print("22:00:00 [WARNING] Bing 仅获取 3/10 条：翻页无新增结果（当前网络环境可能限制 Bing 翻页），可尝试 --proxy", file=sys.stderr)
    if query == "escape":
        print("22:00:00 [WARNING] \x1b[31mBing 仅获取 3/10 条\x1b[0m：翻页无新增结果", file=sys.stderr)
    if query == "crowded":
        for i in (1, 2, 3):
            print(f"22:00:0{i} [WARNING] Bing 仅获取 {i}/10 条：翻页无新增结果", file=sys.stderr)
    if query == "empty":
        print(f"22:00:00 [WARNING] {engine} 仅获取 0/{num} 条：html 端点在当前环境可能不提供翻页，建议改用 bing/google 或降低 -n", file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
