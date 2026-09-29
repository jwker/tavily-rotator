"""线程安全演示:多线程并发搜索,内部锁保证选 key 与记账不串账。

注意:每个 key 内置 1.1 秒冷却(防限流),并发请求会自然摊到不同 key 上;
key 较少时,实际吞吐仍受单 key 约 1 QPS 限制。

运行前:
    export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3
    python examples/concurrent_search.py
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from tavily_rotator import get_rotator

QUERIES = [
    "Python GIL 是什么",
    "Tavily API 文档",
    "RAG 检索增强生成",
    "LangChain 工具调用",
    "向量数据库对比",
    "搜索引擎评测基准",
]


def main() -> None:
    if not os.environ.get("TAVILY_SEARCH_KEYS", "").strip():
        sys.exit("请先设置环境变量:export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3")

    rot = get_rotator()

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda q: rot.search(q, max_results=3), QUERIES))
    elapsed = time.monotonic() - start

    ok = sum(1 for d in results if d.get("results"))
    print(f"{len(QUERIES)} 个查询完成:成功 {ok},耗时 {elapsed:.1f}s\n")
    for q, d in zip(QUERIES, results):
        first = (d.get("results") or [{}])[0]
        print(f"  {q} -> {first.get('title', '(无结果)')}")


if __name__ == "__main__":
    main()
