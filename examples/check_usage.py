"""查看本地用量账本(只读,不发网络请求)。

用法:
    python examples/check_usage.py                    # 默认 ~/.tavily_rotator/tavily_usage.json
    python examples/check_usage.py /path/to/state.json
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from tavily_rotator import DEFAULT_DATA_FILE


def mask(key: str) -> str:
    """key 是敏感信息,输出时只保留前 8 位。"""
    return key[:8] + "…" if len(key) > 8 else key


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts > 0 else "-"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_DATA_FILE)
    if not path.exists():
        sys.exit(f"状态文件不存在:{path}(还没有运行过搜索?)")

    keys = json.loads(path.read_text(encoding="utf-8")).get("keys", {})
    if not keys:
        sys.exit(f"状态文件里还没有 key 记录:{path}")

    print(f"状态文件:{path}\n")
    print(f"{'key':<14}{'已用':>6}  {'状态':<6}{'最近使用':<12}{'最近探测':<12}")
    for key, s in keys.items():
        status = "已耗尽" if s.get("exhausted") else "可用"
        print(
            f"{mask(key):<14}{s.get('used', 0):>6}  {status:<6}"
            f"{fmt_ts(s.get('last_used_at', 0.0)):<12}{fmt_ts(s.get('last_probe_at', 0.0)):<12}"
        )
    print("\n删除该文件即可重置全部计数。")


if __name__ == "__main__":
    main()
