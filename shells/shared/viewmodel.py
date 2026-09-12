"""原生壳共用视图模型：把 ``aisubench.status.collect_status`` 的 dict 转成壳展示行。

纯函数、零第三方依赖（不 import rumps/AppKit/PySide6），Linux 可直接单测；
``shells/macos`` 把这些行挂进 NSMenu，``shells/windows`` 用它们绘制悬浮球
详情面板。文案与 ``aisubench status`` CLI 一致：不可用 /
数据不足(Δ 未超粒度) / 账本中暂无该池样本。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

APP_TITLE = "AISUBench"
# 最后采样距今超过该秒数时头部追加「数据陈旧」。
STALE_AFTER_SEC = 30 * 60
# 池名解析不出窗口小时数时的兜底值。
DEFAULT_POOL_WINDOW_HOURS = 24.0

_HOURS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*h$")
_NAMED_WINDOWS = {"week": 168.0, "w": 168.0, "month": 720.0, "mo": 720.0}


def _urgent_pool(pools: list | None) -> dict | None:
    """最紧急池：有 ETA 的池取最小 ETA，否则取已用%最大的池，都没有回 None。"""
    pools = pools or []
    with_eta = [pool for pool in pools if pool.get("eta_hours") is not None]
    if with_eta:
        return min(with_eta, key=lambda item: item["eta_hours"])
    with_pct = [pool for pool in pools if pool.get("current_pct") is not None]
    return max(with_pct, key=lambda item: item["current_pct"]) if with_pct else None


def title_text(status: dict | None) -> str:
    """菜单栏标题：最紧急池（``_urgent_pool``）的 ``池名 已用%``，无池回 APP_TITLE。

    格式如 ``"5h 46%"``。
    """
    pool = _urgent_pool((status or {}).get("pools"))
    if pool is None:
        return APP_TITLE
    pct = pool.get("current_pct")
    if pct is None:
        return str(pool.get("name") or APP_TITLE)
    return f"{pool['name']} {pct:.0f}%"


def ball_view(status: dict | None) -> dict:
    """悬浮球面视图：最紧急池（与 ``title_text`` 同口径）的紧凑文案与告警级别。

    返回 ``{"text", "level"}``：text 形如 ``"86%"``；选中的池无读数时为
    ``"--"``，无池/空状态为 ``"—"``；level 为该池 ``_level`` 结果
    （``normal`` / ``warn`` / ``critical``，无池为 ``"none"``），供壳层映射
    球体颜色。
    """
    pool = _urgent_pool((status or {}).get("pools"))
    if pool is None:
        return {"text": "—", "level": "none"}
    pct = pool.get("current_pct")
    return {"text": "--" if pct is None else f"{pct:.0f}%",
            "level": _level(pool)}


def pool_rows(status: dict | None) -> list[dict]:
    """每池一行 {title, detail, level, pct}；空账本（ledger 缺失或 n_samples=0）返回空。

    - title 形如 ``"5h · 已用 46%"``（字段缺失显示「不可用」）；
    - detail 第一行为「已用≈tokens · Q(低–高)」，第二行为「速率 · ETA」；
    - level: ``eta < 2`` 为 'critical'；``eta < 池窗口一半`` 为 'warn'
      （池窗口从池名解析：'5h'→5、'week'→168、'month'→720，解析不了按 24）。
    """
    if not status or status.get("ledger_exists") is False or not status.get("n_samples"):
        return []
    return [_pool_row(pool) for pool in status.get("pools") or []]


def header_rows(status: dict | None, now: float | None = None) -> list[str]:
    """头部信息行：样本数与跨度、最后采样时间、未观测渠道提示。

    空账本（ledger_exists=False 或 n_samples=0）改为 watch 引导；
    最后采样距今超过 ``STALE_AFTER_SEC``（30 分钟）时追加「数据陈旧」；
    ``now`` 可注入便于测试。
    """
    if not status or status.get("ledger_exists") is False or not status.get("n_samples"):
        return ["账本还没有样本——这是正常的初始状态。",
                "先运行 python3 -m aisubench watch --once 采样，再看这里。"]
    rows = [f"样本 {int(status.get('n_samples') or 0)} · "
            f"跨度 {float(status.get('span_hours') or 0.0):.1f} h"]
    last = status.get("last_sample_ts")
    if last is None:
        rows.append("最后采样 不可用")
    else:
        stamp = datetime.fromtimestamp(float(last)).strftime("%Y-%m-%d %H:%M")
        line = f"最后采样 {stamp}"
        current = time.time() if now is None else float(now)
        if current - float(last) > STALE_AFTER_SEC:
            line += "（数据陈旧）"
        rows.append(line)
    external = int(status.get("external_tokens") or 0)
    if external > 0:
        rows.append(f"提示：约 {external:,} tokens 可能混入未观测渠道")
    return rows


def _pool_row(pool: dict) -> dict:
    name = str(pool.get("name") or "?")
    pct = pool.get("current_pct")
    title = f"{name} · 已用 {'不可用' if pct is None else f'{pct:.0f}%'}"
    if not pool.get("has_samples"):
        return {"title": title, "detail": "账本中暂无该池样本",
                "level": "normal", "pct": pct}
    used = pool.get("used_tokens")
    used_text = "已用≈不可用" if used is None else f"已用≈{used:,.0f}"
    detail = f"{used_text} · {_quota_text(pool)}\n{_rate_text(pool)} · {_eta_text(pool)}"
    return {"title": title, "detail": detail, "level": _level(pool), "pct": pct}


def _quota_text(pool: dict) -> str:
    if not pool.get("available"):
        return "Q 数据不足(Δ 未超粒度)"
    quota, low, high = pool.get("quota_tokens"), pool.get("q_low"), pool.get("q_high")
    if quota is None or low is None:
        return "Q 不可用"
    if high is None:
        return f"Q {quota:,.0f}（≥{low:,.0f}）"
    return f"Q {quota:,.0f}（{low:,.0f}–{high:,.0f}）"


def _rate_text(pool: dict) -> str:
    rate = pool.get("rate_tph")
    return "速率 不可用" if rate is None else f"速率 {rate:,.0f} tok/h"


def _eta_text(pool: dict) -> str:
    eta = pool.get("eta_hours")
    return "ETA 不可用" if eta is None else f"ETA {eta:.1f} h"


def _level(pool: dict) -> str:
    eta = pool.get("eta_hours")
    if eta is None:
        return "normal"
    if eta < 2:
        return "critical"
    if eta < _pool_window_hours(pool.get("name")) / 2:
        return "warn"
    return "normal"


def _pool_window_hours(name: Any) -> float:
    """从池名解析窗口小时数：'5h'→5、'week'→168、'month'→720，解析不了按 24。"""
    text = str(name or "").strip().lower()
    match = _HOURS_RE.match(text)
    if match:
        return float(match.group(1))
    return _NAMED_WINDOWS.get(text, DEFAULT_POOL_WINDOW_HOURS)
