"""watch 采样：周期性把「meter token 增量 + 额度探针快照」写入账本（``ledger`` 模块）。

    aisubench watch --agent <name> --probe <name> [--once] [--interval SEC] [--ledger PATH]

一次采样 = meter 增量 + probe 快照：

- meter 复用 :mod:`aisubench.meters`（``kimi`` / ``claude``），agent 在
  ``aisubench.toml`` 的 [agents.*] 里配置 ``meter`` 与 ``log_path``。
  watch 往往是分钟级独立进程（cron + ``--once``），而 meter 的字节偏移是内存态，
  因此每个 log 文件的读取偏移持久化在 ``state/meter_offsets.json``
  （结构 ``{log 绝对路径: 字节偏移}``）：本次采样从上次偏移续读，采样成功后写回；
  文件比偏移小（轮转/截断）则偏移归零从头重读。
  已知局限：claude meter 的 message id 去重集不持久化——同一次采样区间内的重复
  行仍会正确去重，但跨采样区间的同消息重复写入会被计两次；
  长驻进程配较小 ``--interval`` 时去重集在进程内持续有效，可减轻该问题。
- probe 复用 :mod:`aisubench.quota`（``arkcli`` / ``manual`` / ``mock``），
  ``snapshot()`` 的 {池名: 已用%} 即样本的 ``pools`` 字段。

``clean`` 判定的简化假设（与估计器的契约口径）：
    距同 agent 上一个样本的间隔小于阈值（``[watch].clean_window_sec``，默认
    600 秒）时 ``clean=True``，否则 ``clean=False``。假设是：在足够密的窗口内，
    未被 meter 记入 agent 日志的外部渠道（网页版、手机端等）产生的额度变化
    可忽略；而间隔过长时无法排除这类外部消耗，整段区间即不可信，标为不 clean，
    由估计侧（``estimate_pools(clean_only=...)``）决定丢弃该相邻对。
    首个样本没有可对比的前后间隔，其 usage 覆盖长度未知的时段，保守标
    ``clean=False``。

探针/网络等瞬时异常在循环中只打警告并继续（该轮不落账本、偏移不前进，
消耗并入下一轮增量）；``--once`` 下异常直接上抛、命令以错误退出。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import ROOT, load_config
from .ledger import DEFAULT_LEDGER, append_sample, load_samples
from .meters.claude import ClaudeMeter
from .meters.kimi import KimiMeter
from .quota.arkcli import ArkcliQuotaProbe
from .quota.manual import ManualQuotaProbe
from .quota.mock import MockQuotaProbe

METERS: dict[str, Any] = {"kimi": KimiMeter, "claude": ClaudeMeter}
PROBES: dict[str, Any] = {"mock": MockQuotaProbe, "arkcli": ArkcliQuotaProbe,
                          "manual": ManualQuotaProbe}

DEFAULT_INTERVAL_SEC = 300.0
DEFAULT_CLEAN_WINDOW_SEC = 600.0
OFFSETS_FILE = ROOT / "state" / "meter_offsets.json"
# 冒烟特判：`watch --agent mock` 未配 meter 时使用的合成演示日志（首次采样生成，
# 内容为 kimi wire.jsonl 格式），避免默认配置指向不存在的真实 agent 日志。
MOCK_LOG_PATH = ROOT / "state" / "mock_wire.jsonl"


def load_offsets(path: str | Path) -> dict[str, int]:
    """读取持久化的 meter 字节偏移；文件缺失或损坏时返回空表（从头读）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    offsets: dict[str, int] = {}
    for key, value in data.items():
        if isinstance(value, bool):
            continue
        try:
            offsets[str(key)] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    return offsets


def save_offsets(path: str | Path, offsets: dict[str, int]) -> None:
    """原子写回偏移状态（临时文件 + rename），父目录自动创建。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(file_path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump({str(key): int(value) for key, value in offsets.items()},
                  handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp_path, file_path)


class WatchSession:
    """一个 agent 的采样会话：偏移持久化续读 + 探针快照 + clean 判定 + 落账本。"""

    def __init__(self, agent: str, meter_name: str, log_path: str | Path, probe: Any,
                 ledger_path: str | Path, offsets_path: str | Path = OFFSETS_FILE,
                 clean_window_sec: float = DEFAULT_CLEAN_WINDOW_SEC):
        if meter_name not in METERS:
            raise ValueError(f"未知 meter: {meter_name}（可选 kimi / claude）")
        self.agent = agent
        self.meter_name = meter_name
        self.log_path = Path(log_path)
        self.probe = probe
        self.ledger_path = Path(ledger_path)
        self.offsets_path = Path(offsets_path)
        self.clean_window_sec = float(clean_window_sec)

    def sample_once(self, now: float | None = None) -> dict:
        """采样一次并追加到账本，打印摘要；探针异常时上抛且不改任何持久化状态。"""
        ts = time.time() if now is None else float(now)
        log_key = str(self.log_path)
        offsets = load_offsets(self.offsets_path)
        start = offsets.get(log_key, 0)
        try:
            size = self.log_path.stat().st_size
        except OSError:
            size = 0
        if size < start:
            start = 0  # 日志轮转/截断：偏移归零重读
        meter = METERS[self.meter_name](self.log_path)
        meter.offset = start
        usage = meter.collect()
        pools = {str(name): float(pct) for name, pct in dict(self.probe.snapshot()).items()}
        sample = {
            "ts": ts,
            "agent": self.agent,
            "source": "watch",
            "usage": {
                "input": max(0, int(usage.input)),
                "cached": max(0, int(usage.cached)),
                "output": max(0, int(usage.output)),
                "requests": max(0, int(usage.requests)),
            },
            "pools": pools,
            "clean": self._interval_clean(ts),
        }
        append_sample(self.ledger_path, sample)
        offsets[log_key] = max(0, int(meter.offset))
        save_offsets(self.offsets_path, offsets)
        print(self.format_summary(sample))
        return sample

    def _interval_clean(self, ts: float) -> bool:
        """距同 agent 上一样本的间隔小于阈值视为 clean；首个样本保守为 False。"""
        previous: float | None = None
        for sample in load_samples(self.ledger_path):
            if sample.get("agent") != self.agent:
                continue
            value = sample.get("ts")
            if isinstance(value, bool):
                continue
            try:
                stamp = float(value)
            except (TypeError, ValueError):
                continue
            if previous is None or stamp > previous:
                previous = stamp
        return previous is not None and ts - previous < self.clean_window_sec

    @staticmethod
    def format_summary(sample: dict) -> str:
        usage = sample.get("usage") or {}
        total = sum(max(0, int(usage.get(key, 0) or 0)) for key in ("input", "cached", "output"))
        stamp = datetime.fromtimestamp(float(sample.get("ts", 0) or 0))
        parts = [
            f"[{stamp.strftime('%Y-%m-%d %H:%M:%S')}] +{total} tokens"
            f" (input={usage.get('input', 0)} cached={usage.get('cached', 0)}"
            f" output={usage.get('output', 0)})",
        ]
        pools = sample.get("pools") or {}
        if pools:
            parts.append(" ".join(f"{name}={pct:g}%" for name, pct in pools.items()))
        parts.append("clean" if sample.get("clean") else "unclean")
        return " | ".join(parts)


def run_watch(agent: str, probe: str, *, once: bool = False, interval: float | None = None,
              ledger: str | Path | None = None, config: dict | None = None,
              sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """watch 子命令主体。``config`` / ``sleep_fn`` 可注入，便于测试与 cron 复用。"""
    config = load_config() if config is None else config
    watch_cfg = config.get("watch") or {}
    clean_window = float(watch_cfg.get("clean_window_sec", DEFAULT_CLEAN_WINDOW_SEC))
    if interval is None:
        interval = float(watch_cfg.get("interval_sec", DEFAULT_INTERVAL_SEC))
    if interval <= 0 or clean_window <= 0:
        raise ValueError("采样间隔与 clean 阈值必须为正数")
    ledger_path = _resolve_path(ledger or watch_cfg.get("ledger"), DEFAULT_LEDGER)
    offsets_path = _resolve_path(watch_cfg.get("offsets_file"), OFFSETS_FILE)
    agent_settings = (config.get("agents") or {}).get(agent) or {}
    meter_name, log_path = _resolve_meter(agent, agent_settings)
    if probe not in PROBES:
        raise ValueError(f"未知 probe: {probe}（可选 mock / arkcli / manual）")
    probe_obj = PROBES[probe]()
    if not once and getattr(probe_obj, "continuous", True) is False:
        print("提示：manual 探针每轮采样都要求人工输入，不适合循环模式；"
              "建议用 --once 手动节奏采样（配合 cron 等外部节奏）。", file=sys.stderr)
    session = WatchSession(agent=agent, meter_name=meter_name, log_path=log_path,
                           probe=probe_obj, ledger_path=ledger_path,
                           offsets_path=offsets_path, clean_window_sec=clean_window)
    try:
        if once:
            session.sample_once()
            return 0
        while True:
            try:
                session.sample_once()
            except Exception as exc:
                print(f"警告：本轮采样失败，跳过并继续：{exc}", file=sys.stderr)
            sleep_fn(interval)
    except KeyboardInterrupt:
        print("watch：收到 Ctrl-C，停止采样。", file=sys.stderr)
        return 0


def _resolve_meter(agent: str, settings: dict) -> tuple[str, Path]:
    """从 [agents.*] 配置解析 (meter 名, 日志绝对路径)；mock agent 走合成日志特判。"""
    meter = str(settings.get("meter") or "").strip().lower()
    raw_path = settings.get("log_path")
    if not meter and agent == "mock":
        meter = "mock"
    if meter == "mock":
        log_path = _resolve_path(raw_path, MOCK_LOG_PATH)
        return "kimi", _ensure_mock_log(log_path)
    if not meter or not raw_path:
        raise ValueError(
            f"agent {agent} 未在 aisubench.toml 的 [agents.*] 配置 meter / log_path"
            "（冒烟可先用 --agent mock）")
    if meter not in METERS:
        raise ValueError(f"未知 meter: {meter}（可选 kimi / claude）")
    return meter, _resolve_path(raw_path, Path(str(raw_path)))


def _resolve_path(value: Any, default: Path) -> Path:
    if not value:
        return Path(default)
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else ROOT / path


def _ensure_mock_log(path: Path) -> Path:
    """mock 冒烟用合成日志：不存在时写入两条 kimi wire.jsonl 格式的演示记录。"""
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"inputOther": 40, "output": 12, "inputCacheRead": 300, "inputCacheCreation": 8},
        {"inputOther": 6, "output": 3, "inputCacheRead": 50, "inputCacheCreation": 1},
    ]
    lines = []
    for index, usage in enumerate(records):
        lines.append(json.dumps({
            "type": "usage.record", "agentId": "watch-demo", "model": "demo/mock",
            "usage": usage, "usageScope": "turn", "time": 1789111388997 + index * 10000,
        }, ensure_ascii=False) + "\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path
