"""进程内采样线程：daemon 循环复用 ``aisubench.watch`` 的 ``WatchSession``。

采样会话的构造口径与 ``aisubench gui`` 一致（meter/log_path、offsets、
clean 阈值都从 ``aisubench.toml`` 的 ``[watch]`` / ``[agents.*]`` 解析）。
周期循环与「立即采样」共用一把锁防重入；单轮异常打印警告后继续，不崩溃。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from aisubench.watch import (DEFAULT_CLEAN_WINDOW_SEC, OFFSETS_FILE, PROBES,
                             WatchSession, _resolve_meter, _resolve_path)


def build_session(agent: str, probe: str, config: dict,
                  ledger_path: str | Path) -> WatchSession:
    """按 watch 的解析口径构造采样会话（与 ``aisubench.gui._make_session`` 同口径）。"""
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
                                             DEFAULT_CLEAN_WINDOW_SEC)),
        subscription=agent_settings.get("subscription"))


class Sampler:
    """周期采样守护线程 + 带锁单次采样（防重入）。

    ``start()`` 起 daemon 线程每 ``interval`` 秒采样一次；``sample_now()``
    供菜单「立即采样」调用，在后台短线程执行、不阻塞 UI 主线程，采样进行中
    重复触发会被忽略。
    """

    def __init__(self, session: WatchSession, interval: float):
        if interval <= 0:
            raise ValueError("采样间隔必须为正数")
        self.session = session
        self.interval = float(interval)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="aisubench-sampler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def sample_now(self) -> bool:
        """后台触发一次采样；已有采样在进行时返回 False（不重入）。"""
        if self._lock.locked():
            return False
        threading.Thread(target=self._sample_guarded, daemon=True).start()
        return True

    def _sample_guarded(self) -> None:
        with self._lock:
            try:
                self.session.sample_once()
            except Exception as exc:
                print(f"警告：本轮采样失败，跳过并继续：{exc}", file=sys.stderr)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample_guarded()
            self._stop.wait(self.interval)
