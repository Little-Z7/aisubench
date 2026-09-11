from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import signal
import subprocess
from typing import Sequence


@dataclass
class VerifyResult:
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def run_verify(command: str | Sequence[str], cwd: str | Path, timeout_sec: float = 60,
               task_id: str | None = None) -> VerifyResult:
    """Run a verifier in its own process group and enforce its timeout."""
    environment = os.environ.copy()
    workspace = str(Path(cwd).resolve())
    if task_id:
        environment["AISUBENCH_TASK_ID"] = task_id
    environment["AISUBENCH_WORKSPACE"] = workspace
    shell = isinstance(command, str)
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=environment,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        process.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    stdout, stderr = _collect_output(process)
    return VerifyResult(
        not timed_out and process.returncode == 0,
        -1 if timed_out else process.returncode,
        stdout,
        stderr,
        timed_out,
    )


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _collect_output(process: subprocess.Popen) -> tuple[str, str]:
    """Collect output without waiting forever for a descendant-held pipe."""
    try:
        stdout, stderr = process.communicate(timeout=0.25)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout
        stderr = exc.stderr
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    return _text(stdout), _text(stderr)


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
