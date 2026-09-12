from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import time

from .config import ROOT, load_config
from .meters.base import Usage
from .meters.mock import run_mock
from .task import TaskSpec
from .verify import run_verify


class Runner:
    def __init__(self, runs_dir: str | Path | None = None, config: dict | None = None):
        self.runs_dir = Path(runs_dir) if runs_dir else ROOT / "runs"
        self.config = config if config is not None else load_config()

    def run(self, task: TaskSpec, agent: str = "mock", output_dir: str | Path | None = None) -> dict:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = f"{task.id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        run_path = Path(output_dir) if output_dir else self.runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(prefix=f"aisubench-{task.id}-")).resolve()
        started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        agent_returncode = 0
        agent_timed_out = False
        token_limit_exceeded = False
        agent_stdout = ""
        agent_stderr = ""
        usage = Usage()
        try:
            meter = self._meter(agent)
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        try:
            if task.fixtures_dir.exists():
                shutil.copytree(task.fixtures_dir, workspace, dirs_exist_ok=True)
            if meter is not None:
                meter.start()
            if agent == "mock":
                usage = _run_mock(task, workspace)
                agent_stdout = "mock agent complete"
            elif agent == "api":
                agent_stdout, usage = _run_api(task)
                (workspace / ".agent_complete").write_text("true\n", encoding="utf-8")
                (workspace / ".aisubench_usage.json").write_text(
                    json.dumps(_usage_dict(usage)), encoding="utf-8"
                )
            else:
                command = self._agent_command(agent, task, workspace)
                environment = os.environ.copy()
                root = str(ROOT)
                environment["PYTHONPATH"] = root + os.pathsep + environment.get("PYTHONPATH", "")
                environment["AISUBENCH_TASK_ID"] = task.id
                environment["AISUBENCH_WORKSPACE"] = str(workspace)
                process = subprocess.Popen(
                    command, cwd=workspace, env=environment, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                )
                agent_returncode, agent_timed_out, agent_stdout, agent_stderr = _run_process(
                    process, task.timeout_sec
                )
            if meter is not None:
                metered = meter.collect()
                if _usage_total(metered) or metered.requests:
                    usage = metered
            usage = _read_usage_file(workspace, usage)
        except Exception as exc:
            agent_returncode = -1
            agent_stderr = _text(exc)
            usage = _read_usage_file(workspace, usage)
        finally:
            if meter is not None:
                try:
                    metered = meter.collect()
                    if _usage_total(metered) or metered.requests:
                        usage = metered
                except Exception:
                    pass
            usage = _read_usage_file(workspace, usage)

        try:
            elapsed = time.monotonic() - started
            usage.wall_time = usage.wall_time or elapsed
            token_limit_exceeded = _usage_total(usage) > task.max_tokens
            verify_script = (task.path / "verify.py").resolve()
            verify_timeout = _verify_timeout(task)
            if verify_script.exists():
                verification = run_verify(
                    [sys.executable, str(verify_script)], workspace, verify_timeout, task.id
                )
            else:
                verification = run_verify(task.verify, workspace, verify_timeout, task.id)
            if token_limit_exceeded:
                verification.passed = False
                verification.stderr = (verification.stderr + "\n" if verification.stderr else "") + "超过 max_tokens 熔断上限"
            agent_reported_complete = (
                agent_returncode == 0 and (workspace / ".agent_complete").exists()
            )
            converged = verification.passed and agent_returncode == 0 and not agent_timed_out
            result = {
                "run_id": run_id, "task_id": task.id, "tier": task.tier, "agent": agent,
                "started_at": started_at.isoformat(), "wall_time": elapsed,
                "agent_returncode": agent_returncode, "agent_timed_out": agent_timed_out,
                "token_limit_exceeded": token_limit_exceeded, "max_tokens": task.max_tokens,
                "agent_reported_complete": agent_reported_complete,
                "agent_stdout": agent_stdout, "agent_stderr": agent_stderr,
                "verify": verification.to_dict(), "converged": converged,
                "false_convergence": (
                    agent_reported_complete and not verification.passed
                    and not token_limit_exceeded and not agent_timed_out
                ),
                "usage": _usage_dict(usage), "workspace": str(workspace),
            }
            (run_path / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _agent_command(self, agent: str, task: TaskSpec, workspace: Path) -> list[str]:
        command = self.config.get("agents", {}).get(agent, {}).get("command")
        if not command:
            raise ValueError(f"未知 agent: {agent}")
        if isinstance(command, str):
            command = shlex.split(command)
        values = {"task_id": task.id, "prompt": task.prompt, "workspace": str(workspace)}
        return [string.Template(str(part)).substitute(values) for part in command]

    def _meter(self, agent: str):
        settings = self.config.get("agents", {}).get(agent, {})
        meter_name = settings.get("meter")
        log_path = settings.get("log_path")
        if not meter_name or not log_path:
            return None
        if meter_name == "kimi":
            from .meters.kimi import KimiMeter
            return KimiMeter(log_path)
        if meter_name == "claude":
            from .meters.claude import ClaudeMeter
            return ClaudeMeter(log_path)
        raise ValueError(f"未知 meter: {meter_name}")


def _run_mock(task: TaskSpec, workspace: Path) -> Usage:
    solution = task.path / "solution"
    if solution.is_dir():
        shutil.copytree(solution, workspace, dirs_exist_ok=True)
    usage = run_mock(task.id, workspace)
    if solution.is_dir():
        shutil.copytree(solution, workspace, dirs_exist_ok=True)
    usage_path = workspace / ".aisubench_usage.json"
    usage_path.write_text(json.dumps(_usage_dict(usage)), encoding="utf-8")
    (workspace / ".agent_complete").write_text("true\n", encoding="utf-8")
    return usage


def _run_api(task: TaskSpec) -> tuple[str, Usage]:
    api_key = os.environ.get("AISUBENCH_API_KEY")
    if not api_key:
        raise ValueError("--agent api 需要设置环境变量 AISUBENCH_API_KEY")
    from .meters.api_agent import run_api_agent
    endpoint = os.environ.get("AISUBENCH_BASE_URL", "")
    model = os.environ.get("AISUBENCH_MODEL", "default")
    try:
        response = run_api_agent(task.prompt, endpoint, api_key=api_key, model=model,
                                 timeout=task.timeout_sec)
    except TypeError:
        response = run_api_agent(task.prompt, {
            "endpoint": endpoint, "base_url": endpoint, "api_key": api_key, "model": model,
            "timeout": task.timeout_sec,
        })
    if isinstance(response, tuple):
        answer, usage = response
    else:
        answer, usage = "", response
    if not isinstance(usage, Usage):
        usage = Usage.from_dict(usage) if isinstance(usage, dict) else Usage()
    return str(answer), usage


def _run_process(process: subprocess.Popen, timeout: float) -> tuple[int, bool, str, str]:
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        process.wait()
    stdout, stderr = _collect_output(process)
    return (-1 if timed_out else process.returncode, timed_out, stdout, stderr)


def _collect_output(process: subprocess.Popen) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=0.25)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = exc.stdout, exc.stderr
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return _text(stdout), _text(stderr)


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _read_usage_file(workspace: Path, usage: Usage) -> Usage:
    path = workspace / ".aisubench_usage.json"
    if not path.exists():
        return usage
    try:
        return Usage.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return usage


def _verify_timeout(task: TaskSpec) -> float:
    try:
        import tomllib
        with (task.path / "task.toml").open("rb") as handle:
            return float(tomllib.load(handle).get("verify_timeout_sec", 60))
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return 60.0


def _usage_total(usage: Usage) -> int:
    return max(0, usage.input) + max(0, usage.cached) + max(0, usage.output)


def _usage_dict(usage: Usage) -> dict:
    data = usage.to_dict()
    data["total"] = _usage_total(usage)
    return data


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
