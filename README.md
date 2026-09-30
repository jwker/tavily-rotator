# tavily-rotator

<p align="center">
  <img src="https://img.shields.io/pypi/v/tavily-rotator?style=flat-square&cacheSeconds=3600" alt="PyPI version">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="License MIT">
  <img src="https://img.shields.io/pypi/dm/tavily-rotator?style=flat-square&cacheSeconds=3600" alt="PyPI downloads">
  <img src="https://img.shields.io/github/last-commit/jwker/tavily-rotator?style=flat-square&cacheSeconds=3600" alt="Last commit">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Powered%20by-Tavily-0E7C66?style=flat-square" alt="Powered by Tavily">
</p>

> 在多个 Tavily API key 之间按用量自动轮换搜索,优先使用剩余额度最多的 key。

一个轻量的 Tavily 搜索工具,解决"单个 key 配额有限、用量分散在多个 key 上"的场景。核心只需 `requests`。

## 特性

- **用量感知轮换**:根据每次请求的 `search_cost` 本地记账,优先选剩余额度最多的 key
- **自动容错**:某个 key 配额耗尽(403)自动切换并继续尝试其余 key;触发限流(429)明确报错
- **用量校准**:以 Tavily `/usage` 为权威数据源,事件驱动校准——首搜前并行探测(可关)、余额逼近阈值时确认真值、403 后 24 小时门控懒探测恢复
- **全耗尽抢跑**:所有 key 都用完时,立即探测最早耗尽的 key
- **无状态文件**:不持久化任何数据,没有跨进程并发写问题;多设备共用 key 也无需担心账本漂移
- **线程安全**:内部用锁保护状态,可安全并发调用
- **CLI 命令行**(接入任意 agent 的示例见 [接入 LangChain](#接入-langchain);更多可运行脚本见 [examples/](examples/))

## 安装

```bash
pip install tavily-rotator
# 或
uv add tavily-rotator
```

## 快速开始

设置环境变量,逗号分隔多个 key:

```bash
export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3
```

使用:

```python
from tavily_rotator import get_rotator

rot = get_rotator()
data = rot.search("今天上海天气", max_results=5)
print(data["results"])
```

`get_rotator()` 返回进程内共享的同一个实例:同一份用量账本、线程安全, CLI 内部用的也是它。不想设环境变量时,也可直接传 key 列表(仅首次调用生效):

```python
from tavily_rotator import get_rotator

rot = get_rotator(keys=["tvly-key1", "tvly-key2", "tvly-key3"])
```

## 进阶:独立实例

可以直接构造 `TavilyRotator`, 例如测试隔离、多组互不干扰的 key 池或自定义配额:

```python
from tavily_rotator import TavilyRotator

rot = TavilyRotator(
    keys=["tvly-key1", "tvly-key2", "tvly-key3"],
    limit=100,  # 默认 1000;也可传 {"tvly-key1": 1000, "tvly-key2": 5000} 按 key 单独指定
    startup_probe=True,  # 首搜前调 /usage 校准各 key 用量;一次性脚本可传 False
)
data = rot.search("今天上海天气")
print(rot.usage())  # {"tvly-key1": {"used": 1, "limit": 100, "remaining": 99, ...}}
```

注意:每个实例各自持锁、互不共享记账,普通使用请勿重复创建。

## 在 LangChain 中使用示例

```python
from langchain.tools import tool
from tavily_rotator import get_rotator

@tool
def tavily_search(query: str, max_results: int = 5) -> str:
    """搜索最新信息、时事、事实核查等。"""
    return get_rotator().search(query=query, max_results=max_results).get("results", "")
```

放入 Deep Agent:

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model="openai:gpt-4o",
    tools=[tavily_search],
    system_prompt="你是一个乐于助人的助手。",
)
result = agent.invoke({"messages": [{"role": "user", "content": "今天上海天气怎么样?"}]})
print(result["messages"][-1].content)
```


## CLI

```bash
# 临时使用,不装进任何项目
uvx tavily-rotator "今天上海天气"

# 或安装后直接调用
tavily-search "今天上海天气" --max-results 5
tavily-search "今天上海天气" --json
```

## 配置

**单例 `get_rotator()` —— key 来源:**

| 项 | 说明 |
|---|---|
| `TAVILY_SEARCH_KEYS` | 环境变量,逗号分隔的多个 key,`get_rotator()` 的默认来源 |
| `get_rotator(keys=...)` | 也可显式传入 key 列表(仅首次调用生效) |

**独立实例 `TavilyRotator` —— 构造参数:**

| 项 | 说明 |
|---|---|
| `keys` | key 列表(必填) |
| `limit` | 配额上限,默认 1000;传 `{key: limit}` dict 可按 key 单独指定 |
| `startup_probe` | 首搜前并行调 `/usage` 校准所有 key(默认开)。常驻进程建议开;一次性脚本/CLI 建议关,靠 403 自愈 |

## 如何工作

1. 每次搜索,按"剩余额度最多 + 最近未使用"的原则挑选 key
2. 响应里的 `search_cost` 累加到**内存**计数(仅用于排序,尽力而为)
3. 用量校准按需发生,以 Tavily `/usage` 为准:
   - **首搜前**并行探测所有 key(`startup_probe=False` 可关)
   - **余额 < 配额 10%** 时确认真值(60 秒节流,失败静默降级)
   - 返回 403 说明该 key 配额耗尽 → 标记并继续尝试其余 key,直到成功或试穿候选池
4. 耗尽的 key 每 24 小时懒探测一次,配额周期刷新后自动恢复;全部耗尽时立即抢跑探测

进程退出后内存计数即消失,下次进程的用量认知由启动校准或 403 自愈重建——本地不存任何文件。

查看实时用量(本地估计,非精确账单):

```python
rot.usage()
# {"tvly-key1": {"used": 340, "exhausted": False, "limit": 1000, "remaining": 660}}
```

## 示例

[examples/](examples/) 目录提供了可直接运行的脚本,涵盖快速开始、多线程并发、独立实例构造、本地用量查看:

| 文件 | 说明 |
|---|---|
| [basic_search.py](examples/basic_search.py) | 快速开始:`get_rotator()` 单例搜索 |
| [concurrent_search.py](examples/concurrent_search.py) | 线程安全:多线程并发搜索,记账不串账 |
| [custom_instance.py](examples/custom_instance.py) | 进阶:自定义状态文件与按 key 配额 |
| [check_usage.py](examples/check_usage.py) | 查看本地用量账本(不发网络请求) |

```bash
python examples/basic_search.py   # 运行前先 export TAVILY_SEARCH_KEYS=...
```

## 注意

- 请确保你有权使用所配置的 API key,并遵守对应服务商的[服务条款](https://docs.tavily.com)与用量政策。
- 本地计数只统计本程序的使用量,且仅用于选 key 的排序;精确用量以 Tavily 官方为准(`/usage` 校准会自动对齐)。
- 多设备/多程序共用同一 key 时,本地计数彼此不可见,可能出现多耗一两次 403 的情况——这是设计内的行为,不损失配额。
- 各 key 默认约 1 请求/秒,内置 1.1 秒冷却以避免触发限流。

### 从 0.1.x 升级

- `TavilyRotator` 不再接受 `data_file` 参数,`DEFAULT_DATA_FILE` 已移除:库不再读写任何状态文件
- 旧的 `~/.tavily_rotator/tavily_usage.json` 可以直接删除,不再使用

## 许可证

[MIT](LICENSE)
