"""进阶:直接构造 TavilyRotator,适合测试隔离、多组互不干扰的 key 池。

普通使用请用 get_rotator()(进程内单例);多个独立实例各自持锁、
各自读盘,不共享记账。填入真实 key 并取消搜索注释后才会联网。

运行:
    python examples/custom_instance.py
"""

import tempfile
from pathlib import Path

from tavily_rotator import TavilyRotator

PLACEHOLDER = "tvly-把这里换成你的key"


def main() -> None:
    keys = [PLACEHOLDER + "-1", PLACEHOLDER + "-2"]
    if all(k.startswith(PLACEHOLDER) for k in keys):
        print("检测到示例占位 key —— 下面仅演示构造参数,不发起网络请求。\n")

    rot = TavilyRotator(
        keys=keys,
        # 状态文件:默认 ~/.tavily_rotator/tavily_usage.json(配额属于 key 而非项目,
        # 建议保持默认全机共享);这里用临时文件做项目级隔离
        data_file=str(Path(tempfile.gettempdir()) / "tavily_rotator_example_usage.json"),
        # 配额上限:传 int 统一指定;传 {key: limit} 按 key 单独指定(未列出的走默认 1000)
        limit={PLACEHOLDER + "-1": 1000, PLACEHOLDER + "-2": 5000},
    )

    print("实例已创建,状态文件:", rot._data_file)  # noqa: SLF001 - 示例仅为展示
    print("配额:key1=1000, key2=5000")

    # 发起搜索(填入真实 key 后取消注释):
    # data = rot.search("今天上海天气", max_results=5)
    # print(data["results"])


if __name__ == "__main__":
    main()
