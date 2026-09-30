"""Tavily 多 key 轮换搜索核心。

设计要点:
- 权威用量数据在 Tavily 侧(/usage);内存记账只用于"优先选剩余多的 key",尽力而为
- 不持久化:没有状态文件,不存在跨进程并发写问题;进程重启后的用量认知
  靠启动校准(可关)或 403 自愈重建
- /usage 校准全部事件驱动,不做周期校准:
  1) 首搜前并行探测所有 key(startup_probe=False 可关,CLI 等一次性场景跳过)
  2) 选中 key 的余额低于配额阈值时确认一次真值(带最小间隔节流)
  3) 403 标记时记录探测时间,进入 24h 门控
- 403(配额耗尽)→ 标记该 key,当次调用内继续换下一个,直到成功或试穿候选池
- 全部耗尽 → 抢跑探测"最早耗尽且到期"的 key
- 线程安全:选 key/记账/校准在锁内,HTTP 搜索在锁外
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"
DEFAULT_LIMIT = 1000           # 各 key 默认配额
PROBE_INTERVAL = 24 * 3600     # 耗尽 key 的重置探测间隔:24 小时
RATE_COOLDOWN = 1.1            # 每个 key 最短使用间隔(避免触发限流)
LOW_BALANCE_RATIO = 0.1        # 余额 < 配额 10% 时触发阈值校准
USAGE_PROBE_MIN_INTERVAL = 60  # 阈值校准最小间隔(秒),防止高频打 /usage


class TavilyRotator:
    """在多个 Tavily API key 之间轮换搜索,优先用剩余额度最多的活跃 key。"""

    def __init__(
        self,
        keys: list[str],
        limit: int | dict[str, int] = DEFAULT_LIMIT,
        startup_probe: bool = True,
    ):
        """keys: Tavily key 列表;limit: 配额上限,传 int 统一指定,或传 {key: limit} 按 key 单独指定。

        startup_probe: 首次搜索前并行调 /usage 校准所有 key 的本地计数(默认开)。
        常驻进程建议开启;一次性调用(如 CLI)建议关闭,靠 403 自愈即可。
        """
        self._keys = [k for k in keys if k]
        if not self._keys:
            raise ValueError("未配置任何 Tavily key")
        if isinstance(limit, dict):
            self._limit = DEFAULT_LIMIT  # 未单独指定的 key 的兜底配额
            self._limits = dict(limit)   # key -> 独立配额
        else:
            self._limit = limit
            self._limits = {}
        self._lock = threading.Lock()
        self._state = {
            k: {"used": 0, "exhausted": False, "last_probe_at": 0.0, "last_used_at": 0.0}
            for k in self._keys
        }
        self._probed = not startup_probe  # 关闭启动校准 = 视为已"校准",直接进搜索
        self._last_calibration: dict[str, float] = {}  # key -> 上次校准时刻(阈值节流)

    def _limit_for(self, key: str) -> int:
        """单个 key 的配额上限(未单独指定时用兜底值)。"""
        return self._limits.get(key, self._limit)

    # ---------------- /usage 校准 ----------------

    def _fetch_usage(self, key: str) -> tuple[int, int] | None:
        """调 /usage 取单个 key 的 (真实用量, 配额上限);任何失败返回 None。

        失败一律静默降级:校准只是优化,搜索主路径不能被它阻塞。
        """
        try:
            resp = requests.get(TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=10)
            info = resp.json().get("key", {})
            usage = info.get("usage", 0)
            limit = info.get("limit") or self._limit_for(key)
            return usage, limit
        except Exception:  # noqa: BLE001 - 见 docstring
            return None

    def _ensure_started(self) -> None:
        """懒启动校准(进程生命周期内只做一次):并行探测所有 key,校准内存计数。

        - 放在首搜前而非 __init__:构造 Rotator 不应触发网络 IO
        - 校准失败的 key 静默降级为 used=0,由阈值校准与 403 自愈兜底
        - 启动即已耗尽的 key 直接标记,省掉一次注定失败的搜索请求
        """
        with self._lock:
            if self._probed:
                return
            self._probed = True
            now = time.time()
            with ThreadPoolExecutor(max_workers=min(len(self._keys), 8)) as pool:
                futures = {pool.submit(self._fetch_usage, k): k for k in self._keys}
                for fut, key in futures.items():
                    got = fut.result()
                    self._last_calibration[key] = now  # 启动校准即一次校准,纳入节流
                    if got is not None:
                        s = self._state[key]
                        s["used"] = got[0]
                        if got[0] >= got[1]:
                            s["exhausted"] = True

    def _calibrate_if_stale(self, key: str) -> None:
        """阈值校准:选中 key 的余额低于配额 10% 时,拉 /usage 确认真值(带节流)。

        只在"逼近耗尽、误差敏感"的临界区按需购买精度;其余时刻本地计数
        仅用于排序,无需精确。失败静默跳过,由 403 自愈兜底。
        """
        limit = self._limit_for(key)
        if limit - self._state[key]["used"] > limit * LOW_BALANCE_RATIO:
            return
        now = time.time()
        if now - self._last_calibration.get(key, 0.0) < USAGE_PROBE_MIN_INTERVAL:
            return
        self._last_calibration[key] = now
        got = self._fetch_usage(key)
        if got is not None:
            usage, real_limit = got
            self._state[key]["used"] = usage
            if usage >= real_limit:
                self._state[key]["exhausted"] = True  # 校准发现已耗尽,当次就换 key

    # ---------------- 探测恢复(锁内调用) ----------------

    def _probe_reset(self, key: str) -> bool:
        """探测耗尽 key 是否已进入新配额周期。重置则校准计数并重新启用。"""
        s = self._state[key]
        s["last_probe_at"] = time.time()
        got = self._fetch_usage(key)
        if got is None:
            return False
        usage, limit = got
        s["used"] = usage
        if usage < limit:
            s["exhausted"] = False
            return True
        return False

    def _probe_oldest_exhausted(self) -> str | None:
        """全部耗尽时:按最近探测时间升序,探测"到期"的 key,重置了就返回它。"""
        now = time.time()
        state = self._state
        candidates = [
            k for k in self._keys
            if state[k]["exhausted"] and now - state[k]["last_probe_at"] >= PROBE_INTERVAL
        ]
        candidates.sort(key=lambda k: state[k]["last_probe_at"])
        for k in candidates:
            if self._probe_reset(k):
                return k
        return None

    def _lazy_probe_due(self) -> None:
        """时间门控懒探测:每次 search 顺手探"一个"到期的耗尽 key(最多一个)。"""
        now = time.time()
        state = self._state
        for k in self._keys:
            if state[k]["exhausted"] and now - state[k]["last_probe_at"] >= PROBE_INTERVAL:
                self._probe_reset(k)
                return

    # ---------------- 选 key(锁内调用) ----------------

    def _pick_key(self) -> str:
        now = time.time()
        state = self._state
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
        return key

    # ---------------- 对外:搜索 ----------------

    def search(self, query: str, **kwargs) -> dict:
        self._ensure_started()  # 首搜前的启动校准;startup_probe=False 时为空操作

        tried: set[str] = set()
        while len(tried) <= len(self._keys):  # 上限仅防御;每 key 一次即自然收敛
            with self._lock:
                self._lazy_probe_due()
                try:
                    key = self._pick_key()
                except RuntimeError:
                    raise RuntimeError("所有 Tavily key 配额均已耗尽") from None
                self._calibrate_if_stale(key)
                if self._state[key]["exhausted"]:
                    continue  # 阈值校准发现已耗尽,当次直接换下一个,不发注定失败的请求
            tried.add(key)

            data = self._do_search(key, query, **kwargs)
            if data is not None:
                return data

            # 403:配额耗尽 → 标记,下一轮换下一个 key,直到试穿候选池
            with self._lock:
                self._state[key]["exhausted"] = True
                self._state[key]["last_probe_at"] = time.time()
        raise RuntimeError("所有 Tavily key 配额均已耗尽")

    def usage(self) -> dict[str, dict]:
        """各 key 的本地用量视图(只读内存计数,不发网络请求)。

        数值是尽力而为的估计:仅统计本进程的使用与校准结果,可能与
        Tavily 后台的精确值有偏差;多设备/多程序共用同一 key 时偏差更大。
        """
        with self._lock:
            return {
                k: {
                    "used": s["used"],
                    "exhausted": s["exhausted"],
                    "limit": self._limit_for(k),
                    "remaining": max(self._limit_for(k) - s["used"], 0),
                }
                for k, s in self._state.items()
            }

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
                self._state[key]["used"] += cost
            return data

        if resp.status_code == 403:
            return None  # 调用方负责标记耗尽

        if resp.status_code == 429:
            raise RuntimeError("Tavily 触发限流(429),请稍后重试")

        raise RuntimeError(f"Tavily 请求异常: HTTP {resp.status_code} {resp.text[:200]}")


# 模块级单例,供工具/CLI 复用
_rotator: TavilyRotator | None = None


def get_rotator(keys: list[str] | None = None, *, startup_probe: bool = True) -> TavilyRotator:
    """获取单例。默认从环境变量 TAVILY_SEARCH_KEYS(逗号分隔)读取 key;也可显式传入。

    keys / startup_probe 仅首次调用生效(单例构造后就固定)。
    """
    global _rotator
    if _rotator is None:
        if keys is None:
            keys = [k.strip() for k in os.environ.get("TAVILY_SEARCH_KEYS", "").split(",") if k.strip()]
        _rotator = TavilyRotator(keys, startup_probe=startup_probe)
    return _rotator
