"""gui 面板测试：端口 0 起 ThreadingHTTPServer，urllib 验证路由，全程离线。"""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

from aisubench import gui as gui_module
from aisubench.gui import GuiState, build_server, run_gui
from aisubench.ledger import append_sample, load_samples
from aisubench.watch import WatchSession

CONFIG = {"subscriptions": {"demo": {"pools": ["5h"]}},
          "calibration": {"granularity_pct": 1.0},
          "watch": {"interval_sec": 300}}


def mk(ts, pools, input=0, cached=0, output=0, clean=True):
    return {"ts": float(ts), "agent": "t", "source": "watch",
            "usage": {"input": input, "cached": cached, "output": output, "requests": 1},
            "pools": pools, "clean": clean}


def kimi_line(input_other=10, output=5, cache_read=100, cache_creation=2) -> str:
    return json.dumps({
        "type": "usage.record", "agentId": "main", "model": "demo",
        "usage": {"inputOther": input_other, "output": output,
                  "inputCacheRead": cache_read, "inputCacheCreation": cache_creation},
        "time": 1789111388997,
    }) + "\n"


class DictProbe:
    continuous = True

    def __init__(self, pools=None):
        self.pools = pools if pools is not None else {"5h": 12.0}

    def snapshot(self):
        return dict(self.pools)


class BlockingProbe:
    """snapshot 阻塞到 release 事件，用于验证 POST /api/sample 的防重入锁。"""

    continuous = True

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def snapshot(self):
        self.entered.set()
        self.release.wait(timeout=10)
        return {"5h": 1.0}


class ServerFixture:
    """端口 0 起服务 + 守护线程 serve_forever；提供 urllib 便捷方法。"""

    def __init__(self, state):
        self.server = build_server(state, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        return urllib.request.urlopen(self.url(path), timeout=5)

    def post(self, path):
        request = urllib.request.Request(self.url(path), data=b"", method="POST")
        return urllib.request.urlopen(request, timeout=5)


class GuiServerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.jsonl"
        self.log = self.root / "wire.jsonl"
        self.offsets = self.root / "meter_offsets.json"

    def tearDown(self):
        self._tmp.cleanup()

    def make_state(self, session=None, **kwargs):
        return GuiState(config=CONFIG, ledger_path=self.ledger,
                        window_hours=24.0, session=session, **kwargs)

    def make_session(self, probe=None):
        return WatchSession(agent="t", meter_name="kimi", log_path=self.log,
                            probe=probe or DictProbe(), ledger_path=self.ledger,
                            offsets_path=self.offsets, clean_window_sec=600.0)

    def test_binds_localhost_only(self):
        with ServerFixture(self.make_state()) as fix:
            self.assertEqual(fix.server.server_address[0], "127.0.0.1")

    def test_index_returns_html(self):
        with ServerFixture(self.make_state()) as fix:
            response = fix.get("/")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            body = response.read().decode("utf-8")
            self.assertIn("<html", body)
            self.assertIn("AISUBench 监控面板", body)
            self.assertIn("/api/status", body)

    def test_api_status_synthetic_ledger(self):
        samples = [mk(0, {"5h": 0}),
                   mk(3600, {"5h": 1}, input=500),
                   mk(7200, {"5h": 3}, input=400, output=600),
                   mk(10800, {"5h": 6}, input=1000, cached=500)]
        for item in samples:
            append_sample(self.ledger, item)
        with ServerFixture(self.make_state()) as fix:
            response = fix.get("/api/status")
            self.assertEqual(response.status, 200)
            self.assertIn("application/json", response.headers["Content-Type"])
            data = json.loads(response.read().decode("utf-8"))
        # 与 CLI status 同口径：R=500、Q=50,000、速率 1,000 tok/h、ETA 47.0h
        self.assertEqual(data["n_samples"], 4)
        self.assertEqual(data["span_hours"], 3.0)
        self.assertEqual(data["window_hours"], 24.0)
        self.assertEqual(data["last_sample_ts"], 10800.0)
        self.assertFalse(data["sampling_enabled"])
        pool = data["pools"][0]
        self.assertEqual(pool["name"], "5h")
        self.assertEqual(pool["current_pct"], 6.0)
        self.assertEqual(pool["used_tokens"], 3000.0)
        self.assertEqual(pool["quota_tokens"], 50000.0)
        self.assertEqual(round(pool["q_low"]), 42857)
        self.assertEqual(pool["q_high"], 60000.0)
        self.assertEqual(pool["rate_tph"], 1000.0)
        self.assertEqual(pool["eta_hours"], 47.0)
        self.assertEqual(pool["n_pairs"], 3)
        self.assertEqual(pool["resets"], 0)

    def test_api_status_empty_ledger_guidance_state(self):
        with ServerFixture(self.make_state()) as fix:
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
        self.assertEqual(data["n_samples"], 0)
        self.assertIsNone(data["last_sample_ts"])
        self.assertFalse(data["stale"])
        # 配置池仍在（has_samples=False），前端按空账本态渲染引导提示
        self.assertEqual([p["name"] for p in data["pools"]], ["5h"])
        self.assertFalse(data["pools"][0]["has_samples"])

    def test_post_sample_triggers_watch_session(self):
        self.log.write_text(kimi_line(), encoding="utf-8")
        with ServerFixture(self.make_state(session=self.make_session())) as fix:
            response = fix.post("/api/sample")
            self.assertEqual(response.status, 200)
            body = json.loads(response.read().decode("utf-8"))
            self.assertTrue(body["ok"])
            self.assertEqual(body["sample"]["pools"], {"5h": 12.0})
        samples = load_samples(self.ledger)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["source"], "watch")

    def test_post_sample_without_session_is_403(self):
        with ServerFixture(self.make_state()) as fix:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                fix.post("/api/sample")
            self.assertEqual(ctx.exception.code, 403)
            body = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertFalse(body["ok"])

    def test_post_sample_reentrant_returns_409(self):
        self.log.write_text(kimi_line(), encoding="utf-8")
        probe = BlockingProbe()
        with ServerFixture(self.make_state(session=self.make_session(probe))) as fix:
            result = {}

            def first():
                result["first"] = fix.post("/api/sample").status

            thread = threading.Thread(target=first, daemon=True)
            thread.start()
            self.assertTrue(probe.entered.wait(timeout=5))
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                fix.post("/api/sample")
            self.assertEqual(ctx.exception.code, 409)
            probe.release.set()
            thread.join(timeout=10)
            self.assertEqual(result["first"], 200)
        self.assertEqual(len(load_samples(self.ledger)), 1)

    def test_unknown_path_404(self):
        with ServerFixture(self.make_state()) as fix:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                fix.get("/nope")
            self.assertEqual(ctx.exception.code, 404)


class RunGuiArgTests(unittest.TestCase):
    """run_gui 的参数校验：错误组合在起服务前抛 ValueError。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = {"agents": {},
                       "watch": {"offsets_file": str(self.root / "meter_offsets.json")}}

    def tearDown(self):
        self._tmp.cleanup()

    def run_gui(self, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return run_gui(ledger=self.root / "ledger.jsonl",
                           config=self.config, **kwargs)

    def test_agent_without_probe_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(agent="mock")

    def test_probe_without_agent_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(probe="mock")

    def test_sample_interval_without_agent_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(sample_interval=5)

    def test_nonpositive_interval_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(sample_interval=0, agent="mock", probe="mock")

    def test_nonpositive_window_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(window_hours=0)

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            self.run_gui(agent="ghost", probe="mock")


if __name__ == "__main__":
    unittest.main()
