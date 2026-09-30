"""进阶:直接构造 TavilyRotator,适合测试隔离、多组互不干扰的 key 池。

普通使用请用 get_rotator()(进程内单例);多个独立实例各自持锁、
互不共享记账。本示例全程不发网络请求;填入真实 key 并取消
搜索注释后才会联网。

运行:
    python examples/custom_instance.py
"""

from tavily_rotator import TavilyRotator

PLACEHOLDER = "tvly-把这里换成你的key"


def main() -> None:
    keys = [PLACEHOLDER + "-1", PLACEHOLDER + "-2"]
    if all(k.startswith(PLACEHOLDER) for k in keys):
        print("检测到示例占位 key —— 下面仅演示构造参数,不发起网络请求。\n")

    rot = TavilyRotator(
        keys=keys,
        # 配额上限:传 int 统一指定;传 {key: limit} 按 key 单独指定(未列出的走默认 1000)
        limit={PLACEHOLDER + "-1": 1000, PLACEHOLDER + "-2": 5000},
        # 首搜前是否调 /usage 校准所有 key 的用量(默认开)。
        # 常驻进程建议开;一次性脚本可关,靠 403 自愈
        startup_probe=True,
    )
    print("实例已创建。配额:key1=1000, key2=5000")
    print("初始用量视图:", rot.usage())

    # 发起搜索(填入真实 key 后取消注释):
    # data = rot.search("今天上海天气", max_results=5)
    # print(data["results"])
    # print(rot.usage())  # 实时查看各 key 的本地用量估计


if __name__ == "__main__":
    main()
