"""订阅额度恢复提醒开关：``state/alerts.json`` 的读写（GUI 面板铃铛按钮持久化）。

文件结构 ``{"<订阅名>.<池名>": true}``——只存开启的开关，关闭即删键；
文件缺失/损坏时按空表处理，不抛异常。写回用临时文件 + rename 原子替换。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ROOT

ALERTS_FILE = ROOT / "state" / "alerts.json"


def load_alerts(path: str | Path) -> dict[str, bool]:
    """读取提醒开关表；文件缺失或损坏时返回空表。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): True for key, value in data.items() if value is True}


def save_alerts(path: str | Path, alerts: dict[str, Any]) -> None:
    """原子写回开关表（临时文件 + rename），父目录自动创建。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(file_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump({str(key): True for key, value in alerts.items() if value is True},
                  handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, file_path)


def set_alert(path: str | Path, key: str, enabled: bool) -> dict[str, bool]:
    """设置 ``<订阅>.<池>`` 开关并持久化；关闭即删键。返回最新开关表。"""
    alerts = load_alerts(path)
    if enabled:
        alerts[str(key)] = True
    else:
        alerts.pop(str(key), None)
    save_alerts(path, alerts)
    return alerts
