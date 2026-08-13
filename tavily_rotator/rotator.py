"""Tavily 多 key 轮换搜索核心。

设计要点:
- 每次搜索响应的 `search_cost` 本地累加,不依赖额外查询
- 403(配额耗尽)→ 标记该 key 本轮不参与,24h 门控下用 /usage 懒探测是否重置
- 全部耗尽 → 立刻探测"最早耗尽且到期"的 key
- 状态持久化到用户目录 ~/.tavily_rotator/tavily_usage.json(重启不丢,不污染包目录)
- 线程安全:pick/记账在锁内,HTTP 搜索在锁外
"""

import json
import os
import threading
import time
from pathlib import Path

import requests

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"
DEFAULT_LIMIT = 1000          # 各 key 默认配额
PROBE_INTERVAL = 24 * 3600    # 探测间隔:24 小时
RATE_COOLDOWN = 1.1           # 每个 key 最短使用间隔(避免触发限流)

# 状态文件放用户目录,不依赖项目/包目录
DEFAULT_DATA_FILE = str(Path.home() / ".tavily_rotator" / "tavily_usage.json")


class TavilyRotator:
    """在多个 Tavily API key 之间轮换搜索,优先用剩余额度最多的活跃 key。"""

    def __init__(
        self,
        keys: list[str],
        data_file: str = DEFAULT_DATA_FILE,
        limit: int | dict[str, int] = DEFAULT_LIMIT,
    ):
        """keys: Tavily key 列表;limit: 配额上限,传 int 统一指定,或传 {key: limit} 按 key 单独指定。"""
        self._keys = [k for k in keys if k]
        if not self._keys:
            raise ValueError("未配置任何 Tavily key")
        self._data_file = Path(data_file)
        if isinstance(limit, dict):
            self._limit = DEFAULT_LIMIT  # 未单独指定的 key 的兜底配额
            self._limits = dict(limit)   # key -> 独立配额
        else:
            self._limit = limit
            self._limits = {}
        self._lock = threading.Lock()
        self._state = self._load()

    # ---------------- 状态持久化 ----------------

    def _default_state(self) -> dict:
        return {"keys": {k: {"used": 0, "exhausted": False, "last_probe_at": 0.0, "last_used_at": 0.0} for k in self._keys}}

    def _load(self) -> dict:
        try:
            data = json.loads(self._data_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return self._default_state()
        keys = data.setdefault("keys", {})
        for k in self._keys:
            keys.setdefault(k, {"used": 0, "exhausted": False, "last_probe_at": 0.0, "last_used_at": 0.0})
        return data

    def _save(self) -> None:
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._data_file.with_name(self._data_file.name + ".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._data_file)  # 原子替换,多进程并发写不会读到半个文件

    def _limit_for(self, key: str) -> int:
        """单个 key 的配额上限(未单独指定时用兜底值)。"""
        return self._limits.get(key, self._limit)

    # ---------------- 选 key(锁内调用) ----------------

    def _pick_key(self) -> str:
        now = time.time()
        state = self._state["keys"]
        active = [k for k in self._keys if not state[k]["exhausted"] and state[k]["used"] < self._limit_for(k)]

        if active:
            # 优先"最近 RATE_COOLDOWN 秒内没用过"的,再按剩余额度最多
            fresh = [k for k in active if now - state[k]["last_used_at"] >= RATE_COOLDOWN]
            pool = fresh or active
            key = max(pool, key=lambda k: self._limit_for(k) - state[k]["used"])
        else:
            # 全部耗尽 → 抢跑探测最早耗尽且到期的 key
            key = self._probe_oldest_exhausted()
            if key is None:
                raise RuntimeError("所有 Tavily key 均已耗尽,且暂无到期可探测的重置")

        state[key]["last_used_at"] = now
        self._save()
        return key

    # ---------------- 探测(锁内调用) ----------------

    def _probe(self, key: str) -> bool:
        """调 /usage 探测单个 key。返回 True=配额已重置可用,False=仍耗尽。"""
        self._state["keys"][key]["last_probe_at"] = time.time()
        try:
            resp = requests.get(TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10)
            data = resp.json()
            usage = data.get("key", {}).get("usage", 0)
            limit = data.get("key", {}).get("limit") or self._limit_for(key)
            if usage < limit:
                # 新周期到了,校准本地计数并重新启用
                self._state["keys"][key]["used"] = usage
                self._state["keys"][key]["exhausted"] = False
                return True
        except Exception:  # noqa: BLE001 - 探测失败就当仍耗尽,下个门控窗口再试
            pass
        self._state["keys"][key]["exhausted"] = True
        return False

    def _probe_oldest_exhausted(self) -> str | None:
        """全部耗尽时:按最近探测时间升序,探测"到期"的 key,重置了就返回它。"""
        now = time.time()
        state = self._state["keys"]
        candidates = [
            k for k in self._keys
            if state[k]["exhausted"] and now - state[k]["last_probe_at"] >= PROBE_INTERVAL
        ]
        candidates.sort(key=lambda k: state[k]["last_probe_at"])
        for k in candidates:
            if self._probe(k):
                return k
        return None

    def _lazy_probe_due(self) -> None:
        """时间门控懒探测:每次 search 顺手探"一个"到期的耗尽 key(最多一个)。"""
        now = time.time()
        state = self._state["keys"]
        for k in self._keys:
            if state[k]["exhausted"] and now - state[k]["last_probe_at"] >= PROBE_INTERVAL:
                self._probe(k)
                return

    # ---------------- 对外:搜索 ----------------

    def search(self, query: str, **kwargs) -> dict:
        with self._lock:
            self._lazy_probe_due()
            key = self._pick_key()

        data = self._do_search(key, query, **kwargs)
        if data is not None:
            return data

        # 403:配额耗尽 → 标记,换下一个重试一次
        with self._lock:
            self._state["keys"][key]["exhausted"] = True
            self._state["keys"][key]["last_probe_at"] = time.time()
            self._save()
            try:
                nxt = self._pick_key()
            except RuntimeError:
                nxt = None
        if nxt and nxt != key:
            data = self._do_search(nxt, query, **kwargs)
            if data is not None:
                return data
        raise RuntimeError("所有 Tavily key 配额均已耗尽")

    def _do_search(self, key: str, query: str, **kwargs) -> dict | None:
        """执行一次搜索,成功返回数据,403 返回 None,其它异常抛出。"""
        # api_key 必须由轮换逻辑决定,防止 kwargs 覆盖破坏记账
        kwargs.pop("api_key", None)
        try:
            resp = requests.post(
                TAVILY_SEARCH_URL,
                json={"api_key": key, "query": query, **kwargs},
                timeout=30,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Tavily 请求失败: {e}") from e

        if resp.status_code == 200:
            data = resp.json()
            cost = data.get("search_cost", 1)
            with self._lock:
                self._state["keys"][key]["used"] += cost
                self._save()
            return data

        if resp.status_code == 403:
            return None  # 调用方负责标记耗尽

        if resp.status_code == 429:
            raise RuntimeError("Tavily 触发限流(429),请稍后重试")

        raise RuntimeError(f"Tavily 请求异常: HTTP {resp.status_code} {resp.text[:200]}")


# 模块级单例,供工具/CLI 复用
_rotator: TavilyRotator | None = None


def get_rotator(keys: list[str] | None = None) -> TavilyRotator:
    """获取单例。默认从环境变量 TAVILY_SEARCH_KEYS(逗号分隔)读取 key;也可显式传入。"""
    global _rotator
    if _rotator is None:
        if keys is None:
            keys = [k.strip() for k in os.environ.get("TAVILY_SEARCH_KEYS", "").split(",") if k.strip()]
        _rotator = TavilyRotator(keys)
    return _rotator
