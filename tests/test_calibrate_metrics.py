from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aisubench.calibrate import calculate_calibration
from aisubench.metrics import task_metrics, total_tokens, tps_gen, tps_wall
from aisubench.quota.mock import MockQuotaProbe
from aisubench.report import load_results, render_report


class IntervalMathTests(unittest.TestCase):
    """±g 区间数学：Δ 为两次读数之差，每次读数误差 ±g/2，Δ 最坏误差 ±g。

    手算基准：S=1000, Δ=4, g=1 →
    R = 250；R_low = 1000/5 = 200；R_high = 1000/3 ≈ 333.333。
    Q = R×100。
    """

    def test_interval_formula(self):
        rows = calculate_calibration({"5h": 10}, {"5h": 14}, 1000, 1)
        self.assertEqual(len(rows), 1)
        window = rows[0]
        self.assertAlmostEqual(window.delta_pct, 4.0)
        self.assertAlmostEqual(window.tokens_per_pct, 250.0)
        self.assertAlmostEqual(window.quota_tokens, 25000.0)
        self.assertAlmostEqual(window.lower, 200.0)
        self.assertAlmostEqual(window.upper, 1000.0 / 3.0)
        self.assertAlmostEqual(window.q_lower, 20000.0)
        self.assertAlmostEqual(window.q_upper, 100000.0 / 3.0)
        self.assertTrue(window.available)

    def test_upper_unavailable_when_delta_le_granularity(self):
        rows = calculate_calibration({"5h": 10}, {"5h": 11}, 1000, 1)
        self.assertEqual(rows[0].upper, None)
        self.assertEqual(rows[0].q_upper, None)
        self.assertTrue(rows[0].available)

    def test_tokens_zero_window_unavailable(self):
        rows = calculate_calibration({"5h": 10}, {"5h": 14}, 0, 1)
        self.assertEqual(len(rows), 1)
        window = rows[0]
        self.assertFalse(window.available)
        self.assertIsNone(window.tokens_per_pct)
        self.assertIsNone(window.quota_tokens)
        self.assertIsNone(window.lower)
        self.assertIsNone(window.upper)

    def test_nonpositive_delta_skipped(self):
        rows = calculate_calibration({"a": 10, "b": 10}, {"a": 10, "b": 9}, 1000, 1)
        self.assertEqual([row.name for row in rows], [])


class UsageAccountingTests(unittest.TestCase):
    def test_total_tokens_counts_cached_and_input(self):
        # cache_creation 已计入 input，cached 是缓存命中，total = input+cached+output。
        usage = {"input": 100, "cached": 20, "output": 30}
        self.assertEqual(total_tokens(usage), 150)

    def test_total_tokens_fallback_to_total(self):
        self.assertEqual(total_tokens({"total": 88}), 88)

    def test_task_metrics_cost(self):
        usage = {"input": 100, "cached": 10, "output": 40}
        results = [{"usage": usage, "converged": True},
                   {"usage": usage, "converged": False}]
        summary = task_metrics(results, price=30.0, quota_tokens=10000.0)
        self.assertEqual(summary["total_tokens"], 300)
        self.assertAlmostEqual(summary["tokens_per_task"], 150.0)
        self.assertAlmostEqual(summary["tokens_per_passed_task"], 150.0)
        # 元/task = 150 × 30 / 10000 = 0.45
        self.assertAlmostEqual(summary["yuan_per_task"], 0.45)
        # 元/有效任务 = 全部 300 / 通过 1 × 30/10000 = 0.9
        self.assertAlmostEqual(summary["yuan_per_effective_task"], 0.9)

    def test_tokens_per_passed_task_unavailable_without_passes(self):
        results = [{"usage": {"input": 5}, "converged": False}]
        summary = task_metrics(results)
        self.assertIsNone(summary["tokens_per_passed_task"])

    def test_empty_results_unavailable(self):
        summary = task_metrics([])
        self.assertIsNone(summary["pass_rate"])
        self.assertIsNone(summary["tokens_per_task"])
        self.assertIsNone(summary["tokens_per_passed_task"])
        self.assertNotIn("yuan_per_task", task_metrics([], price=1.0, quota_tokens=0))


class TpsGuardTests(unittest.TestCase):
    def test_mixed_dimension_timestamps_is_zero(self):
        usage = {"input": 0, "output": 100,
                 "first_token_ts": "2026-09-11T08:00:00", "last_token_ts": 8.5}
        self.assertEqual(tps_gen(usage), 0.0)

    def test_mixed_dimensions_reversed_still_zero(self):
        usage = {"input": 0, "output": 100,
                 "first_token_ts": 1.0, "last_token_ts": "2026-09-11T08:00:00"}
        self.assertEqual(tps_gen(usage), 0.0)

    def test_consistent_epoch_seconds_ok(self):
        usage = {"input": 0, "output": 100, "first_token_ts": 1, "last_token_ts": 6}
        self.assertEqual(tps_gen(usage), 20.0)

    def test_consistent_iso_strings_ok(self):
        usage = {"input": 0, "output": 100,
                 "first_token_ts": "2026-09-11T08:00:01",
                 "last_token_ts": "2026-09-11T08:00:06"}
        self.assertEqual(tps_gen(usage), 20.0)

    def test_tps_wall_uses_result_level_wall_time(self):
        usage = {"input": 100, "cached": 10, "output": 40, "wall_time": 1.0}
        self.assertEqual(tps_wall(usage, wall_time=3.0), 50.0)


class ReportFilterTests(unittest.TestCase):
    def _make_runs(self, root: Path) -> None:
        for index, (agent, task) in enumerate(
                [("mock", "a"), ("kimi", "b"), ("mock", "c")]):
            run_dir = root / f"run-{index}"
            run_dir.mkdir()
            (run_dir / "result.json").write_text(json.dumps({
                "task_id": task, "agent": agent, "converged": True,
                "usage": {"input": 10, "output": 10, "total": 20},
                "wall_time": 2,
            }), encoding="utf-8")
            # 保证 mtime 严格递增（run-2 最新）。
            stamp = 1_000_000 + index * 100
            import os
            os.utime(run_dir, (stamp, stamp))

    def test_filter_agent_and_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_runs(root)
            self.assertEqual([item["task_id"] for item in load_results(root)],
                             ["c", "b", "a"])
            self.assertEqual([item["task_id"] for item in load_results(root, agent="mock")],
                             ["c", "a"])
            self.assertEqual([item["task_id"] for item in load_results(root, last=2)],
                             ["c", "b"])

    def test_render_report_notes_full_scope_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_runs(root)
            report = render_report(load_results(root, agent="kimi"),
                                   title="AISUBench 评测报告（汇总了 runs/ 下全部 3 个 run）",
                                   calibrations=[{
                                       "plan": "demo", "tasks": 1, "tokens": 0,
                                       "windows": [{"name": "5h", "delta_pct": 2.0,
                                                    "tokens": 0, "tokens_per_pct": None,
                                                    "quota_tokens": None, "lower": None,
                                                    "upper": None, "q_lower": None,
                                                    "q_upper": None, "available": False}],
                                   }])
            self.assertIn("全部 3 个 run", report)
            self.assertIn("不可用", report)

    def test_render_report_unavailable_without_passes(self):
        report = render_report([{"task_id": "x", "agent": "mock", "converged": False,
                                 "usage": {"input": 1}, "wall_time": 1}])
        self.assertIn("不可用", report)


class CalibrateProbeContractTests(unittest.TestCase):
    def test_probe_continuous_flags(self):
        self.assertTrue(MockQuotaProbe.continuous)
        from aisubench.quota.manual import ManualQuotaProbe
        from aisubench.quota.arkcli import ArkcliQuotaProbe
        self.assertFalse(ManualQuotaProbe.continuous)
        self.assertTrue(ArkcliQuotaProbe.continuous)

    def test_manual_probe_snaps_to_granularity(self):
        from aisubench.quota.manual import ManualQuotaProbe
        probe = ManualQuotaProbe(input_fn=lambda: "5h=12.4,week=20.6",
                                 granularity_pct=1.0)
        snapshot = probe.snapshot()
        self.assertEqual(snapshot, {"5h": 12.0, "week": 21.0})


if __name__ == "__main__":
    unittest.main()
