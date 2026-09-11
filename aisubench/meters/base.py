"""共享 token 计量类型。

所有 meter 统一口径：

- ``input``：非缓存输入 tokens（Kimi ``inputOther``；Claude ``input_tokens`` +
  ``cache_creation_input_tokens``；OpenAI ``prompt_tokens - cached_tokens``）
- ``cached``：缓存命中读取的 tokens（cache read；Kimi ``inputCacheRead``、
  Claude ``cache_read_input_tokens``、OpenAI ``prompt_tokens_details.cached_tokens``）
- ``output``：生成的 tokens
- ``total``：``input + cached + output``
- ``requests``：LLM 请求数，每条 usage 记录计 1
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


class TokenMeter(Protocol):
    def start(self, **kwargs: Any) -> None: ...

    def collect(self, **kwargs: Any) -> "Usage": ...


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cached: int = 0
    requests: int = 0
    first_token_ts: float | None = None
    last_token_ts: float | None = None
    wall_time: float = 0.0

    @property
    def total(self) -> int:
        return max(0, self.input) + max(0, self.cached) + max(0, self.output)

    def add(self, other: "Usage") -> "Usage":
        first = self.first_token_ts
        if first is None or (other.first_token_ts is not None and other.first_token_ts < first):
            first = other.first_token_ts
        last = self.last_token_ts
        if last is None or (other.last_token_ts is not None and other.last_token_ts > last):
            last = other.last_token_ts
        self.input += other.input
        self.output += other.output
        self.cached += other.cached
        self.requests += other.requests
        self.first_token_ts, self.last_token_ts = first, last
        self.wall_time += other.wall_time
        return self

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total"] = self.total
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Usage":
        allowed = {field for field in cls.__dataclass_fields__}
        return cls(**{key: data[key] for key in allowed if key in data})
