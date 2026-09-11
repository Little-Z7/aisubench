from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Usage
from .kimi import (
    _cached_tokens,
    _file_size,
    _find_usage,
    _float,
    _int,
    _iter_jsonl,
    _timestamp,
)


def parse_claude_jsonl(path: str | Path, start_offset: int = 0,
                       seen_ids: set[str] | None = None) -> Usage:
    usage, _ = read_claude_jsonl(path, start_offset, seen_ids)
    return usage


def read_claude_jsonl(path: str | Path, start_offset: int = 0,
                      seen_ids: set[str] | None = None) -> tuple[Usage, int]:
    """解析 Claude Code 会话 jsonl，返回 (usage, 结束偏移)。

    真实记录形如::

        {"type":"assistant","message":{"id":"msg_...","usage":{
         "input_tokens":..,"output_tokens":..,"cache_read_input_tokens":..,
         "cache_creation_input_tokens":..}},"timestamp":"ISO8601"}

    同一条 assistant 消息会被重复写入多行且 usage 完全相同，按 ``message.id`` 去重，
    否则会高估。``seen_ids`` 可由 meter 跨多次 collect 复用。
    """
    usage = Usage()
    state = seen_ids if seen_ids is not None else set()
    file_path = Path(path)
    if not file_path.exists():
        return usage, start_offset
    end = start_offset
    for record, end in _iter_jsonl(file_path, start_offset):
        if record is None:
            continue
        message = record.get("message")
        value = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(value, dict):
            value = _find_usage(record)
        if not isinstance(value, dict):
            continue
        message_id = message.get("id") if isinstance(message, dict) else None
        if isinstance(message_id, str) and message_id:
            if message_id in state:
                continue
            state.add(message_id)
        moment = _timestamp(record.get("timestamp", record.get("time")))
        usage.add(Usage(
            input=_int(value, "input_tokens", "input", "prompt_tokens")
                  + _int(value, "cache_creation_input_tokens"),
            output=_int(value, "output_tokens", "output", "completion_tokens"),
            cached=_cached_tokens(value),
            requests=1,
            first_token_ts=moment,
            last_token_ts=moment,
            wall_time=_float(record.get("wall_time")),
        ))
    return usage, end


class ClaudeMeter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.offset = 0
        self._seen_ids: set[str] = set()

    def start(self, **kwargs: Any) -> None:
        self.offset = _file_size(self.path)

    def collect(self, **kwargs: Any) -> Usage:
        if not self.path.exists():
            self.offset = 0
            return Usage()
        if self.path.stat().st_size < self.offset:
            self.offset = 0
            self._seen_ids.clear()
        usage, end = read_claude_jsonl(self.path, self.offset, self._seen_ids)
        self.offset = end
        return usage
