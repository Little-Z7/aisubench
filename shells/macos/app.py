"""macOS 状态栏壳：rumps.App 菜单栏应用 + 进程内采样，纯原生、不开端口。

    python3 -m shells.macos --agent mock --probe mock [--interval 300] [--refresh 30]
    python3 -m shells.macos --no-sample          # 不采样，只读账本

硬性约束：无浏览器跳转、无 WebView、无本地 HTTP 服务；取数靠进程内
``aisubench.status.collect_status``，采样靠进程内 daemon 线程
（``shells.shared.sampler`` 复用 watch 的 ``WatchSession``，与 Windows 壳共用）。

rumps 只在 macOS 上可用，故做惰性导入：缺 rumps 或非 darwin 时本模块仍可
import / py_compile（Linux CI 不炸），``main`` 打印中文提示后以非零码退出。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aisubench.config import load_config
from aisubench.ledger import DEFAULT_LEDGER
from aisubench.status import collect_status
from aisubench.watch import _resolve_path

from shells.shared import viewmodel as vm

from shells.shared.sampler import Sampler, build_session

try:
    import rumps
except ImportError:  # Linux 或未安装 rumps：模块保持可 import
    rumps = None

DEFAULT_INTERVAL_SEC = 300.0
DEFAULT_REFRESH_SEC = 30.0
MISSING_HINT = "需要 macOS 且 pip install -r shells/macos/requirements.txt"

# level → 菜单行前缀标记（文本符号，不用 emoji）。
_LEVEL_PREFIX = {"warn": "! ", "critical": "!! "}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m shells.macos",
        description="AISUBench macOS 状态栏监控（纯原生壳：无浏览器/WebView/HTTP）")
    parser.add_argument("--agent",
                        help="agent 名，取 [agents.*] 的 meter/log_path（mock 用内置合成日志）")
    parser.add_argument("--probe", choices=["mock", "arkcli", "manual"], help="额度探针")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                        help="进程内采样间隔秒数（默认 %(default)g）")
    parser.add_argument("--refresh", type=float, default=DEFAULT_REFRESH_SEC,
                        help="菜单栏刷新间隔秒数（默认 %(default)g）")
    parser.add_argument("--no-sample", dest="no_sample", action="store_true",
                        help="不启用采样，只读账本（「立即采样」置灰）")
    parser.add_argument("--ledger", help="账本 JSONL 路径（默认取 [watch].ledger）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if rumps is None or sys.platform != "darwin":
        print(f"AISUBench 状态栏壳不可用：{MISSING_HINT}", file=sys.stderr)
        return 1
    if args.refresh <= 0 or args.interval <= 0:
        print("错误：--refresh 与 --interval 必须为正数", file=sys.stderr)
        return 2
    config = load_config()
    watch_cfg = config.get("watch") or {}
    ledger_path = _resolve_path(args.ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)

    sampler = None
    if not args.no_sample:
        if args.agent or args.probe:
            if not (args.agent and args.probe):
                print("错误：--agent 与 --probe 需同时提供才能启用采样", file=sys.stderr)
                return 2
            try:
                session = build_session(args.agent, args.probe, config, ledger_path)
            except ValueError as exc:
                print(f"错误：{exc}", file=sys.stderr)
                return 2
            sampler = Sampler(session, args.interval)

    app = StatusBarApp(config=config, ledger_path=ledger_path,
                       sampler=sampler, refresh_sec=args.refresh)
    if sampler is not None:
        sampler.start()
        print(f"采样：agent={args.agent} probe={args.probe} "
              f"每 {args.interval:g}s → {ledger_path}")
    try:
        app.run()
    finally:
        if sampler is not None:
            sampler.stop()
    return 0


if rumps is not None:

    class StatusBarApp(rumps.App):
        """状态栏应用：Timer 每 refresh 秒用 collect_status + viewmodel 重建菜单。"""

        def __init__(self, *, config: dict, ledger_path: Path,
                     sampler: Sampler | None, refresh_sec: float):
            super().__init__("AISUBench", title=vm.APP_TITLE, quit_button=None)
            self._config = config
            self._ledger_path = Path(ledger_path)
            self._sampler = sampler
            self._timer = rumps.Timer(self._on_tick, float(refresh_sec))
            self._rebuild_menu()
            self._timer.start()

        def _on_tick(self, _timer) -> None:
            self._rebuild_menu()

        def _on_sample(self, _sender) -> None:
            if self._sampler is not None and not self._sampler.sample_now():
                message = "上一次采样尚未结束，已忽略本次触发。"
                try:
                    rumps.notification("AISUBench", "采样进行中", message)
                except Exception:
                    print(f"提示：{message}", file=sys.stderr)

        def _on_refresh(self, _sender) -> None:
            self._rebuild_menu()

        def _on_quit(self, _sender) -> None:
            rumps.quit_application()

        def _collect(self) -> dict | None:
            try:
                return collect_status(self._config, ledger=self._ledger_path)
            except Exception as exc:
                print(f"警告：读取状态失败：{exc}", file=sys.stderr)
                return None

        def _rebuild_menu(self) -> None:
            status = self._collect()
            items: list = []
            if status is None:
                self.title = vm.APP_TITLE
                items.append("读取账本失败（详见终端输出）")
            else:
                self.title = vm.title_text(status)
                items.extend(vm.header_rows(status))
                rows = vm.pool_rows(status)
                if rows:
                    items.append(None)
                    for row in rows:
                        items.append(_LEVEL_PREFIX.get(row["level"], "") + row["title"])
                        items.extend("　" + line
                                     for line in row["detail"].splitlines())
                items.append(None)
            sample_item = rumps.MenuItem("立即采样")
            if self._sampler is not None:
                sample_item.set_callback(self._on_sample)
            items.append(sample_item)
            items.append(rumps.MenuItem("重新读取", callback=self._on_refresh))
            items.append(rumps.MenuItem("退出", callback=self._on_quit))
            self.menu.clear()
            self.menu.update(items)

else:
    StatusBarApp = None
