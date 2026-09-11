from __future__ import annotations

import json
import re
import subprocess


class ArkcliQuotaProbe:
    continuous = True

    def __init__(self, command: list[str] | None = None):
        self.command = command or ["arkcli", "usage", "plan", "--json"]

    def snapshot(self) -> dict[str, float]:
        completed = subprocess.run(self.command, text=True, capture_output=True, check=True)
        return parse_snapshot(completed.stdout)


def parse_snapshot(text: str) -> dict[str, float]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = None
    result: dict[str, float] = {}
    if isinstance(data, dict):
        values = data.get("windows", data.get("quota", data))
        _collect_json_windows(values, result)
        if result:
            return result
    for name, value in re.findall(
            r"([\w-]+)\s*(?:窗口)?\s*[:=：]\s*([0-9]+(?:\.[0-9]+)?)\s*%?", text):
        result[name] = float(value)
    return result


def _collect_json_windows(value: object, result: dict[str, float], name: str | None = None) -> None:
    if isinstance(value, dict):
        used = value.get("used_pct", value.get("used_percent", value.get("percent")))
        if used is not None and name:
            try:
                result[name] = float(str(used).rstrip("%"))
            except (TypeError, ValueError):
                pass
            return
        for key, item in value.items():
            _collect_json_windows(item, result, str(key))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                window = item.get("window", item.get("name", item.get("period")))
                _collect_json_windows(item, result, str(window) if window else name)
    elif name and value is not None:
        try:
            result[name] = float(str(value).rstrip("%"))
        except (TypeError, ValueError):
            pass
