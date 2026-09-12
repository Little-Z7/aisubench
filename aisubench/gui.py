"""gui 监控面板：只绑 127.0.0.1 的本地单页应用（标准库 http.server，无外部资源）。

    aisubench gui [--port 7788] [--window-hours 24] [--clean-only]
                  [--sample-interval N] [--agent X --probe Y] [--ledger PATH]
                  [--debug]

路由：
- ``GET /``            单页监控面板（``aisubench/dashboard.html``，vanilla JS
                       每 5 秒拉一次 ``/api/status``，无任何外部 CDN 资源）；
- ``GET /api/status``  ``status.collect_status`` 的结构化 JSON，与 CLI
                       ``status`` 同口径同数字；
- ``POST /api/sample`` 立即采样一次（复用 watch 的 ``WatchSession.sample_once``，
                       启动时需传 ``--agent/--probe``，否则返回 403 且按钮置灰；
                       与后台采样线程共用一把锁防并发重入，重入返回 409）；
- ``GET /debug``       仅 ``--debug`` 时开放（否则 404）：服务端渲染的调试页，
                       含状态栏预览（复用 ``shells/shared/viewmodel`` 的
                       title_text/pool_rows/header_rows，浏览器所见即菜单栏
                       菜单内容）、``collect_status`` 完整 JSON、账本末尾
                       10 条样本原文与 meter_offsets.json 内容。

``--sample-interval N`` 时 GUI 进程内起守护线程每 N 秒采样一次，单进程 =
采样 + 展示；不传则只读账本。服务只绑定 127.0.0.1，不暴露到局域网。
"""

from __future__ import annotations

import html
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
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
                 sample_interval_sec: float | None = None,
                 debug: bool = False):
        self.config = config
        self.ledger_path = Path(ledger_path)
        self.window_hours = float(window_hours)
        self.clean_only = bool(clean_only)
        self.session = session
        self.debug = bool(debug)
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
        status["debug_enabled"] = self.debug
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
        elif path == "/debug" and self.server.state.debug:
            self._send(200, "text/html; charset=utf-8",
                       _debug_page(self.server.state))
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


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _menubar_preview(status: dict) -> str:
    """状态栏预览：复用 shells/shared/viewmodel，渲染结果与 macOS 菜单栏一致。"""
    from shells.shared import viewmodel
    parts = [f'<div class="mb-line">菜单栏标题：<code>{_esc(viewmodel.title_text(status))}</code></div>',
             '<div class="mb-group">头部信息行</div>']
    parts.extend(f'<div class="mb-line">{_esc(line)}</div>'
                 for line in viewmodel.header_rows(status))
    parts.append('<div class="mb-group">池行</div>')
    rows = viewmodel.pool_rows(status)
    if not rows:
        parts.append('<div class="mb-line dim">（空账本，菜单栏无池行）</div>')
    for row in rows:
        level = str(row.get("level") or "normal")
        prefix = {"warn": "! ", "critical": "!! "}.get(level, "")
        detail = "<br>".join(_esc(line)
                             for line in str(row.get("detail") or "").splitlines())
        parts.append(
            f'<div class="mb-pool {level}">'
            f'<div class="mb-line">{_esc(prefix + str(row.get("title") or ""))}'
            f'<span class="lvl">{_esc(level)}</span></div>'
            f'<div class="mb-detail">{detail}</div></div>')
    return "\n".join(parts)


def _debug_page(state: GuiState) -> bytes:
    """服务端渲染 /debug：刷新时间 + 状态栏预览 + 原始数据（无自动轮询）。"""
    status = state.status_payload()
    stamp = datetime.fromtimestamp(status["server_time"]).strftime(
        "%Y-%m-%d %H:%M:%S")
    try:
        preview = _menubar_preview(status)
    except Exception as exc:
        preview = f'<div class="mb-line warn">状态栏预览不可用：{_esc(exc)}</div>'

    status_json = _esc(json.dumps(status, ensure_ascii=False, indent=2))
    try:
        lines = state.ledger_path.read_text(
            encoding="utf-8", errors="replace").splitlines()
        tail = lines[-10:]
        ledger_text = _esc("\n".join(tail)) if tail else "（账本为空）"
    except OSError:
        ledger_text = f"（账本文件不存在：{_esc(state.ledger_path)}）"

    if state.session is not None:
        offsets_path = state.session.offsets_path
    else:
        watch_cfg = state.config.get("watch") or {}
        offsets_path = _resolve_path(watch_cfg.get("offsets_file"), OFFSETS_FILE)
    try:
        offsets_text = _esc(offsets_path.read_text(
            encoding="utf-8", errors="replace").strip() or "（空文件）")
    except OSError:
        offsets_text = f"（meter_offsets.json 不存在：{_esc(offsets_path)}）"

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AISUBench 调试模式</title>
<style>
:root {{
  --bg: #0d1117; --panel: #161b22; --border: #30363d;
  --fg: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
  --ok: #3fb950; --warn: #d29922; --bad: #f85149;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 16px; }}
header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
h1 {{ font-size: 18px; margin: 0; font-weight: 600; }}
.sub {{ color: var(--dim); font-size: 12px; margin-top: 2px; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-top: 12px; }}
.card h2 {{ font-size: 14px; margin: 0 0 8px; font-weight: 600; }}
.menubar {{ font: 13px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.mb-line {{ padding: 2px 8px; }}
.mb-line code {{ color: var(--accent); background: #21262d; padding: 1px 6px; border-radius: 4px; }}
.mb-group {{ color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; padding: 8px 8px 2px; border-top: 1px solid var(--border); margin-top: 6px; }}
.mb-group:first-of-type {{ border-top: 0; }}
.mb-pool {{ margin: 4px 8px; padding-left: 8px; border-left: 3px solid var(--border); }}
.mb-pool.warn {{ border-left-color: var(--warn); }}
.mb-pool.critical {{ border-left-color: var(--bad); }}
.mb-detail {{ color: var(--dim); padding: 0 8px 4px; }}
.lvl {{ font-size: 11px; color: var(--dim); margin-left: 8px; }}
.mb-pool.warn .lvl {{ color: var(--warn); }}
.mb-pool.critical .lvl {{ color: var(--bad); }}
.dim {{ color: var(--dim); }}
.warn {{ color: var(--warn); }}
pre {{
  margin: 8px 0 0; padding: 10px 12px; overflow-x: auto; font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: #0d1117; border: 1px solid var(--border); border-radius: 6px;
}}
pre .dim {{ color: var(--dim); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>AISUBench 调试模式</h1>
      <div class="sub">数据刷新时间：{stamp}（手动刷新页面更新，不自动轮询）</div>
    </div>
    <div><a href="/">← 返回监控面板</a></div>
  </header>
  <section class="card">
    <h2>状态栏预览（与 macOS 菜单栏菜单一致）</h2>
    <div class="menubar">
{preview}
    </div>
  </section>
  <section class="card">
    <h2>原始数据 · collect_status JSON</h2>
    <pre>{status_json}</pre>
  </section>
  <section class="card">
    <h2>原始数据 · 账本末尾 10 条样本（{_esc(state.ledger_path)}）</h2>
    <pre>{ledger_text}</pre>
  </section>
  <section class="card">
    <h2>原始数据 · meter_offsets.json</h2>
    <pre>{offsets_text}</pre>
  </section>
</div>
</body>
</html>
"""
    return page.encode("utf-8")


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
            ledger: str | Path | None = None, config: dict | None = None,
            debug: bool = False) -> int:
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
                     sample_interval_sec=freshness_interval, debug=debug)
    server = build_server(state, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"AISUBench 监控面板已启动：{url}（仅本机访问，Ctrl-C 退出）")
    if debug:
        print(f"调试模式已开启：{url}debug")
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
