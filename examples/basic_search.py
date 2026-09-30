"""快速开始:环境变量配置 key,get_rotator() 单例搜索。

运行前:
    export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3
    python examples/basic_search.py
"""

import os
import sys

from tavily_rotator import get_rotator


def main() -> None:
    if not os.environ.get("TAVILY_SEARCH_KEYS", "").strip():
        sys.exit("请先设置环境变量:export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3")

    rot = get_rotator()  # 进程内单例:CLI 与你的代码共用同一份用量账本

    data = rot.search("今天上海天气", max_results=5)
    for r in data.get("results", []):
        print(f"- {r.get('title', '')}")
        print(f"  {r.get('url', '')}")
        print(f"  {r.get('content', '')[:100]}\n")

    for key, u in rot.usage().items():  # 本地用量估计(尽力而为,非精确账单)
        print(f"[{key[:8]}…] 已用 {u['used']}/{u['limit']},剩余 {u['remaining']}")


if __name__ == "__main__":
    main()
