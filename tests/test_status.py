"""status 展示侧测试：临时目录合成账本，全程离线、不读真实 state/。"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
from unittest import mock
import unittest
from contextlib import redirect_stderr, redirect_stdout

from aisubench import cli as cli_module
from aisubench import status as status_module
from aisubench.cli import main
from aisubench.ledger import append_sample
from aisubench.status import (collect_status, collect_subscriptions,
                              mask_account, pace_note, pool_window_hours,
                              relative_time, run_status)

CONFIG = {"subscriptions": {"demo": {"pools": ["5h"]}},
          "calibration": {"granularity_pct": 1.0}}
CONFIG_MULTI = {"subscriptions": {"demo": {"pools": ["5h", "week", "month"]}},
                "calibration": {"granularity_pct": 1.0}}
CONFIG_TWO_SUBS = {
    "subscriptions": {
        "mock1": {"label": "Max 20x", "account": "t***@g***.com",
                  "agent": "claude", "host": "workstation",
                  "pools": ["5h", "week"]},
        "mock2": {"label": "Pro", "agent": "kimi",
                  "pools": ["5h", "week"]},
    },
    "calibration": {"granularity_pct": 1.0},
}


def mk(ts, pools, input=0, cached=0, output=0, clean=True, subscription="demo",
       agent="t"):
    sample = {"ts": float(ts), "agent": agent, "source": "watch",
              "usage": {"input": input, "cached": cached, "output": output,
                        "requests": 1},
              "pools": pools, "clean": clean}
    if subscription is not None:
        sample["subscription"] = subscription
    return sample


def single_pool_samples():
    """3 个有效对：Σtokens=3000、ΣΔ=6 → R=500、Q=50,000；
    q_low=3000/7×100≈42,857、q_high=3000/5×100=60,000；
    24h 速率窗口含全部样本：3000 tokens ÷ 3h = 1,000 tok/h；
    ETA = (100−6)×500 ÷ 1000 = 47.0 h。"""
    return [mk(0, {"5h": 0}),
            mk(3600, {"5h": 1}, input=500),
            mk(7200, {"5h": 3}, input=400, output=600),
            mk(10800, {"5h": 6}, input=1000, cached=500)]


class StatusTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write_ledger(self, name, samples):
        path = self.root / name
        for item in samples:
            append_sample(path, item)
        return path

    def status_text(self, ledger, config=CONFIG, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = run_status(ledger=ledger, config=config, **kwargs)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def rows(self, text):
        return {line.split()[0]: line for line in text.splitlines()
                if line and line.split()[0] in ("5h", "week", "month")}

    def test_missing_ledger_prints_watch_hint(self):
        text = self.status_text(self.root / "nowhere.jsonl")
        self.assertIn("还没有样本", text)
        self.assertIn("正常的初始状态", text)
        self.assertIn("aisubench watch", text)
        self.assertIn("文件尚不存在", text)

    def test_empty_ledger_same_hint_without_missing_note(self):
        path = self.root / "ledger.jsonl"
        path.write_text("", encoding="utf-8")
        text = self.status_text(path)
        self.assertIn("还没有样本", text)
        self.assertNotIn("文件尚不存在", text)

    def test_single_pool_report_numbers(self):
        path = self.write_ledger("ledger.jsonl", single_pool_samples())
        text = self.status_text(path)
        self.assertIn("样本数：4（速率窗口 24 小时；估计用全部样本）", text)
        self.assertIn("监测跨度：3.0 小时", text)
        self.assertNotIn("未观测渠道", text)  # 全部 clean 时不外溢提示
        self.assertIn("## demo", text)      # 按订阅分组的分节标题
        row = self.rows(text)["5h"]
        self.assertIn("6%", row)
        self.assertIn("3,000", row)                       # 已用≈tokens
        self.assertIn("50,000 (42,857–60,000)", row)      # Q 与 ±g 区间
        self.assertIn("1,000 tok/h", row)
        self.assertIn("47.0 h", row)
        self.assertIn("对=3 重置=0", row)
        self.assertNotIn("仅供参考", row)                 # n_pairs=3 不触发提示

    def test_multi_pool_rows_and_missing_configured_pool(self):
        samples = [mk(0, {"5h": 0, "week": 0}),
                   mk(3600, {"5h": 1, "week": 10}, input=500),
                   mk(7200, {"5h": 3, "week": 20}, input=400, output=600),
                   mk(10800, {"5h": 6, "week": 30}, input=1000, cached=500)]
        path = self.write_ledger("ledger.jsonl", samples)
        text = self.status_text(path, config=CONFIG_MULTI)
        rows = self.rows(text)
        self.assertEqual(list(rows), ["5h", "week", "month"])  # 配置顺序在前
        # week：R=3000/30=100 → Q=10,000；ETA=(100−30)×100÷1000=7.0 h
        self.assertIn("10,000 (9,677–10,345)", rows["week"])
        self.assertIn("7.0 h", rows["week"])
        self.assertIn("账本中暂无该池样本", rows["month"])
        self.assertIn("—", rows["month"])

    def test_insufficient_delta_state(self):
        path = self.write_ledger("ledger.jsonl",
                                 [mk(0, {"5h": 5}), mk(3600, {"5h": 5}, input=1000)])
        row = self.rows(self.status_text(path))["5h"]
        self.assertIn("数据不足(Δ 未超粒度)", row)
        self.assertIn("不可用", row)      # 无比率 → ETA 不可用
        self.assertIn("对=0 重置=0", row)

    def test_clean_only_switch(self):
        samples = [mk(0, {"5h": 0}),
                   mk(60, {"5h": 2}, input=400, clean=False),
                   mk(120, {"5h": 4}, input=400, clean=False)]
        path = self.write_ledger("ledger.jsonl", samples)
        default = self.status_text(path)
        self.assertIn("未观测渠道", default)          # external_tokens=800 提示
        # ΣΔ=4、g=1 → q_low=800/5×100=16,000、q_high=800/3×100≈26,667
        self.assertIn("20,000 (16,000–26,667)", self.rows(default)["5h"])
        clean = self.status_text(path, clean_only=True)
        self.assertIn("仅 clean 样本", clean)
        self.assertIn("数据不足(Δ 未超粒度)", self.rows(clean)["5h"])

    def test_eta_none_when_pool_exhausted(self):
        path = self.write_ledger("ledger.jsonl",
                                 [mk(0, {"5h": 50}), mk(3600, {"5h": 100}, input=22500)])
        row = self.rows(self.status_text(path))["5h"]
        self.assertIn("22,500 tok/h", row)   # 速率可用
        self.assertIn("不可用", row)         # 剩余为 0 → ETA 不可用

    def test_low_pairs_hint(self):
        path = self.write_ledger("ledger.jsonl",
                                 [mk(0, {"5h": 0}), mk(60, {"5h": 1}, input=500),
                                  mk(120, {"5h": 3}, input=1000)])
        row = self.rows(self.status_text(path))["5h"]
        self.assertIn("对=2", row)
        self.assertIn("仅供参考", row)


class SubscriptionGroupTests(unittest.TestCase):
    """collect_subscriptions：样本按 subscription 分组、显示字段兜底、节奏分析。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def collect(self, samples, config=CONFIG_TWO_SUBS, **kwargs):
        ledger = self.root / "ledger.jsonl"
        for item in samples:
            append_sample(ledger, item)
        return collect_subscriptions(config, ledger, **kwargs)

    def subs_by_name(self, data):
        return {sub["name"]: sub for sub in data["subscriptions"]}

    def test_samples_grouped_by_subscription(self):
        samples = [mk(0, {"5h": 10}, subscription="mock1"),
                   mk(3600, {"5h": 20}, input=500, subscription="mock1"),
                   mk(0, {"5h": 40}, subscription="mock2"),
                   mk(3600, {"5h": 60}, input=200, subscription="mock2")]
        subs = self.subs_by_name(self.collect(samples))
        self.assertEqual(set(subs), {"mock1", "mock2"})
        self.assertEqual(subs["mock1"]["n_samples"], 2)
        self.assertEqual(subs["mock1"]["last_sample_ts"], 3600.0)
        pool = {p["name"]: p for p in subs["mock2"]["pools"]}["5h"]
        self.assertEqual(pool["current_pct"], 60.0)
        # 各订阅估计互不影响：mock2 的 ΣΔ=20、Σtokens=200 → R=10
        self.assertEqual(pool["quota_tokens"], 1000.0)
        # 显示字段：声明的 label/account/agent/host + 未声明 host 兜底 localhost
        self.assertEqual(subs["mock1"]["label"], "Max 20x")
        self.assertEqual(subs["mock1"]["account"], "t***@g***.com")
        self.assertEqual(subs["mock1"]["agent"], "claude")
        self.assertEqual(subs["mock1"]["host"], "workstation")
        self.assertEqual(subs["mock2"]["label"], "Pro")
        self.assertIsNone(subs["mock2"]["account"])
        self.assertEqual(subs["mock2"]["host"], "localhost")

    def test_legacy_samples_fall_into_default_group(self):
        samples = [mk(0, {"5h": 10}, subscription=None),
                   mk(3600, {"5h": 20}, input=500, subscription=None)]
        data = self.collect(samples)
        subs = self.subs_by_name(data)
        self.assertIn("default", subs)
        self.assertEqual(subs["default"]["n_samples"], 2)
        self.assertEqual(subs["default"]["label"], "default")
        # 未声明订阅的 agent 兜底取最新样本的 agent
        self.assertEqual(subs["default"]["agent"], "t")
        self.assertEqual(subs["default"]["host"], "localhost")
        # 声明了但无样本的订阅仍然出现（配置池行保持可见）
        self.assertIn("mock1", subs)
        self.assertFalse(subs["mock1"]["pools"][0]["has_samples"])
        # 配置声明的订阅排在发现的分组之前
        self.assertEqual([s["name"] for s in data["subscriptions"]][:2],
                         ["mock1", "mock2"])

    def test_analysis_pace_notes(self):
        # mock1：5h 池 eta≈0.07 < 5×0.5 → 偏快；week 池 eta≈642 > 168×2 → 偏慢。
        # R 由全部有效对累计（Σtokens/ΣΔ≈1964），速率只计 24h 窗口（150 tok/h）。
        samples = [mk(0, {"5h": 0, "week": 0}, subscription="mock1"),
                   mk(3600, {"5h": 99.9, "week": 50}, input=100000,
                            subscription="mock1"),
                   mk(356400, {"5h": 99.95, "week": 50.5}, input=50,
                              subscription="mock1"),
                   mk(360000, {"5h": 99.99, "week": 51}, input=100,
                              subscription="mock1")]
        data = self.collect(samples)
        sub = self.subs_by_name(data)["mock1"]
        self.assertIn("5h 用量进度偏快", sub["analysis"])
        self.assertIn("week 用量进度偏慢", sub["analysis"])
        self.assertIn(" · ", sub["analysis"])
        self.assertIn("分析：", run_status_rendered(data))

    def test_no_analysis_when_pace_normal(self):
        # 5h 池：R=50、rate=100 → eta=9.0，落在 [2.5, 10] 区间内 → 无分析行
        samples = [mk(0, {"5h": 80}, subscription="mock1"),
                   mk(3600, {"5h": 82}, input=100, subscription="mock1")]
        sub = self.subs_by_name(self.collect(samples))["mock1"]
        self.assertEqual(sub["analysis"], "")

    def test_stale_flag(self):
        samples = [mk(1000, {"5h": 10}, subscription="mock1"),
                   mk(1600, {"5h": 20}, input=100, subscription="mock1")]
        data = self.collect(samples, now=1600 + 4 * 300,
                            stale_interval_sec=300)
        self.assertTrue(self.subs_by_name(data)["mock1"]["stale"])
        data = self.collect(samples, now=1600 + 2 * 300,
                            stale_interval_sec=300)
        self.assertFalse(self.subs_by_name(data)["mock1"]["stale"])


def run_status_rendered(data):
    return status_module.render_status(data)


class HelperFunctionTests(unittest.TestCase):
    """节奏判断边界、池窗口解析、相对时间格式化、账号掩码。"""

    def pool(self, name, eta):
        return {"name": name, "has_samples": True, "eta_hours": eta}

    def test_pool_window_hours(self):
        self.assertEqual(pool_window_hours("5h"), 5.0)
        self.assertEqual(pool_window_hours("7d"), 168.0)
        self.assertEqual(pool_window_hours("week"), 168.0)
        self.assertEqual(pool_window_hours("month"), 720.0)
        self.assertEqual(pool_window_hours("unknown"), 24.0)
        self.assertEqual(pool_window_hours(""), 24.0)

    def test_pace_boundaries(self):
        # 5h 池窗口一半 = 2.5、两倍 = 10：边界值本身不算偏快/偏慢
        self.assertIsNone(pace_note(self.pool("5h", 2.5)))
        self.assertIsNone(pace_note(self.pool("5h", 10.0)))
        self.assertEqual(pace_note(self.pool("5h", 2.4)), "5h 用量进度偏快")
        self.assertEqual(pace_note(self.pool("5h", 10.1)), "5h 用量进度偏慢")
        self.assertIsNone(pace_note(self.pool("5h", 5.0)))
        # 无 ETA / 无样本 → 不判断
        self.assertIsNone(pace_note(self.pool("5h", None)))
        self.assertIsNone(pace_note({"name": "5h", "has_samples": False,
                                     "eta_hours": 1.0}))

    def test_relative_time(self):
        now = 100000.0
        self.assertEqual(relative_time(None, now), "无样本")
        self.assertEqual(relative_time(now - 30, now), "刚刚")
        self.assertEqual(relative_time(now - 120, now), "2 分钟前")
        self.assertEqual(relative_time(now - 59 * 60, now), "59 分钟前")
        self.assertEqual(relative_time(now - 12 * 3600, now), "12 小时前")
        self.assertEqual(relative_time(now - 26 * 3600, now), "1 天前")
        self.assertEqual(relative_time(now + 60, now), "刚刚")  # 未来时间钳到 0

    def test_mask_account(self):
        self.assertEqual(mask_account("test@gmail.com"), "t***@g***.com")
        self.assertEqual(mask_account("t***@g***.com"), "t***@g***.com")  # 幂等
        self.assertEqual(mask_account("ab"), "a***")
        self.assertIsNone(mask_account(None))
        self.assertIsNone(mask_account(""))

    def test_collect_status_keeps_flat_pools(self):
        # shells/viewmodel 兼容契约：collect_status 仍输出扁平 pools + prev_pct
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            for item in single_pool_samples():
                append_sample(ledger, item)
            data = collect_status(CONFIG, ledger)
        self.assertIn("pools", data)
        self.assertNotIn("subscriptions", data)
        pool = data["pools"][0]
        self.assertEqual(pool["name"], "5h")
        self.assertEqual(pool["current_pct"], 6.0)
        self.assertEqual(pool["prev_pct"], 3.0)  # 上一次读数（用于恢复检测）


class ResetDetectionTests(unittest.TestCase):
    """额度恢复检测：最新读数低于上一样本读数 → prev_pct > current_pct。"""

    def test_prev_pct_marks_pool_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            samples = [mk(0, {"5h": 40}),
                       mk(3600, {"5h": 55}, input=750),
                       mk(7200, {"5h": 12}, input=100)]  # 55→12 回落 = 重置/恢复
            for item in samples:
                append_sample(ledger, item)
            data = collect_subscriptions(CONFIG, ledger)
        pool = {p["name"]: p for p in data["subscriptions"][0]["pools"]}["5h"]
        self.assertEqual(pool["current_pct"], 12.0)
        self.assertEqual(pool["prev_pct"], 55.0)
        self.assertGreater(pool["resets"], 0)

    def test_no_recovery_when_pct_rises(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.jsonl"
            for item in single_pool_samples():
                append_sample(ledger, item)
            data = collect_subscriptions(CONFIG, ledger)
        pool = {p["name"]: p for p in data["subscriptions"][0]["pools"]}["5h"]
        self.assertFalse(pool["current_pct"] < pool["prev_pct"])  # 6 > 3，非恢复


class StatusCliTests(unittest.TestCase):
    """CLI 层：--window-hours 透传与错误路径（非正窗口 → rc 1）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.jsonl"
        for item in single_pool_samples():
            append_sample(self.ledger, item)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(status_module, "load_config", lambda *a, **k: CONFIG), \
                mock.patch.object(cli_module, "load_config", lambda *a, **k: CONFIG), \
                redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_cli_default_window(self):
        rc, out, _ = self.run_cli(["status", "--ledger", str(self.ledger)])
        self.assertEqual(rc, 0)
        self.assertIn("47.0 h", out)

    def test_cli_window_hours_changes_rate_and_eta(self):
        # 1 小时窗口只含最新两点（Σtokens=1000+1500、跨度 1h → 2,500 tok/h），
        # ETA=(100−6)×500÷2500=18.8 h。
        rc, out, _ = self.run_cli(["status", "--ledger", str(self.ledger),
                                   "--window-hours", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("2,500 tok/h", out)
        self.assertIn("18.8", out)

    def test_cli_json_outputs_parseable_collect_subscriptions(self):
        rc, out, _ = self.run_cli(["status", "--ledger", str(self.ledger),
                                   "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("subscriptions", data)
        self.assertIn("n_samples", data)
        sub = {s["name"]: s for s in data["subscriptions"]}["demo"]
        pool = {p["name"]: p for p in sub["pools"]}["5h"]
        self.assertEqual(pool["current_pct"], 6.0)

    def test_cli_json_combines_with_window_hours_and_clean_only(self):
        rc, out, _ = self.run_cli(["status", "--ledger", str(self.ledger),
                                   "--json", "--window-hours", "1",
                                   "--clean-only"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["window_hours"], 1.0)
        self.assertTrue(data["clean_only"])

    def test_cli_nonpositive_window_is_error(self):
        rc, _, err = self.run_cli(["status", "--ledger", str(self.ledger),
                                   "--window-hours", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("必须为正数", err)


if __name__ == "__main__":
    unittest.main()
