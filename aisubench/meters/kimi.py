from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterator

from .base import Usage


def _iter_jsonl(path: Path, start_offset: int) -> Iterator[tuple[dict[str, Any] | None, int]]:
    """按行读取 JSONL，产出 ``(记录或 None, 该行结束后的字节偏移)``。

    二进制读取 + ``errors="replace"``，坏行产出 None 而不 interrupt。
    """
    with path.open("rb") as handle:
        handle.seek(start_offset)
        position = start_offset
        for raw in handle:
            position += len(raw)
            text = raw.decode("utf-8", errors="replace").strip()
            record: dict[str, Any] | None = None
            if text:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    record = parsed
            yield record, position


def _find_usage(record: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("usage", "token_usage", "tokenUsage"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    for key in ("message", "data", "result"):
        value = record.get(key)
        if isinstance(value, dict):
            nested = _find_usage(value)
            if nested is not None:
                return nested
    if any(key in record for key in ("input_tokens", "output_tokens", "prompt_tokens",
                                     "completion_tokens", "inputOther")):
        return record
    return None


def _int(value: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in value and value[key] is not None:
            try:
                return max(0, int(value[key]))
            except (TypeError, ValueError):
                continue
    return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cached_tokens(value: dict[str, Any]) -> int:
    direct = _int(value, "cached_tokens", "cached", "cache_read_input_tokens", "inputCacheRead")
    if direct:
        return direct
    details = value.get("prompt_tokens_details")
    if isinstance(details, dict):
        return _int(details, "cached_tokens")
    return 0


def _timestamp(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) > 1e11:
            seconds /= 1000.0
        return seconds
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _kimi_input(value: dict[str, Any]) -> int:
    direct = _int(value, "input", "input_tokens", "prompt_tokens")
    if direct:
        return direct
    return _int(value, "inputOther") + _int(value, "inputCacheCreation")


def parse_wire_jsonl(path: str | Path, start_offset: int = 0) -> Usage:
    usage, _ = read_wire_jsonl(path, start_offset)
    return usage


def read_wire_jsonl(path: str | Path, start_offset: int = 0) -> tuple[Usage, int]:
    """解析 ``start_offset`` 之后的 Kimi Code wire.jsonl，返回 (usage, 结束偏移)。

    真实记录形如::

        {"type":"usage.record","usage":{"inputOther":..,"output":..,
         "inputCacheRead":..,"inputCacheCreation":..},"time":<epoch 毫秒>}
    """
    usage = Usage()
    file_path = Path(path)
    if not file_path.exists():
        return usage, start_offset
    end = start_offset
    for record, end in _iter_jsonl(file_path, start_offset):
        if record is None:
            continue
        value = _find_usage(record)
        if value is None:
            continue
        moment = _timestamp(record.get("time", record.get("timestamp")))
        usage.add(Usage(
            input=_kimi_input(value),
            output=_int(value, "output", "output_tokens", "completion_tokens"),
            cached=_cached_tokens(value),
            requests=1,
            first_token_ts=moment,
            last_token_ts=moment,
            wall_time=_float(record.get("wall_time")),
        ))
    return usage, end


class KimiMeter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.offset = 0

    def start(self, **kwargs: Any) -> None:
        self.offset = _file_size(self.path)

    def collect(self, **kwargs: Any) -> Usage:
        if not self.path.exists():
            self.offset = 0
            return Usage()
        if self.path.stat().st_size < self.offset:
            self.offset = 0
        usage, end = read_wire_jsonl(self.path, self.offset)
        self.offset = end
        return usage
