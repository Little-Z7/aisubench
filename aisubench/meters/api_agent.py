from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import Usage
from .kimi import _cached_tokens, _int

DEFAULT_MAX_TOKENS = 4096


def _config_max_tokens() -> int:
    try:
        from ..config import load_config
        config = load_config()
    except Exception:
        return DEFAULT_MAX_TOKENS
    for section in (config.get("api"), config.get("agents", {}).get("api")):
        if isinstance(section, dict) and "max_tokens" in section:
            try:
                return max(1, int(section["max_tokens"]))
            except (TypeError, ValueError):
                continue
    return DEFAULT_MAX_TOKENS


def parse_openai_usage(raw: dict) -> Usage:
    """把 OpenAI 兼容的 usage 对象转为统一口径。

    OpenAI 的 ``prompt_tokens`` 已包含缓存命中部分，因此 ``input`` 取
    ``prompt_tokens - prompt_tokens_details.cached_tokens``。
    """
    prompt = _int(raw, "prompt_tokens", "input_tokens", "input")
    cached = _cached_tokens(raw)
    return Usage(
        input=max(0, prompt - cached),
        output=_int(raw, "completion_tokens", "output_tokens", "output"),
        cached=cached,
        requests=1,
    )


def run_api_agent(prompt: str, endpoint: str, api_key: str | None = None,
                  model: str = "default", rounds: int = 1, timeout: float = 60,
                  max_tokens: int | None = None) -> tuple[str, Usage]:
    """运行最小 OpenAI 兼容循环；调用方负责确保 endpoint 可访问。

    非流式响应拿不到逐 token 时间戳，``first_token_ts``/``last_token_ts`` 记为请求
    起止时间，因此经此 usage 计算出的 tps_gen 只是近似值。
    """
    usage = Usage()
    messages = [{"role": "user", "content": prompt}]
    answer = ""
    started = time.time()
    limit = max_tokens if max_tokens is not None else _config_max_tokens()
    for _ in range(max(0, rounds)):
        payload = json.dumps({"model": model, "messages": messages, "max_tokens": limit}).encode()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read())
        except (HTTPError, URLError, OSError, ValueError):
            break
        choices = data.get("choices") or []
        answer = ""
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict):
                answer = message.get("content") or ""
        usage.add(parse_openai_usage(data.get("usage") or {}))
        messages.append({"role": "assistant", "content": answer})
    usage.wall_time = time.time() - started
    if usage.requests:
        usage.first_token_ts = started
        usage.last_token_ts = time.time()
    return answer, usage
