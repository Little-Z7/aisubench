"""watch/ledger 采样侧测试：全程使用临时目录与假探针，不联网、不调真实 arkcli。"""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import tempfile
from unittest import mock
import unittest
from contextlib import redirect_stderr, redirect_stdout

from aisubench import watch as watch_module
from aisubench.cli import main
from aisubench.ledger import append_sample, latest_sample, load_samples
from aisubench.meters.kimi import KimiMeter
from aisubench.watch import WatchSession, load_offsets, run_watch, save_offsets


def kimi_line(input_other=10, output=5, cache_read=100, cache_creation=2, time_ms=1789111388997) -> str:
    return json.dumps({
        "type": "usage.record", "agentId": "main", "model": "demo",
        "usage": {"inputOther": input_other, "output": output,
                  "inputCacheRead": cache_read, "inputCacheCreation": cache_creation},
        "time": time_ms,
    }) + "\n"


class DictProbe:
    continuous = True

    def __init__(self, pools=None):
        self.pools = pools if pools is not None else {"5h": 12.0, "week": 34.0}

    def snapshot(self):
        return dict(self.pools)


class FlakyProbe(DictProbe):
    """前 ``failures`` 次 snapshot 抛异常，模拟网络/探针失败。"""

    def __init__(self, failures=1, pools=None):
        super().__init__(pools)
        self.failures = failures
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("模拟探针失败")
        return super().snapshot()


class SilentManualProbe:
    continuous = False

    def snapshot(self):
        return {}


def usage_total(sample: dict) -> int:
    usage = sample["usage"]
    return usage["input"] + usage["cached"] + usage["output"]


class LedgerTests(unittest.TestCase):
    def test_append_creates_parents_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deep" / "ledger.jsonl"
            append_sample(path, {"ts": 2.0, "agent": "a", "source": "watch",
                                 "usage": {"input": 1, "cached": 2, "output": 3, "requests": 1},
                                 "pools": {"5h": 10.0}, "clean": True})
            append_sample(path, {"ts": 1.0, "agent": "a", "source": "watch",
                                 "usage": {"input": 0, "cached": 0, "output": 0, "requests": 0},
                                 "pools": {}, "clean": False})
            samples = load_samples(path)
            self.assertEqual([s["ts"] for s in samples], [1.0, 2.0])  # 按 ts 排序
            self.assertEqual(samples[1]["usage"]["cached"], 2)
            self.assertEqual(latest_sample(path)["ts"], 2.0)

    def test_bad_lines_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_bytes(
                b'{"ts": 2.0}\n'
                + b"not json at all\n"
                + b'{"ts": broken\n'
                + b"\n"
                + b"[1, 2, 3]\n"
                + b'{"ts": 1.5}\n'
                + b"\xff\xfe binary junk\n"
            )
            samples = load_samples(path)
            self.assertEqual([s["ts"] for s in samples], [1.5, 2.0])
            self.assertEqual(latest_sample(path)["ts"], 2.0)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nowhere.jsonl"
            self.assertEqual(load_samples(path), [])
            self.assertIsNone(latest_sample(path))


class OffsetsStateTests(unittest.TestCase):
    def test_corrupt_offsets_file_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "meter_offsets.json"
            path.write_text("{invalid", encoding="utf-8")
            self.assertEqual(load_offsets(path), {})
            path.write_text('{"a": -5, "b": "x", "c": 7, "d": true}', encoding="utf-8")
            self.assertEqual(load_offsets(path), {"a": 0, "c": 7})  # 负偏移钳到 0

    def test_save_offsets_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "meter_offsets.json"
            save_offsets(path, {"/x/wire.jsonl": 42})
            self.assertEqual(load_offsets(path), {"/x/wire.jsonl": 42})
            self.assertFalse(list(path.parent.glob("*.tmp")))


class SessionSamplingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "wire.jsonl"
        self.ledger = self.root / "ledger.jsonl"
        self.offsets = self.root / "meter_offsets.json"

    def tearDown(self):
        self._tmp.cleanup()

    def make_session(self, probe=None, agent="kimi-x", clean_window=600.0):
        return WatchSession(agent=agent, meter_name="kimi", log_path=self.log,
                            probe=probe or DictProbe(), ledger_path=self.ledger,
                            offsets_path=self.offsets, clean_window_sec=clean_window)

    def sample(self, session, now):
        with redirect_stdout(io.StringIO()):
            return session.sample_once(now=now)

    def test_contract_fields(self):
        self.log.write_text(kimi_line(), encoding="utf-8")
        sample = self.sample(self.make_session(), now=1726000000.0)
        self.assertEqual(set(sample), {"ts", "agent", "subscription", "source",
                                       "usage", "pools", "clean"})
        self.assertEqual(set(sample["usage"]), {"input", "cached", "output", "requests"})
        self.assertEqual(sample["source"], "watch")
        self.assertEqual(sample["agent"], "kimi-x")
        # 未配置 [agents.X].subscription 时缺省为 agent 名本身
        self.assertEqual(sample["subscription"], "kimi-x")
        self.assertEqual(sample["pools"], {"5h": 12.0, "week": 34.0})
        # 账本落盘内容与返回值一致
        self.assertEqual(load_samples(self.ledger), [sample])

    def test_explicit_subscription_field(self):
        session = self.make_session()
        session.subscription = "demo"
        sample = self.sample(session, now=1726000000.0)
        self.assertEqual(sample["subscription"], "demo")

    def test_offset_persists_across_sessions_without_double_count(self):
        self.log.write_text(kimi_line(input_other=1, output=1, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        first = self.sample(self.make_session(), now=100.0)
        self.assertEqual(usage_total(first), 2)
        self.assertEqual(load_offsets(self.offsets), {str(self.log): self.log.stat().st_size})
        # 新会话模拟新进程：从持久化偏移续读
        self.log.write_text(self.log.read_text(encoding="utf-8")
                            + kimi_line(input_other=2, output=3, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        second = self.sample(self.make_session(), now=160.0)
        self.assertEqual(second["usage"], {"input": 2, "cached": 0, "output": 3, "requests": 1})
        third = self.sample(self.make_session(), now=220.0)
        self.assertEqual(usage_total(third), 0)
        self.assertEqual(third["usage"]["requests"], 0)

    def test_truncated_log_resets_offset(self):
        self.log.write_text(kimi_line(input_other=1, output=1, cache_read=0, cache_creation=0)
                            + kimi_line(input_other=2, output=2, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        self.sample(self.make_session(), now=100.0)
        self.log.write_text(kimi_line(input_other=7, output=8, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        after_rotation = self.sample(self.make_session(), now=160.0)
        self.assertEqual(after_rotation["usage"]["input"], 7)
        self.assertEqual(after_rotation["usage"]["output"], 8)
        self.assertEqual(load_offsets(self.offsets), {str(self.log): self.log.stat().st_size})

    def test_missing_log_samples_zero_without_crash(self):
        sample = self.sample(self.make_session(), now=100.0)
        self.assertEqual(usage_total(sample), 0)
        self.assertEqual(load_offsets(self.offsets), {str(self.log): 0})

    def test_probe_failure_keeps_offset_and_defers_usage(self):
        self.log.write_text(kimi_line(input_other=1, output=1, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        flaky = FlakyProbe(failures=1)
        session = self.make_session(probe=flaky)
        with self.assertRaises(RuntimeError), redirect_stderr(io.StringIO()):
            session.sample_once(now=100.0)
        self.assertEqual(load_samples(self.ledger), [])
        self.assertFalse(self.offsets.exists())
        # 下一轮把未消费的记录一并计入（不因失败丢读数、也不重复）
        ok = self.make_session()
        sample = self.sample(ok, now=160.0)
        self.assertEqual(sample["usage"], {"input": 1, "cached": 0, "output": 1, "requests": 1})

    def test_clean_threshold_window(self):
        session = self.make_session(clean_window=600.0)
        self.assertIs(self.sample(session, now=1000.0)["clean"], False)  # 首个样本保守 False
        self.assertIs(self.sample(session, now=1000.0 + 599.9)["clean"], True)
        self.assertIs(self.sample(session, now=1000.0 + 599.9 + 600.0)["clean"], False)

    def test_clean_ignores_other_agents_samples(self):
        other = self.make_session(agent="other")
        mine = self.make_session(agent="mine")
        self.sample(mine, now=1000.0)
        self.sample(other, now=1030.0)
        self.assertIs(self.sample(mine, now=1060.0)["clean"], True)  # 同 agent 间隔 60s


class SummaryTests(unittest.TestCase):
    def test_format_summary_shape(self):
        line = WatchSession.format_summary({
            "ts": 1726000000.0, "clean": True,
            "usage": {"input": 100, "cached": 900, "output": 34, "requests": 2},
            "pools": {"5h": 12.0, "week": 34.0},
        })
        self.assertRegex(line, re.compile(
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \+1034 tokens"
            r" \(input=100 cached=900 output=34\) \| 5h=12% week=34% \| clean$"))
        self.assertIn("unclean", WatchSession.format_summary(
            {"ts": 1.0, "usage": {}, "pools": {}, "clean": False}))


class RunWatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "wire.jsonl"
        self.ledger = self.root / "ledger.jsonl"
        self.config = {"agents": {"fake": {"meter": "kimi", "log_path": str(self.log)}},
                       "watch": {"offsets_file": str(self.root / "meter_offsets.json"),
                                 "clean_window_sec": 600}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_once_full_flow_twice_no_double_count(self):
        self.log.write_text(kimi_line(input_other=4, output=2, cache_read=10, cache_creation=1),
                            encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = run_watch("fake", "mock", once=True, ledger=self.ledger, config=self.config)
        self.assertEqual(rc, 0)
        self.assertIn("+17 tokens", out.getvalue())
        samples = load_samples(self.ledger)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["usage"], {"input": 5, "cached": 10, "output": 2, "requests": 1})
        self.assertEqual(samples[0]["subscription"], "fake")  # 缺省为 agent 名
        with redirect_stdout(io.StringIO()):
            rc = run_watch("fake", "mock", once=True, ledger=self.ledger, config=self.config)
        self.assertEqual(rc, 0)
        samples = load_samples(self.ledger)
        self.assertEqual(len(samples), 2)
        self.assertEqual(usage_total(samples[1]), 0)          # 不重复计数
        self.assertIs(samples[1]["clean"], True)              # 间隔小于阈值

    def test_loop_survives_probe_error_and_ctrl_c(self):
        self.log.write_text(kimi_line(input_other=1, output=1, cache_read=0, cache_creation=0),
                            encoding="utf-8")
        flaky = FlakyProbe(failures=1)
        sleeps = []

        def sleep_fn(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                raise KeyboardInterrupt

        err = io.StringIO()
        with mock.patch.dict(watch_module.PROBES, {"flaky": lambda: flaky}), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = run_watch("fake", "flaky", interval=0.5, ledger=self.ledger,
                           config=self.config, sleep_fn=sleep_fn)
        self.assertEqual(rc, 0)
        self.assertIn("警告：本轮采样失败", err.getvalue())
        samples = load_samples(self.ledger)
        self.assertEqual(len(samples), 1)                     # 失败轮不落账本
        self.assertEqual(samples[0]["usage"], {"input": 1, "cached": 0, "output": 1, "requests": 1})

    def test_manual_probe_loop_hint(self):
        sleeps = []

        def sleep_fn(seconds):
            sleeps.append(seconds)
            raise KeyboardInterrupt

        err = io.StringIO()
        with mock.patch.dict(watch_module.PROBES, {"manual_hint": SilentManualProbe}), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = run_watch("fake", "manual_hint", interval=1.0, ledger=self.ledger,
                           config=self.config, sleep_fn=sleep_fn)
        self.assertEqual(rc, 0)
        self.assertIn("--once", err.getvalue())

    def test_subscription_from_agent_config(self):
        # [agents.X].subscription 写入样本的 subscription 字段
        config = {"agents": {"fake": {"meter": "kimi", "log_path": str(self.log),
                                     "subscription": "demo"}},
                  "watch": {"offsets_file": str(self.root / "meter_offsets.json")}}
        self.log.write_text(kimi_line(input_other=1, output=1), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            rc = run_watch("fake", "mock", once=True, ledger=self.ledger,
                           config=config)
        self.assertEqual(rc, 0)
        self.assertEqual(load_samples(self.ledger)[0]["subscription"], "demo")

    def test_unknown_agent_raises(self):
        with self.assertRaises(ValueError), redirect_stdout(io.StringIO()):
            run_watch("ghost", "mock", once=True, ledger=self.ledger, config=self.config)


class MockAgentCliTests(unittest.TestCase):
    """冒烟契约：CLI 层跑 `watch --agent mock --probe mock --once` 两次，
    第二条样本 usage 不重复。state/ 路径全部 patch 到临时目录。"""

    def test_cli_watch_mock_once_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = root / "ledger.jsonl"
            config = {"agents": {}, "watch": {"offsets_file": str(root / "meter_offsets.json")}}
            with mock.patch.object(watch_module, "MOCK_LOG_PATH", root / "state" / "mock_wire.jsonl"), \
                    mock.patch.object(watch_module, "OFFSETS_FILE", root / "meter_offsets.json"), \
                    mock.patch.object(watch_module, "load_config", lambda *a, **k: config), \
                    redirect_stdout(io.StringIO()) as out:
                first = main(["watch", "--agent", "mock", "--probe", "mock",
                              "--once", "--ledger", str(ledger)])
                second = main(["watch", "--agent", "mock", "--probe", "mock",
                               "--once", "--ledger", str(ledger)])
            self.assertEqual((first, second), (0, 0))
            samples = load_samples(ledger)
            self.assertEqual(len(samples), 2)
            self.assertGreater(usage_total(samples[0]), 0)     # 合成日志首轮计入
            self.assertEqual(usage_total(samples[1]), 0)       # 第二轮不重复计数
            self.assertIs(samples[0]["clean"], False)
            self.assertIs(samples[1]["clean"], True)
            summary_lines = [line for line in out.getvalue().splitlines() if "+0 tokens" in line]
            self.assertEqual(len(summary_lines), 1)


class MockMeterResolveTests(unittest.TestCase):
    def test_seeded_mock_log_parses_as_kimi(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "mock_wire.jsonl"
            with mock.patch.object(watch_module, "MOCK_LOG_PATH", path):
                meter_name, resolved = watch_module._resolve_meter("mock", {})
            self.assertEqual(meter_name, "kimi")
            self.assertEqual(resolved, path)
            self.assertTrue(path.exists())
            meter = KimiMeter(path)
            usage = meter.collect()
            self.assertEqual(usage.requests, 2)
            self.assertEqual(usage.input, 48 + 7)             # inputOther + cacheCreation
            self.assertEqual(usage.cached, 300 + 50)
            self.assertEqual(usage.output, 12 + 3)
            # 幂等：已存在时不重复追加种子
            with mock.patch.object(watch_module, "MOCK_LOG_PATH", path):
                watch_module._resolve_meter("mock", {})
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_explicit_meter_config_resolves_relative_to_root(self):
        meter_name, path = watch_module._resolve_meter(
            "claude", {"meter": "Claude", "log_path": "state/x.jsonl"})
        self.assertEqual(meter_name, "claude")
        self.assertEqual(path, watch_module.ROOT / "state/x.jsonl")

    def test_missing_meter_config_raises(self):
        with self.assertRaises(ValueError):
            watch_module._resolve_meter("who", {})
        with self.assertRaises(ValueError):
            watch_module._resolve_meter("who", {"meter": "kimi"})


if __name__ == "__main__":
    unittest.main()
