"""tavily_rotator 测试(纯标准库,python -m unittest 或 pytest 均可运行)。

所有网络请求都被 mock,只验证本地逻辑;覆盖三块:
- 403 容错与重试:遍历整个候选池,每 key 单次调用内最多试一次
- /usage 校准:启动校准(可关)、阈值校准(节流)、403/24h 懒探测恢复
- 记账与配额:search_cost 累加、按 key 配额、单例

运行:
    python -m unittest tests.test_rotator -v
"""

import threading
import time
import unittest
from unittest import mock

from tavily_rotator.rotator import TavilyRotator, get_rotator


def _fake_response(status_code: int, payload: dict):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = payload.get("detail", "")
    return resp


def _usage_payload(usage: int, limit: int) -> dict:
    return {"key": {"usage": usage, "limit": limit}}


OK_SEARCH = {"results": [{"title": "ok"}], "search_cost": 1}


class BaseTest(unittest.TestCase):
    """公共基类:统一 mock /usage(GET)与 /search(POST),并记录调用。

    子类通过两个旋钮定制响应,不要直接替换 mock.side_effect(会绕过记录):
    - self.usage_responses:  {key: payload} 定制 /usage 响应,缺省 usage=0/limit=1000
    - self.search_responses: [(status, payload), ...] 按序消费;耗尽后回到默认 200
    - 注入异常仍可用 self.mock_usage.side_effect = Exception(...)
    """

    def setUp(self):
        self.usage_calls: list[str] = []
        self.search_calls: list[str] = []
        self.usage_responses: dict[str, dict] = {}
        self.search_responses: list[tuple[int, dict]] = []
        patcher_usage = mock.patch(
            "tavily_rotator.rotator.requests.get", side_effect=self._fake_usage
        )
        patcher_search = mock.patch(
            "tavily_rotator.rotator.requests.post", side_effect=self._fake_search
        )
        self.mock_usage = patcher_usage.start()
        self.mock_search = patcher_search.start()
        self.addCleanup(patcher_usage.stop)
        self.addCleanup(patcher_search.stop)

    def _fake_usage(self, url, headers=None, timeout=None):
        key = headers["Authorization"].removeprefix("Bearer ")
        self.usage_calls.append(key)
        return _fake_response(200, self.usage_responses.get(key, _usage_payload(0, 1000)))

    def _fake_search(self, url, json=None, timeout=None):
        key = json["api_key"]
        self.search_calls.append(key)
        if self.search_responses:
            status, payload = self.search_responses.pop(0)
            return _fake_response(status, payload)
        return _fake_response(200, dict(OK_SEARCH))


class StartupProbeTest(BaseTest):
    """启动校准:首搜前并行探测所有 key,仅一次;可关闭。"""

    def test_first_search_probes_all_keys_once(self):
        rot = TavilyRotator(keys=["tvly-a", "tvly-b", "tvly-c"], limit=1000)
        rot.search("q1")
        rot.search("q2")
        self.assertEqual(sorted(self.usage_calls), ["tvly-a", "tvly-b", "tvly-c"],
                         "首搜前应每 key 恰好校准一次,之后不再探测")

    def test_startup_probe_false_skips_usage(self):
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], startup_probe=False)
        rot.search("q")
        self.assertEqual(self.usage_calls, [], "关闭启动校准后不应有任何 /usage 请求")
        self.assertEqual(self.search_calls, ["tvly-a"])

    def test_startup_probe_marks_already_exhausted_keys(self):
        """启动时发现远端已耗尽的 key 直接标记,省掉注定失败的搜索请求。"""
        self.usage_responses = {
            "tvly-a": _usage_payload(1000, 1000),
            "tvly-b": _usage_payload(10, 1000),
        }
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit=1000)
        rot.search("q")
        self.assertEqual(self.search_calls, ["tvly-b"], "耗尽的 key 不应再被选中")

    def test_probe_failure_degrades_silently(self):
        """/usage 全挂时静默降级,搜索照常进行。"""
        self.mock_usage.side_effect = Exception("network down")
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit=1000)
        data = rot.search("q")
        self.assertEqual(data["results"][0]["title"], "ok")


class ThresholdCalibrationTest(BaseTest):
    """阈值校准:余额 < 配额 10% 时确认真值,60s 内不重复校准。"""

    def _make_low_balance(self, usage_response: dict) -> TavilyRotator:
        """warmup 完成启动校准后,把内存态推到临界(剩余 5% < 10%)。"""
        self.usage_responses["tvly-a"] = usage_response
        rot = TavilyRotator(keys=["tvly-a"], limit=1000)
        rot.search("warmup")
        rot._state["tvly-a"]["used"] = 950
        rot._last_calibration["tvly-a"] = 0.0
        return rot

    def test_low_balance_triggers_calibration(self):
        rot = self._make_low_balance(_usage_payload(960, 1000))
        rot.search("q")
        # 启动校准 1 次 + 阈值校准 1 次
        self.assertEqual(self.usage_calls.count("tvly-a"), 2, "低余额应触发一次阈值校准")
        # 校准写回 960,本次搜索再 +1
        self.assertEqual(rot._state["tvly-a"]["used"], 961)

    def test_calibration_finding_exhausted_switches_key(self):
        """校准发现真值已耗尽 → 标记并当次换 key,不发注定失败的请求。"""
        self.usage_responses = {"tvly-a": _usage_payload(1000, 1000)}
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit=1000)
        rot.search("warmup")
        rot._state["tvly-a"]["used"] = 950
        rot._last_calibration["tvly-a"] = 0.0
        rot.search("q")
        self.assertEqual(self.search_calls[-1], "tvly-b", "校准发现耗尽后应直接换 key")
        self.assertTrue(rot._state["tvly-a"]["exhausted"])

    def test_calibration_is_throttled(self):
        rot = self._make_low_balance(_usage_payload(960, 1000))
        rot.search("q1")  # 触发一次阈值校准
        rot.search("q2")  # 60s 窗口内:不再校准
        self.assertEqual(self.usage_calls.count("tvly-a"), 2, "节流窗口内不应重复校准")
        rot._last_calibration["tvly-a"] = time.time() - 61
        rot.search("q3")
        self.assertEqual(self.usage_calls.count("tvly-a"), 3, "超过节流窗口后应再次校准")

    def test_high_balance_skips_calibration(self):
        rot = TavilyRotator(keys=["tvly-a"], limit=1000)
        rot.search("warmup")  # 仅启动校准
        rot._state["tvly-a"]["used"] = 100  # 剩余 90%,远离阈值
        rot.search("q")
        self.assertEqual(self.usage_calls, ["tvly-a"], "余额充足时不应有额外校准")

    def test_calibration_failure_does_not_block_search(self):
        """阈值校准失败(/usage 挂了)静默降级,搜索照常。"""
        rot = self._make_low_balance(_usage_payload(960, 1000))
        self.mock_usage.side_effect = Exception("network down")
        data = rot.search("q")
        self.assertEqual(data["results"][0]["title"], "ok")
        self.assertEqual(rot._state["tvly-a"]["used"], 951, "校准失败保留本地计数,本次搜索正常记账")


class RetryTest(BaseTest):
    """403 容错:遍历整个候选池,每 key 单次调用内最多试一次。"""

    def test_403_retries_exhaust_key_pool(self):
        """A、B 都 403 时应继续试 C(旧实现只重试一次,会误报全耗尽)。"""
        rot = TavilyRotator(keys=["tvly-a", "tvly-b", "tvly-c"], limit=1000)
        self.search_responses = [
            (403, {"detail": "quota exceeded"}),
            (403, {"detail": "quota exceeded"}),
            (200, {"results": [{"title": "third"}], "search_cost": 1}),
        ]
        data = rot.search("q")
        self.assertEqual(data["results"][0]["title"], "third")
        self.assertTrue(rot._state["tvly-a"]["exhausted"])
        self.assertTrue(rot._state["tvly-b"]["exhausted"])
        self.assertFalse(rot._state["tvly-c"]["exhausted"])

    def test_all_403_raises_after_trying_every_key(self):
        keys = ["tvly-a", "tvly-b", "tvly-c"]
        rot = TavilyRotator(keys=keys, limit=1000)
        self.search_responses = [(403, {"detail": "quota exceeded"}) for _ in keys]
        with self.assertRaisesRegex(RuntimeError, "均已耗尽"):
            rot.search("q")
        for k in keys:
            self.assertTrue(rot._state[k]["exhausted"])

    def test_each_key_tried_at_most_once_per_search(self):
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit=1000)
        self.search_responses = [(403, {"detail": "quota exceeded"}) for _ in range(2)]
        with self.assertRaisesRegex(RuntimeError, "均已耗尽"):
            rot.search("q")
        self.assertEqual(len(self.search_calls), len(set(self.search_calls)),
                         f"重复尝试了同一个 key: {self.search_calls}")

    def test_429_raises_without_fallback(self):
        """429 是限流不是耗尽:直接抛错,不切换 key。"""
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit=1000)
        self.search_responses = [(429, {"detail": "rate limit"})]
        with self.assertRaisesRegex(RuntimeError, "429"):
            rot.search("q")
        self.assertEqual(self.search_calls, ["tvly-a"])


class AccountingTest(BaseTest):
    """记账与配额:search_cost 累加、按 key 配额、24h 懒探测恢复、单例。"""

    def test_200_records_cost(self):
        rot = TavilyRotator(keys=["tvly-a"], limit=1000)
        self.search_responses = [(200, {"results": [], "search_cost": 3})]
        rot.search("q")
        self.assertEqual(rot._state["tvly-a"]["used"], 3)

    def test_per_key_limits(self):
        """按 key 配额:A 上限 1(用完即排除),B 上限很大,应切到 B。"""
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], limit={"tvly-a": 1, "tvly-b": 1000})
        rot._state["tvly-a"]["used"] = 1  # A 按自身配额已满
        rot.search("q")
        self.assertEqual(self.search_calls[-1], "tvly-b")

    def test_lazy_probe_reenables_after_reset_window(self):
        """耗尽 key 过了 24h 门控 → 懒探测发现配额重置 → 重新入池。"""
        rot = TavilyRotator(keys=["tvly-a"], limit=1000)
        rot.search("warmup")
        rot._state["tvly-a"]["exhausted"] = True
        rot._state["tvly-a"]["last_probe_at"] = time.time() - 24 * 3600 - 1
        rot.search("q")
        self.assertFalse(rot._state["tvly-a"]["exhausted"], "到期探测应重新启用该 key")
        self.assertEqual(self.search_calls[-1], "tvly-a")

    def test_thread_safety_of_accounting(self):
        """并发 search 时记账不串账(进程内锁保护)。"""
        rot = TavilyRotator(keys=["tvly-a"], limit=10_000, startup_probe=False)
        errors = []

        def worker():
            try:
                for _ in range(50):
                    rot.search("q")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(rot._state["tvly-a"]["used"], 8 * 50)

    def test_usage_view(self):
        rot = TavilyRotator(keys=["tvly-a"], limit={"tvly-a": 500}, startup_probe=False)
        self.search_responses = [(200, {"results": [], "search_cost": 40})]
        rot.search("q")
        view = rot.usage()
        self.assertEqual(
            view["tvly-a"],
            {"used": 40, "exhausted": False, "limit": 500, "remaining": 460},
        )

    def test_get_rotator_is_singleton(self):
        r1 = get_rotator(keys=["tvly-a"])
        r2 = get_rotator(keys=["tvly-b"])
        self.assertIs(r1, r2)

    def test_empty_keys_raises(self):
        with self.assertRaises(ValueError):
            TavilyRotator(keys=[])


if __name__ == "__main__":
    unittest.main(verbosity=2)
