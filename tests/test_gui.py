"""gui 面板测试：端口 0 起 ThreadingHTTPServer，urllib 验证路由，全程离线。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from aisubench import gui as gui_module
from aisubench.gui import GuiState, build_server, run_gui
from aisubench.ledger import append_sample, load_samples
from aisubench.runner import Runner
from aisubench.watch import WatchSession

CONFIG = {"subscriptions": {"demo": {"pools": ["5h"]}},
          "calibration": {"granularity_pct": 1.0},
          "watch": {"interval_sec": 300}}


def subs_by_name(data):
    return {sub["name"]: sub for sub in data.get("subscriptions") or []}


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

    def post(self, path, body=None):
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        headers = {} if body is None else {"Content-Type": "application/json"}
        request = urllib.request.Request(self.url(path), data=data,
                                         headers=headers, method="POST")
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

    def make_state(self, session=None, config=None, **kwargs):
        watch = {"alerts_file": str(self.root / "alerts.json"),
                 "offsets_file": str(self.offsets)}
        if config is not None:
            config = {**config, "watch": {**(config.get("watch") or {}), **watch}}
        else:
            config = {**CONFIG, "watch": {"interval_sec": 300, **watch}}
        return GuiState(config=config, ledger_path=self.ledger,
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
            # 顶部导航固定带「任务评测」入口（/debug 入口仍按 --debug 显隐）
            self.assertIn('href="/bench"', body)

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
        # 按订阅分组：mk() 样本无 subscription 字段 → 归 default；
        # 配置声明的 demo 订阅无样本但仍在分组里
        subs = subs_by_name(data)
        self.assertEqual(set(subs), {"demo", "default"})
        self.assertFalse(subs["demo"]["pools"][0]["has_samples"])
        pool = subs["default"]["pools"][0]
        self.assertEqual(pool["name"], "5h")
        self.assertEqual(pool["current_pct"], 6.0)
        self.assertEqual(pool["used_tokens"], 3000.0)
        self.assertEqual(pool["quota_tokens"], 50000.0)
        self.assertEqual(round(pool["q_low"]), 42857)
        self.assertEqual(pool["q_high"], 60000.0)
        self.assertEqual(pool["rate_tph"], 1000.0)
        self.assertEqual(pool["tps"], 0.28)  # 1,000 tok/h ÷ 3600，保留两位小数
        self.assertEqual(pool["eta_hours"], 47.0)
        self.assertEqual(pool["n_pairs"], 3)
        self.assertEqual(pool["resets"], 0)

    def test_api_status_tps_fields(self):
        # /api/status 的池与订阅 dict 都带 tps：池 = rate_tph ÷ 3600（两位小数），
        # 订阅级 = 各池 tps 非 None 最大值；无样本的 demo 订阅为 None。
        samples = [mk(0, {"5h": 0}),
                   mk(3600, {"5h": 1}, input=500),
                   mk(7200, {"5h": 3}, input=400, output=600),
                   mk(10800, {"5h": 6}, input=1000, cached=500)]
        for item in samples:
            append_sample(self.ledger, item)
        with ServerFixture(self.make_state()) as fix:
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
            html = fix.get("/").read().decode("utf-8")
        sub = subs_by_name(data)["default"]
        self.assertEqual(sub["pools"][0]["tps"], 0.28)
        self.assertEqual(sub["tps"], 0.28)
        demo = subs_by_name(data)["demo"]
        self.assertIn("tps", demo)
        self.assertIsNone(demo["tps"])
        self.assertIsNone(demo["pools"][0]["tps"])
        # 面板静态资源包含 TPS 渲染（池次级行 + 订阅汇总行）
        self.assertIn("TPS", html)

    def test_api_status_usage_totals_fields(self):
        # 订阅/全局 token 汇总字段：跨度累计 + 近窗口累计 + 拆分
        samples = [mk(0, {"5h": 0}),
                   mk(3600, {"5h": 1}, input=500),
                   mk(7200, {"5h": 3}, input=400, output=600),
                   mk(10800, {"5h": 6}, input=1000, cached=500)]
        for item in samples:
            append_sample(self.ledger, item)
        with ServerFixture(self.make_state()) as fix:
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
        sub = subs_by_name(data)["default"]
        self.assertEqual(sub["usage_totals"],
                         {"input": 1900, "cached": 500, "output": 600,
                          "requests": 4, "total": 3000})
        self.assertEqual(data["usage_totals"], sub["usage_totals"])
        # 24h 窗口（86400s）覆盖全部样本；无样本订阅为 None 而非假 0
        self.assertEqual(data["window_usage"]["total"], 3000)
        self.assertIsNone(subs_by_name(data)["demo"]["usage_totals"])

    def test_api_status_empty_ledger_guidance_state(self):
        with ServerFixture(self.make_state()) as fix:
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
        self.assertEqual(data["n_samples"], 0)
        self.assertIsNone(data["last_sample_ts"])
        self.assertFalse(data["stale"])
        self.assertEqual(data["alerts"], {})
        # 配置池仍在（has_samples=False），前端按空账本态渲染引导提示
        sub = subs_by_name(data)["demo"]
        self.assertEqual([p["name"] for p in sub["pools"]], ["5h"])
        self.assertFalse(sub["pools"][0]["has_samples"])
        self.assertEqual(sub["last_sample_rel"], "无样本")

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

    def test_debug_page_404_when_disabled(self):
        for item in (mk(0, {"5h": 0}), mk(3600, {"5h": 1}, input=500)):
            append_sample(self.ledger, item)
        with ServerFixture(self.make_state()) as fix:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                fix.get("/debug")
            self.assertEqual(ctx.exception.code, 404)
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
            self.assertFalse(data["debug_enabled"])

    def test_debug_page_renders_preview_and_raw_data(self):
        # 97% 已用 + 速率 750 tok/h → ETA < 2h，池行应为 critical 级
        samples = [mk(0, {"5h": 90}),
                   mk(3600, {"5h": 95}, input=500),
                   mk(7200, {"5h": 97}, input=400, output=600)]
        for item in samples:
            append_sample(self.ledger, item)
        self.offsets.write_text('{"x": 42}', encoding="utf-8")
        with ServerFixture(self.make_state(
                session=self.make_session(), debug=True)) as fix:
            response = fix.get("/debug")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            body = response.read().decode("utf-8")
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
            self.assertTrue(data["debug_enabled"])
        # 状态栏预览：菜单栏标题、池行（含级别标识）、头部信息行
        self.assertIn("状态栏预览", body)
        self.assertIn("菜单栏标题", body)
        self.assertIn("5h 97%", body)
        self.assertIn("5h · 已用 97%", body)
        self.assertIn("critical", body)
        self.assertIn("头部信息行", body)
        self.assertIn("样本 3 · 跨度 2.0 h", body)
        # 原始数据：完整 JSON、账本原文、offsets 内容
        self.assertIn('&quot;n_samples&quot;: 3', body)
        self.assertIn("&quot;pools&quot;: {&quot;5h&quot;: 97}", body)
        self.assertIn("42", body)
        self.assertIn("数据刷新时间", body)
        self.assertIn("返回监控面板", body)

    def test_debug_page_missing_files_hints(self):
        with ServerFixture(self.make_state(debug=True)) as fix:
            body = fix.get("/debug").read().decode("utf-8")
        self.assertIn("账本文件不存在", body)
        self.assertIn("meter_offsets.json 不存在", body)
        # 空账本时菜单栏无池行，头部行是 watch 引导
        self.assertIn("账本还没有样本", body)

    def test_unknown_path_404(self):
        with ServerFixture(self.make_state()) as fix:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                fix.get("/nope")
            self.assertEqual(ctx.exception.code, 404)


class BenchPageTests(unittest.TestCase):
    """GET /bench 任务评测页：与 CLI report 同口径的汇总卡 + 明细表。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runs = self.root / "runs"

    def tearDown(self):
        self._tmp.cleanup()

    def make_state(self, **kwargs):
        config = {**CONFIG, "watch": {"interval_sec": 300,
                                      "alerts_file": str(self.root / "alerts.json"),
                                      "offsets_file": str(self.root / "offsets.json")}}
        return GuiState(config=config, ledger_path=self.root / "ledger.jsonl",
                        window_hours=24.0, runs_dir=self.runs, **kwargs)

    def write_run(self, run_id, task_id, agent, converged, *, input=0, cached=0,
                  output=0, wall_time=1.0, first_ts=None, last_ts=None, mtime=None):
        run_dir = self.runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        result = {
            "run_id": run_id, "task_id": task_id, "tier": "light", "agent": agent,
            "started_at": "2026-09-12T00:00:00+00:00", "wall_time": wall_time,
            "verify": {"passed": converged}, "converged": converged,
            "usage": {"input": input, "cached": cached, "output": output,
                      "first_token_ts": first_ts, "last_token_ts": last_ts},
        }
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8")
        if mtime is not None:  # load_results 按目录 mtime 排序，显式固定
            os.utime(run_dir, (mtime, mtime))

    def test_bench_page_renders_summary_and_detail(self):
        # r1：total=100+20+50=170，tps_gen=50/(3-1)=25.00，tps_wall=170/8=21.25
        self.write_run("r1", "task-a", "mock", True, input=100, cached=20,
                       output=50, wall_time=8.0, first_ts=1.0, last_ts=3.0,
                       mtime=1000)
        # r2：total=200，无首末 token 时间戳 → tps_gen=0.00，tps_wall=200/4=50.00
        self.write_run("r2", "task-b", "other", False, input=200, wall_time=4.0,
                       mtime=2000)
        with ServerFixture(self.make_state()) as fix:
            response = fix.get("/bench")
            self.assertEqual(response.status, 200)
            self.assertIn("text/html", response.headers["Content-Type"])
            body = response.read().decode("utf-8")
        # 汇总卡：2 任务 / 通过 1 / 50.0% / 总 token 370 / 185.0 / 170.0
        self.assertIn("任务数", body)
        self.assertIn("通过率", body)
        self.assertIn("50.0%", body)
        self.assertIn("370", body)
        self.assertIn("185.0", body)
        self.assertIn("170.0", body)
        # 无价格参数 → 元/task 两行沿用「不可用」措辞并提示 CLI --price
        self.assertIn("元/task", body)
        self.assertIn("元/有效任务", body)
        self.assertIn("不可用", body)
        self.assertIn("--price", body)
        # 明细表：任务名/agent/结果/tokens/cached/TPS_gen/TPS_wall/耗时
        self.assertIn("task-a", body)
        self.assertIn("task-b", body)
        self.assertIn("mock", body)
        self.assertIn("other", body)
        self.assertIn("通过", body)
        self.assertIn("失败", body)
        self.assertIn("25.00", body)
        self.assertIn("21.25", body)
        self.assertIn("50.00", body)
        self.assertIn("8.00s", body)
        self.assertIn("4.00s", body)
        self.assertIn("TPS_gen", body)
        self.assertIn("TPS_wall", body)
        self.assertIn("cached", body)
        # 导航与返回链接
        self.assertIn("返回监控面板", body)

    def test_bench_page_empty_runs_guidance(self):
        with ServerFixture(self.make_state()) as fix:
            response = fix.get("/bench")
            self.assertEqual(response.status, 200)
            body = response.read().decode("utf-8")
        self.assertIn("python3 -m aisubench batch --tier light --agent mock", body)
        self.assertIn("返回监控面板", body)

    def test_bench_page_agent_filter(self):
        self.write_run("r1", "task-a", "mock", True, input=100, output=50,
                       mtime=1000)
        self.write_run("r2", "task-b", "other", False, input=200, mtime=2000)
        with ServerFixture(self.make_state()) as fix:
            body = fix.get("/bench?agent=mock").read().decode("utf-8")
        self.assertIn("task-a", body)
        self.assertNotIn("task-b", body)
        self.assertIn("agent=mock", body)
        self.assertIn("100.0%", body)
        # 过滤无匹配 → 空态文案；last=N 只保留最新 N 个 run
        with ServerFixture(self.make_state()) as fix:
            empty = fix.get("/bench?agent=ghost").read().decode("utf-8")
            last1 = fix.get("/bench?last=1").read().decode("utf-8")
        self.assertIn("没有匹配", empty)
        self.assertIn("task-b", last1)
        self.assertNotIn("task-a", last1)


class AlertApiTests(unittest.TestCase):
    """「额度恢复时提醒我」开关：POST /api/alerts 持久化到 alerts.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.jsonl"
        self.alerts_file = self.root / "alerts.json"

    def tearDown(self):
        self._tmp.cleanup()

    def make_state(self, **kwargs):
        config = {**CONFIG, "watch": {"interval_sec": 300,
                                      "alerts_file": str(self.alerts_file)}}
        return GuiState(config=config, ledger_path=self.ledger,
                        window_hours=24.0, **kwargs)

    def test_post_alerts_toggle_persists(self):
        with ServerFixture(self.make_state()) as fix:
            body = json.loads(
                fix.post("/api/alerts",
                         {"key": "demo.5h", "enabled": True}).read())
            self.assertTrue(body["ok"])
            self.assertEqual(body["alerts"], {"demo.5h": True})
            # 持久化到 alerts.json，且 /api/status 带出开关表
            self.assertEqual(json.loads(self.alerts_file.read_text()),
                             {"demo.5h": True})
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
            self.assertEqual(data["alerts"], {"demo.5h": True})
            # 关闭即删键
            body = json.loads(
                fix.post("/api/alerts",
                         {"key": "demo.5h", "enabled": False}).read())
            self.assertEqual(body["alerts"], {})
            self.assertEqual(json.loads(self.alerts_file.read_text()), {})

    def test_post_alerts_bad_body_400(self):
        with ServerFixture(self.make_state()) as fix:
            for body in ({"enabled": True}, {"key": "demo.5h"},
                         {"key": "", "enabled": True},
                         {"key": "demo.5h", "enabled": "yes"}, None):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    fix.post("/api/alerts", body)
                self.assertEqual(ctx.exception.code, 400, body)
            self.assertFalse(self.alerts_file.exists())

    def test_alerts_file_corrupt_degrades_to_empty(self):
        self.alerts_file.write_text("{broken", encoding="utf-8")
        with ServerFixture(self.make_state()) as fix:
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
        self.assertEqual(data["alerts"], {})

    def test_recovery_detection_flag_in_payload(self):
        # 最新读数 55→12 回落（重置/恢复）：prev_pct > current_pct 供前端判定
        for item in (mk(0, {"5h": 40}), mk(3600, {"5h": 55}, input=750),
                     mk(7200, {"5h": 12}, input=100)):
            append_sample(self.ledger, item)
        with ServerFixture(self.make_state()) as fix:
            fix.post("/api/alerts", {"key": "default.5h", "enabled": True})
            data = json.loads(fix.get("/api/status").read().decode("utf-8"))
        pool = subs_by_name(data)["default"]["pools"][0]
        self.assertEqual(pool["prev_pct"], 55.0)
        self.assertEqual(pool["current_pct"], 12.0)
        self.assertTrue(pool["current_pct"] < pool["prev_pct"])
        self.assertTrue(data["alerts"]["default.5h"])


CAL_CONFIG = {
    "subscriptions": {
        "demo": {"agent": "mock", "probe": "mock", "pools": ["5h"]},
        "noagent": {"probe": "mock", "pools": ["5h"]},
        "badagent": {"agent": "ghost", "probe": "mock", "pools": ["5h"]},
        "badprobe": {"agent": "mock", "probe": "nope", "pools": ["5h"]},
    },
    "calibration": {"max_tasks": 2, "granularity_pct": 1.0},
    "watch": {"interval_sec": 300},
}


class BlockingCalProbe:
    """首次 snapshot 阻塞到 release，用于 POST /api/calibrate 防重入测试。"""

    continuous = True
    last = None

    def __init__(self):
        BlockingCalProbe.last = self
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(timeout=10)
        return {"5h": 10.0 + self.calls}


class FailingCalProbe:
    continuous = True

    def snapshot(self):
        raise RuntimeError("探针爆炸")


class CalibrateApiTests(unittest.TestCase):
    """任务标定：POST /api/calibrate 后台标定 + GET 状态 + 结果并入 /api/status。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = self.root / "ledger.jsonl"
        self.reports = self.root / "reports"

    def tearDown(self):
        self._tmp.cleanup()

    def make_state(self, config=None, **kwargs):
        cfg = config or CAL_CONFIG
        cfg = {**cfg, "watch": {**(cfg.get("watch") or {}),
                                "alerts_file": str(self.root / "alerts.json")}}
        return GuiState(config=cfg, ledger_path=self.ledger, window_hours=24.0,
                        runner=Runner(runs_dir=self.root / "runs", config=cfg),
                        reports_dir=self.reports, **kwargs)

    def wait_done(self, fix, name="demo", timeout=30):
        deadline = time.time() + timeout
        while True:
            body = json.loads(
                fix.get(f"/api/calibrate?subscription={name}").read())
            if body["state"] != "running":
                return body
            self.assertLess(time.time(), deadline, "标定超时未完成")
            time.sleep(0.1)

    def test_calibrate_runs_to_done_and_merges_status(self):
        with ServerFixture(self.make_state()) as fix:
            response = fix.post("/api/calibrate", {"subscription": "demo"})
            self.assertEqual(response.status, 202)
            body = json.loads(response.read().decode("utf-8"))
            self.assertTrue(body["ok"])
            self.assertEqual(body["status"]["state"], "running")
            # 状态查询：无记录订阅为 idle
            idle = json.loads(
                fix.get("/api/calibrate?subscription=nobody").read())
            self.assertEqual(idle["state"], "idle")
            done = self.wait_done(fix)
            self.assertEqual(done["state"], "done")
            self.assertIsNone(done["error"])
            self.assertEqual(done["done_tasks"], 2)
            self.assertEqual(done["total_tasks"], 2)
            self.assertEqual(done["result"]["tasks"], 2)
            # 报告落盘 reports/calibration-demo.json
            report_path = self.reports / "calibration-demo.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["plan"], "demo")
            self.assertEqual(report["tasks"], 2)
            self.assertGreater(report["tokens"], 0)
            window = next(w for w in report["windows"] if w["name"] == "5h")
            # /api/status 池 dict 并入标定字段
            payload = json.loads(fix.get("/api/status").read().decode("utf-8"))
        pool = subs_by_name(payload)["demo"]["pools"][0]
        self.assertEqual(pool["calibrated_q"], window["quota_tokens"])
        self.assertEqual(pool["calibrated_at"], report_path.stat().st_mtime)

    def test_calibrate_reentrant_returns_409(self):
        config = {**CAL_CONFIG,
                  "subscriptions": {"demo": {"agent": "mock", "probe": "block",
                                             "pools": ["5h"]}}}
        with mock.patch.dict(gui_module.PROBES, {"block": BlockingCalProbe}):
            with ServerFixture(self.make_state(config)) as fix:
                response = fix.post("/api/calibrate", {"subscription": "demo"})
                self.assertEqual(response.status, 202)
                probe = BlockingCalProbe.last
                self.assertTrue(probe.entered.wait(timeout=5))
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    fix.post("/api/calibrate", {"subscription": "demo"})
                self.assertEqual(ctx.exception.code, 409)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"]["state"], "running")
                probe.release.set()
                self.assertEqual(self.wait_done(fix)["state"], "done")

    def test_calibrate_probe_failure_marks_error(self):
        config = {**CAL_CONFIG,
                  "subscriptions": {"demo": {"agent": "mock", "probe": "fail",
                                             "pools": ["5h"]}}}
        with mock.patch.dict(gui_module.PROBES, {"fail": FailingCalProbe}):
            with ServerFixture(self.make_state(config)) as fix:
                fix.post("/api/calibrate", {"subscription": "demo"})
                body = self.wait_done(fix)
                self.assertEqual(body["state"], "error")
                self.assertIn("探针爆炸", body["error"])

    def test_calibrate_bad_requests_400(self):
        with ServerFixture(self.make_state()) as fix:
            for body in ({"subscription": "ghost"},   # 未声明的订阅
                         {"subscription": "noagent"},  # 无 agent 绑定
                         {"subscription": "badagent"},  # agent 无可运行配置
                         {"subscription": "badprobe"},  # probe 未知
                         {}, None, {"subscription": "  "}):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    fix.post("/api/calibrate", body)
                self.assertEqual(ctx.exception.code, 400, body)
                payload = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertFalse(payload["ok"])
                self.assertTrue(payload["error"])


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
