"""gui 监控面板：只绑 127.0.0.1 的本地单页应用（标准库 http.server，无外部资源）。

    aisubench gui [--port 7788] [--window-hours 24] [--clean-only]
                  [--sample-interval N] [--agent X --probe Y] [--ledger PATH]

路由：
- ``GET /``            单页监控面板（``aisubench/dashboard.html``，vanilla JS
                       每 5 秒拉一次 ``/api/status``，无任何外部 CDN 资源）；
- ``GET /api/status``  ``status.collect_status`` 的结构化 JSON，与 CLI
                       ``status`` 同口径同数字；
- ``POST /api/sample`` 立即采样一次（复用 watch 的 ``WatchSession.sample_once``，
                       启动时需传 ``--agent/--probe``，否则返回 403 且按钮置灰；
                       与后台采样线程共用一把锁防并发重入，重入返回 409）。

``--sample-interval N`` 时 GUI 进程内起守护线程每 N 秒采样一次，单进程 =
采样 + 展示；不传则只读账本。服务只绑定 127.0.0.1，不暴露到局域网。
"""

from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load_config
from .ledger import DEFAULT_LEDGER
from .status import DEFAULT_WINDOW_HOURS, collect_status
from .watch import (DEFAULT_CLEAN_WINDOW_SEC, DEFAULT_INTERVAL_SEC, OFFSETS_FILE,
                    PROBES, WatchSession, _resolve_meter, _resolve_path)

DEFAULT_PORT = 7788
DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"


class GuiState:
    """GUI 共享状态：展示口径（config/账本/窗口/clean）+ 可选采样会话与防重入锁。"""

    def __init__(self, *, config: dict, ledger_path: str | Path,
                 window_hours: float, clean_only: bool = False,
                 session: WatchSession | None = None,
                 sample_interval_sec: float | None = None):
        self.config = config
        self.ledger_path = Path(ledger_path)
        self.window_hours = float(window_hours)
        self.clean_only = bool(clean_only)
        self.session = session
        # 数据新鲜度判定的参考间隔：--sample-interval 或 [watch].interval_sec。
        self.sample_interval_sec = sample_interval_sec
        self.sample_lock = threading.Lock()

    def status_payload(self) -> dict:
        """collect_status 结果 + 面板辅助字段（服务时间/采样开关/新鲜度）。"""
        status = collect_status(self.config, ledger=self.ledger_path,
                                window_hours=self.window_hours,
                                clean_only=self.clean_only)
        status["server_time"] = time.time()
        status["sampling_enabled"] = self.session is not None
        status["sample_interval_sec"] = self.sample_interval_sec
        last = status.get("last_sample_ts")
        status["stale"] = bool(
            last is not None and self.sample_interval_sec
            and status["server_time"] - last > 3 * self.sample_interval_sec)
        return status

    def try_sample(self) -> tuple[int, dict]:
        """立即采样一次。返回 (HTTP 状态码, 响应体)；带锁防并发重入。"""
        if self.session is None:
            return 403, {"ok": False, "error": "未配置 --agent/--probe，采样不可用"}
        if not self.sample_lock.acquire(blocking=False):
            return 409, {"ok": False, "error": "采样进行中，请稍候"}
        try:
            sample = self.session.sample_once()
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}
        finally:
            self.sample_lock.release()
        return 200, {"ok": True, "sample": sample}


class DashboardServer(ThreadingHTTPServer):
    """只绑 127.0.0.1 的线程化 HTTP 服务；``state`` 为 GuiState。"""

    daemon_threads = True

    def __init__(self, state: GuiState, port: int = DEFAULT_PORT):
        self.state = state
        super().__init__(("127.0.0.1", int(port)), _DashboardHandler)


def build_server(state: GuiState, port: int = DEFAULT_PORT) -> DashboardServer:
    return DashboardServer(state, port)


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AISUBenchGUI/0.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.read_bytes())
        elif path == "/api/status":
            self._send_json(200, self.server.state.status_payload())
        else:
            self._send_json(404, {"ok": False, "error": "未知路径"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/api/sample":
            self._send_json(404, {"ok": False, "error": "未知路径"})
            return
        code, body = self.server.state.try_sample()
        self._send_json(code, body)

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _make_session(agent: str, probe: str, config: dict,
                  ledger_path: Path) -> WatchSession:
    """按 watch 的解析口径构造采样会话（meter/log_path、offsets、clean 阈值）。"""
    if probe not in PROBES:
        raise ValueError(f"未知 probe: {probe}（可选 {' / '.join(sorted(PROBES))}）")
    watch_cfg = config.get("watch") or {}
    agent_settings = (config.get("agents") or {}).get(agent) or {}
    meter_name, log_path = _resolve_meter(agent, agent_settings)
    return WatchSession(
        agent=agent, meter_name=meter_name, log_path=log_path,
        probe=PROBES[probe](), ledger_path=ledger_path,
        offsets_path=_resolve_path(watch_cfg.get("offsets_file"), OFFSETS_FILE),
        clean_window_sec=float(watch_cfg.get("clean_window_sec",
                                             DEFAULT_CLEAN_WINDOW_SEC)))


def _sampler_loop(state: GuiState, interval: float, stop: threading.Event) -> None:
    """后台周期采样：瞬时异常只警告并继续（该轮不落账本，消耗并入下一轮）。"""
    while not stop.is_set():
        try:
            with state.sample_lock:
                state.session.sample_once()
        except Exception as exc:
            print(f"警告：本轮采样失败，跳过并继续：{exc}", file=sys.stderr)
        stop.wait(interval)


def run_gui(*, port: int = DEFAULT_PORT, window_hours: float | None = None,
            clean_only: bool = False, sample_interval: float | None = None,
            agent: str | None = None, probe: str | None = None,
            ledger: str | Path | None = None, config: dict | None = None) -> int:
    """gui 子命令主体。``config`` 可注入，便于测试；返回退出码。"""
    config = load_config() if config is None else config
    watch_cfg = config.get("watch") or {}
    ledger_path = _resolve_path(ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)
    window = DEFAULT_WINDOW_HOURS if window_hours is None else float(window_hours)
    if window <= 0:
        raise ValueError("--window-hours 必须为正数")

    session = None
    if agent or probe:
        if not agent or not probe:
            raise ValueError("--agent 与 --probe 需同时提供才能启用采样")
        session = _make_session(agent, probe, config, ledger_path)
    interval = None
    if sample_interval is not None:
        interval = float(sample_interval)
        if interval <= 0:
            raise ValueError("--sample-interval 必须为正数")
        if session is None:
            raise ValueError("--sample-interval 需要同时提供 --agent 与 --probe")
    freshness_interval = interval or float(
        watch_cfg.get("interval_sec", DEFAULT_INTERVAL_SEC))

    state = GuiState(config=config, ledger_path=ledger_path, window_hours=window,
                     clean_only=clean_only, session=session,
                     sample_interval_sec=freshness_interval)
    server = build_server(state, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"AISUBench 监控面板已启动：{url}（仅本机访问，Ctrl-C 退出）")
    if session is not None:
        hint = f"后台采样已开启，间隔 {interval:g}s" if interval is not None \
            else "手动采样已开启（页面「立即采样」按钮）"
        print(f"采样：agent={session.agent} probe 快照 → {ledger_path}；{hint}")
    try:
        webbrowser.open(url)  # 无头环境打开失败时静默跳过
    except Exception:
        pass

    stop = threading.Event()
    if session is not None and interval is not None:
        threading.Thread(target=_sampler_loop, args=(state, interval, stop),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("gui：收到 Ctrl-C，停止。", file=sys.stderr)
    finally:
        stop.set()
        server.server_close()
    return 0
