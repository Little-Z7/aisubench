"""status 展示侧测试：临时目录合成账本，全程离线、不读真实 state/。"""

from __future__ import annotations

import io
from pathlib import Path
import tempfile
from unittest import mock
import unittest
from contextlib import redirect_stderr, redirect_stdout

from aisubench import status as status_module
from aisubench.cli import main
from aisubench.ledger import append_sample
from aisubench.status import run_status

CONFIG = {"subscriptions": {"demo": {"pools": ["5h"]}},
          "calibration": {"granularity_pct": 1.0}}
CONFIG_MULTI = {"subscriptions": {"demo": {"pools": ["5h", "week", "month"]}},
                "calibration": {"granularity_pct": 1.0}}


def mk(ts, pools, input=0, cached=0, output=0, clean=True):
    return {"ts": float(ts), "agent": "t", "source": "watch",
            "usage": {"input": input, "cached": cached, "output": output, "requests": 1},
            "pools": pools, "clean": clean}


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
                if line and not line.startswith(("#", "账本", "-", " ", "先跑"))
                and "|" not in line and "---" not in line
                and line.split()[0] in ("5h", "week", "month")}

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

    def test_cli_nonpositive_window_is_error(self):
        rc, _, err = self.run_cli(["status", "--ledger", str(self.ledger),
                                   "--window-hours", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("必须为正数", err)


if __name__ == "__main__":
    unittest.main()
