from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    config_path = Path(path) if path else ROOT / "aisubench.toml"
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    override = config_path.with_name("aisubench.local.toml")
    if path is None and override.exists():
        with override.open("rb") as handle:
            data = _merge(data, tomllib.load(handle))
    return data


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result
