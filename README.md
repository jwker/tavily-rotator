# tavily-rotator

<p align="center">
  <img src="https://img.shields.io/pypi/v/tavily-rotator?style=flat-square" alt="PyPI version">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square" alt="License MIT">
  <img src="https://img.shields.io/pypi/dm/tavily-rotator?style=flat-square" alt="PyPI downloads">
  <img src="https://img.shields.io/github/last-commit/jwker/tavily-rotator?style=flat-square" alt="Last commit">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Powered%20by-Tavily-0E7C66?style=flat-square" alt="Powered by Tavily">
</p>

> 在多个 Tavily API key 之间按用量自动轮换搜索,优先使用剩余额度最多的 key。

一个轻量的 Tavily 搜索工具,解决"单个 key 配额有限、用量分散在多个 key 上"的场景。核心只需 `requests`。

## 特性

- **用量感知轮换**:根据每次请求的 `search_cost` 本地记账,优先选剩余额度最多的 key
- **自动容错**:某个 key 配额耗尽(403)自动切换;触发限流(429)明确报错
- **懒探测**:24 小时门控下用 `/usage` 检查耗尽的 key 是否已进入新配额周期,自动重新启用
- **全耗尽抢跑**:所有 key 都用完时,立即探测最早耗尽的 key
- **状态持久化**:记录存在 `~/.tavily_rotator/tavily_usage.json`,重启不丢
- **线程安全**:内部用锁保护状态,可安全并发调用
- **CLI 命令行**(接入任意 agent 的示例见 [接入 LangChain](#接入-langchain))

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

可以直接构造 `TavilyRotator`, 例如测试隔离、多组互不干扰的 key 池、自定义状态文件或配额:

```python
from tavily_rotator import TavilyRotator

rot = TavilyRotator(
    keys=["tvly-key1", "tvly-key2", "tvly-key3"],
    data_file="/tmp/test_usage.json",  # 默认 ~/.tavily_rotator/tavily_usage.json
    limit=100,  # 默认 1000;也可传 {"tvly-key1": 1000, "tvly-key2": 5000} 按 key 单独指定
)
data = rot.search("今天上海天气")
```

注意:每个实例各自持锁、构造时重新读盘,多个实例间不共享记账,普通使用请勿重复创建。

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
| `data_file` | 状态文件路径,默认 `~/.tavily_rotator/tavily_usage.json` |
| `limit` | 配额上限,默认 1000;传 `{key: limit}` dict 可按 key 单独指定 |

## 如何工作

1. 每次搜索,按"剩余额度最多 + 最近未使用"的原则挑选 key
2. 响应里的 `search_cost` 累加到本地计数
3. 返回 403 说明该 key 配额耗尽 → 标记并切换下一个
4. 每 24 小时懒探测一次耗尽的 key,配额周期刷新后自动恢复

状态文件示例(`~/.tavily_rotator/tavily_usage.json`):

```json
{
  "keys": {
    "tvly-key1": {"used": 340, "exhausted": false, "last_probe_at": 1697000000.0, "last_used_at": 1697000100.0}
  }
}
```

删除该文件即可重置计数。

## 注意

- 请确保你有权使用所配置的 API key,并遵守对应服务商的[服务条款](https://docs.tavily.com)与用量政策。
- 本地计数只统计本程序的使用量;若同一 key 被其他程序使用,以 Tavily 官方用量为准(`/usage` 探测会校准)。
- 各 key 默认约 1 请求/秒,内置 1.1 秒冷却以避免触发限流。
- 状态文件默认全项目共享(配额属于 key 而非项目);需要项目级隔离时,用 `TavilyRotator(data_file=...)` 各存各的。

## 许可证

[MIT](LICENSE)
