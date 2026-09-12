"""shells/windows 单测：惰性导入守卫、viewmodel 球面视图、位置记忆读写。

本机无 PySide6 / 无显示器：断言 ``import shells.windows.ball`` 不炸、
``main`` 打印中文友好提示并以退出码 1 结束；``ball_view`` 与
``load/save_ball_position`` 为纯逻辑可直接测。同时断言 macOS 壳与
Windows 壳的采样实现同为 ``shells.shared.sampler``。
"""

from __future__ import annotations

import io
import py_compile
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import shells.windows.ball as ball
from shells.shared import viewmodel as vm
from shells.shared.sampler import Sampler, build_session

WINDOWS_DIR = Path(ball.__file__).resolve().parent


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


class LazyImportTests(unittest.TestCase):
    def test_module_importable_without_pyside6(self):
        if ball.QtWidgets is None:
            self.assertIsNone(ball.BallShell)
            self.assertIsNone(ball.BallWindow)
            self.assertIsNone(ball.DetailPanel)

    def test_all_windows_files_py_compile(self):
        for path in sorted(WINDOWS_DIR.glob("*.py")):
            with self.subTest(path=path.name):
                py_compile.compile(str(path), doraise=True)

    def test_main_friendly_exit_without_pyside6(self):
        if ball.QtWidgets is not None:
            self.skipTest("本机已装 PySide6")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = ball.main(["--no-sample"])
        self.assertEqual(rc, 1)
        self.assertIn("pip install -r shells/windows/requirements.txt",
                      err.getvalue())

    def test_module_entry_friendly_exit(self):
        if ball.QtWidgets is not None:
            self.skipTest("本机已装 PySide6")
        proc = subprocess.run(
            [sys.executable, "-m", "shells.windows", "--no-sample"],
            capture_output=True, text=True, cwd=Path(ball.__file__).parents[2])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("悬浮球壳不可用", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_parser_defaults(self):
        args = ball.build_parser().parse_args(["--no-sample"])
        self.assertEqual(args.interval, 300.0)
        self.assertEqual(args.refresh, 30.0)
        self.assertTrue(args.no_sample)


class SharedSamplerTests(unittest.TestCase):
    def test_windows_sampler_reexports_shared(self):
        import shells.windows.sampler as win_sampler
        self.assertIs(win_sampler.Sampler, Sampler)
        self.assertIs(win_sampler.build_session, build_session)

    def test_macos_app_uses_shared_sampler(self):
        import shells.macos.app as macos_app
        self.assertIs(macos_app.Sampler, Sampler)
        self.assertIs(macos_app.build_session, build_session)

    def test_sampler_rejects_nonpositive_interval(self):
        with self.assertRaises(ValueError):
            Sampler(session=None, interval=0)


class BallViewTests(unittest.TestCase):
    def test_ball_picks_min_eta_pool(self):
        st = status([pool("5h", current_pct=46.065, eta_hours=56.4),
                     pool("week", current_pct=80.0, eta_hours=400.0)])
        self.assertEqual(vm.ball_view(st), {"text": "46%", "level": "normal"})

    def test_ball_without_eta_picks_max_pct(self):
        st = status([pool("5h", current_pct=46.0), pool("week", current_pct=80.0)])
        self.assertEqual(vm.ball_view(st)["text"], "80%")

    def test_ball_empty_status(self):
        self.assertEqual(vm.ball_view(None), {"text": "—", "level": "none"})
        self.assertEqual(vm.ball_view(status([])),
                         {"text": "—", "level": "none"})
        self.assertEqual(vm.ball_view(
            status([], n_samples=0, ledger_exists=False)),
            {"text": "—", "level": "none"})

    def test_ball_pct_none_shows_dashes(self):
        st = status([pool("5h", current_pct=None, eta_hours=1.5)])
        self.assertEqual(vm.ball_view(st), {"text": "--", "level": "critical"})

    def test_ball_level_follows_urgent_pool(self):
        st = status([pool("5h", eta_hours=1.9)])
        self.assertEqual(vm.ball_view(st)["level"], "critical")
        st = status([pool("5h", eta_hours=2.4)])
        self.assertEqual(vm.ball_view(st)["level"], "warn")
        st = status([pool("5h", eta_hours=3.0)])
        self.assertEqual(vm.ball_view(st)["level"], "normal")

    def test_pool_rows_expose_pct(self):
        st = status([pool("5h", current_pct=46.5)])
        self.assertEqual(vm.pool_rows(st)[0]["pct"], 46.5)
        st = status([pool("month", has_samples=False, current_pct=None,
                          used_tokens=None, available=False, quota_tokens=None,
                          q_low=None, q_high=None, n_pairs=None, resets=None,
                          rate_tph=None, eta_hours=None)])
        self.assertIsNone(vm.pool_rows(st)[0]["pct"])


class PositionMemoryTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "ball_position.json"
            ball.save_ball_position(path, 123, -45)
            self.assertTrue(path.exists())
            self.assertEqual(ball.load_ball_position(path), (123, -45))

    def test_missing_or_corrupt_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ball_position.json"
            self.assertIsNone(ball.load_ball_position(path))
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(ball.load_ball_position(path))
            path.write_text("[1, 2]", encoding="utf-8")
            self.assertIsNone(ball.load_ball_position(path))
            path.write_text('{"x": "abc", "y": 0}', encoding="utf-8")
            self.assertIsNone(ball.load_ball_position(path))
            path.write_text('{"x": 10}', encoding="utf-8")
            self.assertIsNone(ball.load_ball_position(path))

    def test_default_position_file_under_state(self):
        self.assertEqual(ball.DEFAULT_POSITION_FILE.name, "ball_position.json")
        self.assertEqual(ball.DEFAULT_POSITION_FILE.parent.name, "state")


if __name__ == "__main__":
    unittest.main()
