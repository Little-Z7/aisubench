"""shells/shared/viewmodel 单测：直接构造 collect_status 形态的 dict，纯函数、离线。

覆盖：标题选最紧急池（有 ETA 取最小、无 ETA 取 pct 最大、空状态回退
AISUBench）、warn/critical 阈值、数据不足/暂无样本/不可用文案、空账本
引导、数据陈旧标记、未观测渠道提示；以及无 rumps 环境下
``import shells.macos.app`` 不抛异常。
"""

from __future__ import annotations

import io
import sys
import time
import unittest
from contextlib import redirect_stderr

from shells.shared import viewmodel as vm


def pool(name, **over):
    base = {"name": name, "has_samples": True, "current_pct": 10.0,
            "used_tokens": 1000.0, "tokens_per_pct": 100.0, "available": True,
            "quota_tokens": 10000.0, "q_low": 9000.0, "q_high": 11000.0,
            "n_pairs": 5, "resets": 0, "rate_tph": 500.0, "eta_hours": None}
    base.update(over)
    return base


def status(pools, **over):
    base = {"ledger_exists": True, "n_samples": 10, "span_hours": 2.0,
            "external_tokens": 0, "last_sample_ts": None, "pools": pools}
    base.update(over)
    return base


class TitleTests(unittest.TestCase):
    def test_title_picks_min_eta_pool(self):
        st = status([pool("5h", current_pct=46.065, eta_hours=56.4),
                     pool("week", current_pct=4.251, eta_hours=400.4)])
        self.assertEqual(vm.title_text(st), "5h 46%")

    def test_title_without_eta_picks_max_pct(self):
        st = status([pool("5h", current_pct=46.0), pool("week", current_pct=80.0)])
        self.assertEqual(vm.title_text(st), "week 80%")

    def test_title_empty_status_falls_back(self):
        self.assertEqual(vm.title_text(status([])), "AISUBench")
        self.assertEqual(vm.title_text(status([], n_samples=0,
                                              ledger_exists=False)), "AISUBench")
        self.assertEqual(vm.title_text(None), "AISUBench")


class PoolRowTests(unittest.TestCase):
    def test_warn_and_critical_thresholds(self):
        # 5h 池窗口一半 = 2.5h：eta=2.4 → warn；eta=1.9 < 2 → critical；eta=3.0 → normal。
        st = status([pool("5h", eta_hours=2.4)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "warn")
        st = status([pool("5h", eta_hours=1.9)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "critical")
        st = status([pool("5h", eta_hours=3.0)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "normal")

    def test_named_window_thresholds(self):
        # week 窗口一半 = 84h：eta=80 → warn；解析不了的池名按 24h（一半 12h）。
        st = status([pool("week", eta_hours=80.0)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "warn")
        st = status([pool("custom", eta_hours=11.0)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "warn")
        st = status([pool("custom", eta_hours=13.0)])
        self.assertEqual(vm.pool_rows(st)[0]["level"], "normal")

    def test_row_fields_and_numbers(self):
        st = status([pool("5h", current_pct=46.065, used_tokens=207292.0,
                          quota_tokens=449999.0, q_low=440438.0, q_high=459984.0,
                          rate_tph=4304.0, eta_hours=56.4)])
        row = vm.pool_rows(st)[0]
        self.assertEqual(row["title"], "5h · 已用 46%")
        self.assertIn("已用≈207,292", row["detail"])
        self.assertIn("Q 449,999（440,438–459,984）", row["detail"])
        self.assertIn("速率 4,304 tok/h", row["detail"])
        self.assertIn("ETA 56.4 h", row["detail"])
        self.assertEqual(row["level"], "normal")

    def test_available_false_wording(self):
        st = status([pool("5h", available=False, quota_tokens=None,
                          q_low=None, q_high=None, eta_hours=None)])
        detail = vm.pool_rows(st)[0]["detail"]
        self.assertIn("数据不足(Δ 未超粒度)", detail)

    def test_has_samples_false_wording(self):
        st = status([pool("month", has_samples=False, current_pct=None,
                          used_tokens=None, available=False, quota_tokens=None,
                          q_low=None, q_high=None, n_pairs=None, resets=None,
                          rate_tph=None, eta_hours=None)])
        row = vm.pool_rows(st)[0]
        self.assertIn("账本中暂无该池样本", row["detail"])
        self.assertEqual(row["level"], "normal")

    def test_none_fields_show_unavailable(self):
        st = status([pool("5h", current_pct=None, used_tokens=None,
                          rate_tph=None, eta_hours=None)])
        row = vm.pool_rows(st)[0]
        self.assertIn("不可用", row["title"])
        self.assertIn("不可用", row["detail"])

    def test_q_high_none_shows_lower_bound_only(self):
        st = status([pool("5h", q_high=None)])
        self.assertIn("≥", vm.pool_rows(st)[0]["detail"])

    def test_empty_ledger_returns_no_rows(self):
        self.assertEqual(vm.pool_rows(status([], n_samples=0)), [])
        self.assertEqual(vm.pool_rows(status([], ledger_exists=False)), [])
        self.assertEqual(vm.pool_rows(None), [])


class HeaderRowTests(unittest.TestCase):
    def test_empty_ledger_guidance(self):
        rows = vm.header_rows(status([], n_samples=0))
        self.assertTrue(any("python3 -m aisubench watch --once" in r for r in rows))
        rows = vm.header_rows(status([], ledger_exists=False))
        self.assertTrue(any("python3 -m aisubench watch --once" in r for r in rows))

    def test_normal_header_lines(self):
        now = time.time()
        st = status([], n_samples=289, span_hours=48.0, last_sample_ts=now - 60)
        rows = vm.header_rows(st, now=now)
        self.assertIn("样本 289", rows[0])
        self.assertIn("跨度 48.0 h", rows[0])
        self.assertIn("最后采样", rows[1])
        self.assertNotIn("数据陈旧", rows[1])

    def test_stale_marker_after_30_minutes(self):
        now = time.time()
        st = status([], last_sample_ts=now - 31 * 60)
        rows = vm.header_rows(st, now=now)
        self.assertIn("数据陈旧", rows[1])

    def test_external_tokens_hint(self):
        st = status([], external_tokens=2005)
        rows = vm.header_rows(st)
        self.assertTrue(any("2,005" in r and "未观测渠道" in r for r in rows))


class MacosImportTests(unittest.TestCase):
    def test_app_imports_without_rumps(self):
        # Linux / 未装 rumps：import 与中文提示路径不炸。
        import shells.macos.app as app
        if app.rumps is None or sys.platform != "darwin":
            self.assertIsNone(app.StatusBarApp)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = app.main(["--no-sample"])
            self.assertEqual(rc, 1)
            self.assertIn("pip install -r shells/macos/requirements.txt",
                          err.getvalue())


if __name__ == "__main__":
    unittest.main()
