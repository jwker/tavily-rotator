# 示例

可独立运行的脚本,只依赖本包(`pip install tavily-rotator`),无额外第三方依赖。

| 文件 | 需要联网 | 说明 |
|---|---|---|
| [basic_search.py](basic_search.py) | 是 | 快速开始:环境变量配 key,`get_rotator()` 单例搜索 |
| [concurrent_search.py](concurrent_search.py) | 是 | 线程安全演示:多线程并发搜索,记账不串账 |
| [custom_instance.py](custom_instance.py) | 否* | 进阶:直接构造 `TavilyRotator`,自定义状态文件与按 key 配额 |
| [check_usage.py](check_usage.py) | 否 | 查看本地用量账本(状态文件),不发网络请求 |

`*` 填入真实 key 并取消搜索注释后才会联网。

## 准备

到 [app.tavily.com](https://app.tavily.com) 注册几个账号即可拿到免费 key(每个账号一个 key),然后:

```bash
export TAVILY_SEARCH_KEYS=tvly-key1,tvly-key2,tvly-key3
```

## 运行

```bash
# 用 pip 安装本包后
python examples/basic_search.py

# 或在本仓库开发环境里
uv run python examples/basic_search.py
```

## 错误处理

所有失败路径(网络错误、429 限流、全部 key 耗尽)都抛 `RuntimeError`,统一捕获即可:

```python
from tavily_rotator import get_rotator

try:
    data = get_rotator().search("今天上海天气")
except RuntimeError as e:
    # 403 耗尽的 key 已被自动跳过并重试,走到这里说明:
    # 网络失败 / 触发 429 限流 / 所有 key 均耗尽
    print(f"搜索失败: {e}")
```
