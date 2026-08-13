"""命令行入口:`tavily-search "关键词"`。"""

import argparse
import json

from .rotator import get_rotator


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tavily-search",
        description="Tavily 搜索(多 key 自动轮换)",
    )
    parser.add_argument("query", help="搜索关键词")
    parser.add_argument("--max-results", type=int, default=5, help="返回结果条数(默认 5)")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args(argv)

    data = get_rotator().search(args.query, max_results=args.max_results)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    results = data.get("results", [])
    if not results:
        print("无结果")
        return
    for r in results:
        print(f"- {r.get('title', '')}: {r.get('url', '')}")
        snippet = r.get("content", "")
        if snippet:
            print(f"  {snippet[:120]}")


if __name__ == "__main__":
    main()
