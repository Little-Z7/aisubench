"""Windows 悬浮球壳：PySide6 无边框置顶半透明圆球 + 原生绘制的详情面板。

    python3 -m shells.windows --agent mock --probe mock [--interval 300]
    python3 -m shells.windows --no-sample          # 不采样，只读账本

硬性约束：无浏览器、无 WebView、无本地 HTTP 服务；取数靠进程内
``aisubench.status.collect_status``，采样复用 ``shells.shared.sampler``
（与 macOS 壳同一实现）。球面显示最紧急池的已用%（``viewmodel.ball_view``），
左键点击展开/收起详情面板（每池进度条 / 已用≈tok / Q 估算 / 速率 / ETA /
告警色），左键拖拽移动并把位置记入 ``state/ball_position.json``，右键菜单：
立即采样 / 退出。

PySide6 为惰性导入：未安装时本模块仍可 import / py_compile（Linux CI 不炸），
``main`` 打印中文提示后以退出码 1 退出——与 macOS 壳同一模式。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from aisubench.config import ROOT, load_config
from aisubench.ledger import DEFAULT_LEDGER
from aisubench.status import collect_status
from aisubench.watch import _resolve_path

from shells.shared import viewmodel as vm

from .sampler import Sampler, build_session

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:  # 未安装 PySide6：模块保持可 import
    QtCore = QtGui = QtWidgets = None

DEFAULT_INTERVAL_SEC = 300.0
DEFAULT_REFRESH_SEC = 30.0
MISSING_HINT = "需要桌面环境且 pip install -r shells/windows/requirements.txt（PySide6）"
DEFAULT_POSITION_FILE = ROOT / "state" / "ball_position.json"

BALL_SIZE = 64
PANEL_WIDTH = 340
# 面板因失焦刚收起时，抑制同一次点击立即重新展开的窗口期（秒）。
PANEL_REOPEN_SUPPRESS_SEC = 0.35

# level → 球体底色（RGB）；level→ 面板标题色；pct → 进度条色（与 WebUI 同口径）。
_BALL_COLORS = {"normal": (52, 132, 220), "warn": (232, 165, 32),
                "critical": (214, 69, 65), "none": (105, 115, 125)}
_TITLE_COLORS = {"warn": (240, 180, 60), "critical": (235, 95, 90)}


def load_ball_position(path: str | Path) -> tuple[int, int] | None:
    """读位置记忆 JSON；文件缺失 / 损坏 / 字段非数字一律返回 None（用默认位）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    x, y = data.get("x"), data.get("y")
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def save_ball_position(path: str | Path, x: int, y: int) -> None:
    """把位置写入 JSON（先写临时文件再替换，避免半截文件）；目录缺失自动创建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"x": int(x), "y": int(y)}),
                   encoding="utf-8")
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m shells.windows",
        description="AISUBench Windows 桌面悬浮球监控（纯原生壳：无浏览器/WebView/HTTP）")
    parser.add_argument("--agent",
                        help="agent 名，取 [agents.*] 的 meter/log_path（mock 用内置合成日志）")
    parser.add_argument("--probe", choices=["mock", "arkcli", "manual"], help="额度探针")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                        help="进程内采样间隔秒数（默认 %(default)g）")
    parser.add_argument("--refresh", type=float, default=DEFAULT_REFRESH_SEC,
                        help="球面/面板刷新间隔秒数（默认 %(default)g）")
    parser.add_argument("--no-sample", dest="no_sample", action="store_true",
                        help="不启用采样，只读账本（右键「立即采样」置灰）")
    parser.add_argument("--ledger", help="账本 JSONL 路径（默认取 [watch].ledger）")
    parser.add_argument("--position-file",
                        help="球位置记忆文件（默认 state/ball_position.json）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if QtWidgets is None:
        print(f"AISUBench 悬浮球壳不可用：{MISSING_HINT}", file=sys.stderr)
        return 1
    if args.refresh <= 0 or args.interval <= 0:
        print("错误：--refresh 与 --interval 必须为正数", file=sys.stderr)
        return 2
    config = load_config()
    watch_cfg = config.get("watch") or {}
    ledger_path = _resolve_path(args.ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)
    position_path = _resolve_path(args.position_file, DEFAULT_POSITION_FILE)

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

    app = QtWidgets.QApplication(sys.argv[:1] or ["aisubench-ball"])
    app.setQuitOnLastWindowClosed(False)
    shell = BallShell(config=config, ledger_path=ledger_path, sampler=sampler,
                      refresh_sec=args.refresh, position_path=position_path)
    if sampler is not None:
        sampler.start()
        print(f"采样：agent={args.agent} probe={args.probe} "
              f"每 {args.interval:g}s → {ledger_path}")
    try:
        return int(app.exec())
    finally:
        if sampler is not None:
            sampler.stop()


if QtWidgets is not None:

    def _color(rgb: tuple[int, int, int], alpha: int = 255) -> QtGui.QColor:
        return QtGui.QColor(rgb[0], rgb[1], rgb[2], alpha)

    def _bar_color(pct: float | None) -> QtGui.QColor:
        """进度条色：与 WebUI 卡片同口径 <70 绿 / <90 黄 / ≥90 红，无读数灰。"""
        if pct is None:
            return _color((120, 128, 138))
        if pct >= 90:
            return _color((214, 69, 65))
        if pct >= 70:
            return _color((232, 165, 32))
        return _color((74, 173, 92))

    class BallWindow(QtWidgets.QWidget):
        """悬浮球本体：无边框置顶半透明圆球，左键点击/拖拽，右键菜单。"""

        clicked = QtCore.Signal()
        dragged = QtCore.Signal(int, int)
        sample_requested = QtCore.Signal()
        quit_requested = QtCore.Signal()

        def __init__(self, *, sampler_enabled: bool):
            super().__init__(None, QtCore.Qt.FramelessWindowHint
                             | QtCore.Qt.WindowStaysOnTopHint
                             | QtCore.Qt.Tool)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.setFixedSize(BALL_SIZE, BALL_SIZE)
            self.setToolTip(vm.APP_TITLE)
            self._sampler_enabled = sampler_enabled
            self._view = {"text": "—", "level": "none"}
            self._drag_origin: QtCore.QPoint | None = None
            self._dragged = False

        def set_view(self, view: dict) -> None:
            self._view = view
            self.update()

        def paintEvent(self, _event) -> None:
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            rect = self.rect().adjusted(4, 4, -4, -4)
            color = _color(_BALL_COLORS.get(self._view["level"],
                                            _BALL_COLORS["none"]), 212)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 90), 2))
            painter.setBrush(color)
            painter.drawEllipse(rect)
            painter.setPen(QtGui.QColor(255, 255, 255))
            font = painter.font()
            font.setBold(True)
            font.setPixelSize(max(13, rect.height() // 4))
            painter.setFont(font)
            painter.drawText(rect, QtCore.Qt.AlignCenter, self._view["text"])

        def mousePressEvent(self, event) -> None:
            if event.button() == QtCore.Qt.LeftButton:
                self._drag_origin = (event.globalPosition().toPoint()
                                     - self.frameGeometry().topLeft())
                self._dragged = False
            elif event.button() == QtCore.Qt.RightButton:
                self._context_menu(event.globalPosition().toPoint())

        def mouseMoveEvent(self, event) -> None:
            if self._drag_origin is not None \
                    and event.buttons() & QtCore.Qt.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_origin)
                self._dragged = True

        def mouseReleaseEvent(self, event) -> None:
            if event.button() != QtCore.Qt.LeftButton:
                return
            origin, self._drag_origin = self._drag_origin, None
            if origin is None:
                return
            if self._dragged:
                pos = self.frameGeometry().topLeft()
                self.dragged.emit(pos.x(), pos.y())
            else:
                self.clicked.emit()

        def _context_menu(self, global_pos: QtCore.QPoint) -> None:
            menu = QtWidgets.QMenu(self)
            sample = menu.addAction("立即采样")
            sample.setEnabled(self._sampler_enabled)
            quit_action = menu.addAction("退出")
            chosen = menu.exec(global_pos)
            if chosen is sample:
                self.sample_requested.emit()
            elif chosen is quit_action:
                self.quit_requested.emit()

    class DetailPanel(QtWidgets.QWidget):
        """详情面板：QPainter 原生绘制各池进度条与数字，失焦自动收起。

        用 ``Qt.Tool``（非 ``Qt.Popup``）+ 激活态变化（``ActivationChange``）
        收起，保证「点球收起」和「点面板外收起」语义一致；配合 BallShell
        的重开抑制窗口期，同一次点击不会收起后立刻重开。
        """

        PAD = 12
        BAR_H = 8
        ROW_GAP = 10

        def __init__(self):
            super().__init__(None, QtCore.Qt.Tool
                             | QtCore.Qt.FramelessWindowHint
                             | QtCore.Qt.WindowStaysOnTopHint)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.closed_at = 0.0
            self._armed = False
            normal = self.font()
            self._bold = QtGui.QFont(normal)
            self._bold.setBold(True)
            self._small = QtGui.QFont(normal)
            self._small.setPointSizeF(max(8.0, normal.pointSizeF() - 1.5))
            self._fm = QtGui.QFontMetrics(normal)
            self._fm_bold = QtGui.QFontMetrics(self._bold)
            self._fm_small = QtGui.QFontMetrics(self._small)
            self._ops: list[tuple] = []
            self.setFixedWidth(PANEL_WIDTH)

        def set_status(self, status: dict | None) -> None:
            """按 ``viewmodel`` 的行重排面板内容并刷新。"""
            header = (["读取账本失败（详见终端输出）"] if status is None
                      else vm.header_rows(status))
            rows = [] if status is None else vm.pool_rows(status)
            ops: list[tuple] = []
            y = self.PAD
            ops.append(("title", y, vm.APP_TITLE))
            y += self._fm_bold.height() + 4
            for line in header:
                ops.append(("dim", y, line))
                y += self._fm.height()
            for row in rows:
                y += self.ROW_GAP
                ops.append(("pool_title", y, row))
                y += self._fm_bold.height() + 4
                ops.append(("bar", y, row.get("pct")))
                y += self.BAR_H + 4
                for line in row["detail"].splitlines():
                    ops.append(("dim_small", y, line))
                    y += self._fm_small.height()
            self._ops = ops
            self.setFixedSize(PANEL_WIDTH, y + self.PAD)
            self.update()

        def popup_near(self, anchor: QtCore.QRect) -> None:
            """在球右侧弹出；右缘不够则放左侧，垂直方向压进屏幕内。"""
            screen = (QtGui.QGuiApplication.screenAt(anchor.center())
                      or QtGui.QGuiApplication.primaryScreen())
            geo = screen.availableGeometry()
            if anchor.right() + 8 + self.width() <= geo.right():
                x = anchor.right() + 8
            else:
                x = max(geo.left() + 4, anchor.left() - self.width() - 8)
            y = min(max(anchor.top(), geo.top() + 4),
                    geo.bottom() - self.height() - 4)
            self.move(x, y)
            self.show()
            self.raise_()
            self.activateWindow()

        def showEvent(self, event) -> None:
            self._armed = False
            super().showEvent(event)

        def changeEvent(self, event) -> None:
            # 激活过后再失焦才收起；若窗口系统始终不给激活位，面板保持
            # 显示、由球点击切换兜底，避免「弹出一瞬即被收起」。
            if event.type() == QtCore.QEvent.ActivationChange:
                if self.isActiveWindow():
                    self._armed = True
                elif self._armed:
                    self.hide()
            super().changeEvent(event)

        def hideEvent(self, event) -> None:
            self.closed_at = time.monotonic()
            super().hideEvent(event)

        def paintEvent(self, _event) -> None:
            painter = QtGui.QPainter(self)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QColor(28, 32, 40, 242))
            painter.drawRoundedRect(self.rect(), 10, 10)
            width = PANEL_WIDTH - self.PAD * 2
            for kind, y, payload in self._ops:
                if kind == "title":
                    painter.setFont(self._bold)
                    painter.setPen(QtGui.QColor(235, 240, 248))
                    painter.drawText(self.PAD, y + self._fm_bold.ascent(), payload)
                elif kind == "pool_title":
                    painter.setFont(self._bold)
                    painter.setPen(_color(_TITLE_COLORS.get(
                        payload["level"], (235, 240, 248))))
                    painter.drawText(self.PAD, y + self._fm_bold.ascent(),
                                     self._fm_bold.elidedText(
                                         payload["title"],
                                         QtCore.Qt.ElideRight, width))
                elif kind == "bar":
                    track = QtCore.QRectF(self.PAD, y, width, self.BAR_H)
                    painter.setPen(QtCore.Qt.NoPen)
                    painter.setBrush(QtGui.QColor(255, 255, 255, 36))
                    painter.drawRoundedRect(track, 4, 4)
                    pct = payload
                    if pct is not None:
                        fill = QtCore.QRectF(
                            self.PAD, y,
                            width * max(0.0, min(100.0, float(pct))) / 100.0,
                            self.BAR_H)
                        painter.setBrush(_bar_color(pct))
                        painter.drawRoundedRect(fill, 4, 4)
                elif kind == "dim":
                    painter.setFont(self.font())
                    painter.setPen(QtGui.QColor(168, 178, 190))
                    painter.drawText(self.PAD, y + self._fm.ascent(),
                                     self._fm.elidedText(
                                         payload, QtCore.Qt.ElideRight, width))
                elif kind == "dim_small":
                    painter.setFont(self._small)
                    painter.setPen(QtGui.QColor(168, 178, 190))
                    painter.drawText(self.PAD, y + self._fm_small.ascent(),
                                     self._fm_small.elidedText(
                                         payload, QtCore.Qt.ElideRight, width))

    class BallShell(QtCore.QObject):
        """壳装配：球窗口 + 详情面板 + 刷新定时器 + 位置记忆 + 采样器。"""

        def __init__(self, *, config: dict, ledger_path: Path,
                     sampler: Sampler | None, refresh_sec: float,
                     position_path: Path):
            super().__init__()
            self._config = config
            self._ledger_path = Path(ledger_path)
            self._sampler = sampler
            self._position_path = Path(position_path)
            self.ball = BallWindow(sampler_enabled=sampler is not None)
            self.panel = DetailPanel()
            self.ball.clicked.connect(self._toggle_panel)
            self.ball.dragged.connect(self._save_position)
            self.ball.sample_requested.connect(self._sample_now)
            self.ball.quit_requested.connect(QtWidgets.QApplication.quit)
            self._restore_position()
            self.ball.show()
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(int(refresh_sec * 1000))
            self.refresh()

        def refresh(self) -> None:
            status = self._collect()
            self.ball.set_view(vm.ball_view(status))
            if self.panel.isVisible():
                self.panel.set_status(status)

        def _toggle_panel(self) -> None:
            if self.panel.isVisible():
                self.panel.hide()
                return
            # 面板刚因失焦收起：本次点击就是收起它的那次，不再立即重开。
            if time.monotonic() - self.panel.closed_at < PANEL_REOPEN_SUPPRESS_SEC:
                return
            self.panel.set_status(self._collect())
            self.panel.popup_near(self.ball.geometry())

        def _sample_now(self) -> None:
            if self._sampler is None:
                return
            if not self._sampler.sample_now():
                QtWidgets.QToolTip.showText(
                    self.ball.mapToGlobal(self.ball.rect().center()),
                    "上一次采样尚未结束，已忽略本次触发。", self.ball)

        def _collect(self) -> dict | None:
            try:
                return collect_status(self._config, ledger=self._ledger_path)
            except Exception as exc:
                print(f"警告：读取状态失败：{exc}", file=sys.stderr)
                return None

        def _restore_position(self) -> None:
            pos = load_ball_position(self._position_path)
            if pos is not None and QtGui.QGuiApplication.screenAt(
                    QtCore.QPoint(*pos)) is not None:
                self.ball.move(*pos)
                return
            screen = QtGui.QGuiApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.ball.move(geo.right() - BALL_SIZE - 40,
                               geo.top() + geo.height() // 3)

        def _save_position(self, x: int, y: int) -> None:
            try:
                save_ball_position(self._position_path, x, y)
            except OSError as exc:
                print(f"警告：写入位置记忆失败：{exc}", file=sys.stderr)

else:
    BallWindow = DetailPanel = BallShell = None


if __name__ == "__main__":
    sys.exit(main())
