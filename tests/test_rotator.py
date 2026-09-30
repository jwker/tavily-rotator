"""tavily_rotator 测试(纯标准库,python -m unittest 或 pytest 均可运行)。

所有网络请求都被 mock,只验证本地逻辑;核心是复现并回归
"多进程并发 _save() 原子写冲突(Errno 2)"这个 bug。

运行:
    python -m unittest tests.test_rotator -v
"""

import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tavily_rotator.rotator import TavilyRotator

# 老版本multiprocessing默认fork启动器下,默认start方法可能是fork,统一用spawn更接近
# uvicorn多worker的真实场景(每个worker是全新解释器)。子进程代码写在模块顶层,
# 保证spawn模式下可pickle。
def _child_saves(data_file: str, worker_id: int, rounds: int, started: "multiprocessing.Event", done: "multiprocessing.Event"):
    """子进程:构造自己的实例(自己的锁、自己的内存态),等齐后并发 _save()。

    所有子进程用同一个 key 名:最终文件内容 = 某个 writer 的完整快照
    (跨进程整文件覆盖是已知语义,这里只回归"不再崩溃/不产生脏文件")。
    """
    rot = TavilyRotator(keys=["tvly-shared"], data_file=data_file, limit=10_000)
    started.set()
    done.wait(timeout=30)
    for i in range(rounds):
        # 模拟记账路径:改内存态再落盘(pick_key/do_search 里的 _save 就是这么用的)
        with rot._lock:
            rot._state["keys"]["tvly-shared"]["used"] += 1
            rot._save()
    # 校验:目标文件每次都存在且可解析(老代码在竞态下会直接抛 Errno 2)


def _fake_response(status_code: int, payload: dict):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    return resp


class SaveBasicsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_file = Path(self._tmpdir.name) / "tavily_usage.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make(self) -> TavilyRotator:
        return TavilyRotator(keys=["tvly-a"], data_file=str(self.data_file), limit=100)

    def test_save_writes_target_and_leaves_no_tmp(self):
        rot = self._make()
        rot._state["keys"]["tvly-a"]["used"] = 7
        rot._save()
        self.assertTrue(self.data_file.exists())
        on_disk = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["keys"]["tvly-a"]["used"], 7)
        leftovers = list(self.data_file.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"不应残留临时文件: {leftovers}")

    def test_save_repeatedly_overwrites_same_target(self):
        rot = self._make()
        for used in range(1, 6):
            rot._state["keys"]["tvly-a"]["used"] = used
            rot._save()
        on_disk = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["keys"]["tvly-a"]["used"], 5)

    def test_save_tmp_name_contains_pid_and_tid(self):
        """回归锚点:tmp 名必须带 pid+tid 唯一后缀,不再是固定 .tmp。"""
        rot = self._make()
        observed = []
        real_replace = os.replace

        def spy_replace(src, dst):
            observed.append(os.path.basename(src))
            return real_replace(src, dst)

        with mock.patch("tavily_rotator.rotator.os.replace", side_effect=spy_replace):
            rot._save()
        self.assertEqual(len(observed), 1)
        self.assertIn(f".{os.getpid()}.{threading.get_ident()}.tmp", observed[0])
        self.assertNotEqual(observed[0], self.data_file.name + ".tmp")


class ConcurrentSaveTest(unittest.TestCase):
    """核心回归:多进程/多线程并发 _save() 不再抛 Errno 2。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_file = Path(self._tmpdir.name) / "tavily_usage.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_many_threads_same_instance(self):
        rot = TavilyRotator(keys=["tvly-a"], data_file=str(self.data_file), limit=10_000)
        errors = []

        def worker(n):
            try:
                for _ in range(200):
                    with rot._lock:
                        rot._state["keys"]["tvly-a"]["used"] += 1
                        rot._save()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertEqual(list(self.data_file.parent.glob("*.tmp")), [])
        on_disk = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["keys"]["tvly-a"]["used"], 16 * 200)

    def test_multi_process_multi_thread_stress(self):
        """复现 bug 的原始场景:多进程共享同一 data_file,各自高频 _save()。

        修复前:进程 A/B 撞同一个 xxx.json.tmp,os.replace 抛
        [Errno 2] No such file or directory。
        """
        n_procs, rounds = 8, 30
        started = [multiprocessing.Event() for _ in range(n_procs)]
        done = multiprocessing.Event()
        for i in range(n_procs):
            started[i].clear()
        procs = [
            multiprocessing.Process(
                target=_child_saves,
                args=(str(self.data_file), i, rounds, started[i], done),
            )
            for i in range(n_procs)
        ]
        for p in procs:
            p.start()
        for s in started:
            self.assertTrue(s.wait(timeout=30), "子进程未在超时内就绪")
        time.sleep(0.1)  # 留一点余量让所有子进程都进入 _save 循环前
        done.set()
        for p in procs:
            p.join(timeout=120)
        self.assertEqual([p.exitcode for p in procs], [0] * n_procs,
                         f"有子进程异常退出(修复前这里常见 Errno 2): {[p.exitcode for p in procs]}")
        # 目标文件可解析;最终内容是某个 writer 的完整快照(0 < used <= 总写入次数)
        on_disk = json.loads(self.data_file.read_text(encoding="utf-8"))
        final_used = on_disk["keys"]["tvly-shared"]["used"]
        self.assertGreaterEqual(final_used, rounds,
                                f"最终快照应至少是一个 writer 的完整计数,实际 {final_used}")
        self.assertLessEqual(final_used, n_procs * rounds)
        self.assertEqual(list(self.data_file.parent.glob("*.tmp")), [])

    def test_concurrent_readers_never_see_broken_json(self):
        """原子替换的另一面:读侧(新实例 _load / 外部读文件)永远拿到完整 JSON。"""
        rot = TavilyRotator(keys=["tvly-a"], data_file=str(self.data_file), limit=10_000)
        stop = threading.Event()
        parse_errors = []

        def writer():
            used = 0
            while not stop.is_set():
                used += 1
                with rot._lock:
                    rot._state["keys"]["tvly-a"]["used"] = used
                    rot._save()

        def reader():
            while not stop.is_set():
                try:
                    json.loads(self.data_file.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    pass  # 首次写盘前
                except json.JSONDecodeError as e:
                    parse_errors.append(e)

        w = threading.Thread(target=writer)
        rs = [threading.Thread(target=reader) for _ in range(4)]
        w.start()
        for r in rs:
            r.start()
        time.sleep(1.5)
        stop.set()
        w.join(timeout=10)
        for r in rs:
            r.join(timeout=10)
        self.assertEqual(parse_errors, [], f"读到过半个文件: {parse_errors[:3]}")


class SearchLogicTest(unittest.TestCase):
    """search/_do_search 的本地逻辑:记账、403 容错、429 报错、403 后重试。全程不出网。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_file = Path(self._tmpdir.name) / "tavily_usage.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_200_records_cost(self):
        rot = TavilyRotator(keys=["tvly-a"], data_file=str(self.data_file))
        with mock.patch(
            "tavily_rotator.rotator.requests.post",
            return_value=_fake_response(200, {"results": [], "search_cost": 3}),
        ):
            data = rot.search("q")
        self.assertEqual(data["search_cost"], 3)
        self.assertEqual(rot._state["keys"]["tvly-a"]["used"], 3)
        on_disk = json.loads(self.data_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["keys"]["tvly-a"]["used"], 3)

    def test_403_returns_none_and_429_raises(self):
        rot = TavilyRotator(keys=["tvly-a"], data_file=str(self.data_file))
        with mock.patch(
            "tavily_rotator.rotator.requests.post",
            return_value=_fake_response(403, {"detail": "quota exceeded"}),
        ):
            self.assertIsNone(rot._do_search("tvly-a", "q"))
        with mock.patch(
            "tavily_rotator.rotator.requests.post",
            return_value=_fake_response(429, {"detail": "rate limit"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "429"):
                rot.search("q")

    def test_search_fails_over_to_second_key_on_403(self):
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], data_file=str(self.data_file))
        payloads = [
            _fake_response(403, {"detail": "quota exceeded"}),
            _fake_response(200, {"results": [{"title": "ok"}], "search_cost": 1}),
        ]
        with mock.patch("tavily_rotator.rotator.requests.post", side_effect=payloads):
            data = rot.search("q")
        self.assertEqual(data["results"][0]["title"], "ok")
        self.assertTrue(rot._state["keys"]["tvly-a"]["exhausted"])
        self.assertFalse(rot._state["keys"]["tvly-b"]["exhausted"])
        self.assertEqual(rot._state["keys"]["tvly-b"]["used"], 1)

    def test_403_retries_exhaust_key_pool(self):
        """403 后应遍历整个候选池,而不是只重试一次(旧实现 A、B 都 403 时不试 C)。"""
        rot = TavilyRotator(keys=["tvly-a", "tvly-b", "tvly-c"], data_file=str(self.data_file))
        payloads = [
            _fake_response(403, {"detail": "quota exceeded"}),
            _fake_response(403, {"detail": "quota exceeded"}),
            _fake_response(200, {"results": [{"title": "third"}], "search_cost": 1}),
        ]
        with mock.patch("tavily_rotator.rotator.requests.post", side_effect=payloads):
            data = rot.search("q")
        self.assertEqual(data["results"][0]["title"], "third")
        self.assertTrue(rot._state["keys"]["tvly-a"]["exhausted"])
        self.assertTrue(rot._state["keys"]["tvly-b"]["exhausted"])
        self.assertFalse(rot._state["keys"]["tvly-c"]["exhausted"])
        self.assertEqual(rot._state["keys"]["tvly-c"]["used"], 1)

    def test_all_403_raises_after_trying_every_key(self):
        """全部 key 都 403 → 抛错,且每个 key 恰好被试一次。"""
        keys = ["tvly-a", "tvly-b", "tvly-c"]
        rot = TavilyRotator(keys=keys, data_file=str(self.data_file))
        payloads = [_fake_response(403, {"detail": "quota exceeded"}) for _ in keys]
        with mock.patch("tavily_rotator.rotator.requests.post", side_effect=payloads):
            with self.assertRaisesRegex(RuntimeError, "均已耗尽"):
                rot.search("q")
        for k in keys:
            self.assertTrue(rot._state["keys"][k]["exhausted"])

    def test_each_key_tried_at_most_once_per_search(self):
        """单次 search 内,同一 key 不会被重复尝试(403 标记后不再入池)。"""
        rot = TavilyRotator(keys=["tvly-a", "tvly-b"], data_file=str(self.data_file))
        payloads = [
            _fake_response(403, {"detail": "quota exceeded"}),
            _fake_response(403, {"detail": "quota exceeded"}),
        ]
        with mock.patch("tavily_rotator.rotator.requests.post", side_effect=payloads) as m:
            with self.assertRaisesRegex(RuntimeError, "均已耗尽"):
                rot.search("q")
        tried_keys = [call.kwargs["json"]["api_key"] for call in m.call_args_list]
        self.assertEqual(len(tried_keys), len(set(tried_keys)), f"重复尝试了同一个 key: {tried_keys}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
