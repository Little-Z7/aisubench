from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from aisubench.calibrate import calculate_calibration
from aisubench.meters.base import Usage
from aisubench.meters.claude import parse_claude_jsonl
from aisubench.meters.kimi import parse_wire_jsonl
from aisubench.meters.mock import MockMeter
from aisubench.quota.arkcli import parse_snapshot
from aisubench.quota.mock import MockQuotaProbe
from aisubench.metrics import task_metrics, tps_gen, tps_wall
from aisubench.report import render_report
from aisubench.task import TaskSpec, list_tasks
from aisubench.verify import run_verify


class TaskTests(unittest.TestCase):
    def test_load_and_tiers(self):
        tasks = list_tasks()
        self.assertEqual(len(tasks), 10)
        self.assertEqual({task.tier for task in tasks}, {"calibration", "light", "medium"})

    def test_invalid_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x"
            path.mkdir()
            (path / "task.toml").write_text('id="wrong"\ntier="light"\nprompt="x"\nverify="true"\ntimeout_sec=1\nmax_tokens=1\n')
            with self.assertRaises(ValueError):
                TaskSpec.load(path)


class VerifyTests(unittest.TestCase):
    def test_verify_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(run_verify("exit 0", directory).passed)
            self.assertFalse(run_verify("exit 2", directory).passed)


class MetricsTests(unittest.TestCase):
    def test_math(self):
        usage = {"input": 100, "output": 50, "first_token_ts": 1, "last_token_ts": 6, "wall_time": 10}
        self.assertEqual(tps_gen(usage), 10)
        self.assertEqual(tps_wall(usage), 15)
        summary = task_metrics([{"usage": usage, "converged": True}, {"usage": usage, "converged": False}], 30, 10000)
        self.assertEqual(summary["total_tokens"], 300)
        self.assertEqual(summary["passed"], 1)
        self.assertAlmostEqual(summary["yuan_per_effective_task"], 0.9)


class MeterTests(unittest.TestCase):
    def test_kimi_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(json.dumps({"usage": {"input": 2, "output": 3}}) + "\n")
            self.assertEqual(parse_wire_jsonl(path).total, 5)

    def test_claude_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text(json.dumps({"usage": {"input_tokens": 4, "output_tokens": 6, "cache_read_input_tokens": 2}}) + "\n")
            usage = parse_claude_jsonl(path)
            self.assertEqual(usage.total, 12)  # 新口径 total = input + cached + output
            self.assertEqual(usage.cached, 2)

    def test_usage_add(self):
        usage = Usage(input=1)
        usage.add(Usage(output=2))
        self.assertEqual(usage.total, 3)

    def test_mock_meter(self):
        meter = MockMeter(Usage(input=4, output=5))
        meter.start()
        self.assertEqual(meter.collect().total, 9)

    def test_log_meter_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            path.write_text(json.dumps({"usage": {"input": 1, "output": 1}}) + "\n")
            offset = path.stat().st_size
            path.write_text(path.read_text() + json.dumps({"usage": {"input": 2, "output": 3}}) + "\n")
            self.assertEqual(parse_wire_jsonl(path, offset).total, 5)


class QuotaTests(unittest.TestCase):
    def test_quota_parsers(self):
        self.assertEqual(parse_snapshot('{"windows": {"5h": {"used_pct": 12}}}'), {"5h": 12.0})
        self.assertEqual(parse_snapshot("5h: 12%\nweek=20"), {"5h": 12.0, "week": 20.0})
        probe = MockQuotaProbe()
        self.assertEqual(probe.snapshot()["5h"], 10.0)
        self.assertEqual(probe.snapshot()["5h"], 12.0)


class CalibrationTests(unittest.TestCase):
    def test_interval(self):
        rows = calculate_calibration({"5h": 10}, {"5h": 12}, 1000, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quota_tokens, 50000)
        self.assertLess(rows[0].lower, rows[0].tokens_per_pct)
        self.assertGreater(rows[0].upper, rows[0].tokens_per_pct)


class ReportTests(unittest.TestCase):
    def test_report(self):
        text = render_report([{"task_id": "x", "agent": "mock", "converged": True,
                               "usage": {"input": 1, "output": 2, "total": 3, "first_token_ts": 1, "last_token_ts": 2},
                               "wall_time": 1}])
        self.assertIn("AISUBench 评测报告", text)
        self.assertIn("x", text)


if __name__ == "__main__":
    unittest.main()
