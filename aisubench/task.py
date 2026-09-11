from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from .config import ROOT

TIERS = ("calibration", "light", "medium")


@dataclass(frozen=True)
class TaskSpec:
    id: str
    tier: str
    prompt: str
    verify: str
    timeout_sec: float
    max_tokens: int
    path: Path
    tags: tuple[str, ...] = ()

    @property
    def fixtures_dir(self) -> Path:
        return self.path / "fixtures"

    @classmethod
    def load(cls, task_path: str | Path) -> "TaskSpec":
        path = Path(task_path)
        if path.is_dir():
            path = path / "task.toml"
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        required = ("id", "tier", "prompt", "verify", "timeout_sec", "max_tokens")
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"任务配置缺少字段: {', '.join(missing)}")
        task_id = str(data["id"])
        if not task_id or task_id != path.parent.name:
            raise ValueError("任务 id 必须与目录名一致")
        tier = str(data["tier"])
        if tier not in TIERS:
            raise ValueError(f"未知任务档位: {tier}")
        timeout = float(data["timeout_sec"])
        max_tokens = int(data["max_tokens"])
        if timeout <= 0 or max_tokens <= 0:
            raise ValueError("timeout_sec 与 max_tokens 必须为正数")
        prompt = str(data["prompt"])
        if not prompt:
            raise ValueError("prompt 不能为空")
        verify = str(data["verify"])
        if not verify:
            raise ValueError("verify 不能为空")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError("tags 必须是字符串数组")
        return cls(task_id, tier, prompt, verify, timeout, max_tokens,
                   path.parent, tuple(tags))


def task_root(root: str | Path | None = None) -> Path:
    return Path(root) if root else ROOT / "tasks"


def list_tasks(root: str | Path | None = None, tier: str | None = None) -> list[TaskSpec]:
    base = task_root(root)
    specs = [TaskSpec.load(path) for path in sorted(base.glob("*/task.toml"))]
    if tier:
        specs = [spec for spec in specs if spec.tier == tier]
    return specs


def load_task(task_id: str, root: str | Path | None = None) -> TaskSpec:
    path = task_root(root) / task_id / "task.toml"
    if not path.exists():
        raise FileNotFoundError(f"任务不存在: {task_id}")
    return TaskSpec.load(path)
