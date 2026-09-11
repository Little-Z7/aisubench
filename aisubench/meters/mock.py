from __future__ import annotations

import json
from pathlib import Path
from .base import Usage


class MockMeter:
    """确定性的 meter：始终返回构造时给定的 usage，不受时钟或文件影响。"""

    def __init__(self, usage: Usage | None = None):
        self.usage = usage or Usage()

    def start(self, **kwargs) -> None:
        return None

    def collect(self, **kwargs) -> Usage:
        return self.usage


def run_mock(task_id: str, cwd: str | Path) -> Usage:
    root = Path(cwd)
    outputs = {
        "fix-syntax": {"fixed.py": "def add(a, b):\n    return a + b\n"},
        "echo-file": {"output.txt": "AISUBench\n"},
        "add-two-ints": {"answer.txt": "42\n"},
        "fix-off-by-one": {"solution.py": "def total(values):\n    return sum(values)\n"},
        "implement-from-docstring": {"solution.py": "def square(value):\n    return value * value\n"},
        "rename-across-files": {"renamed.txt": "new_name\n"},
        "log-parse-script": {"summary.txt": "INFO=2 ERROR=1\n"},
        "cli-with-argparse": {"cli.py": "import argparse\n\nparser = argparse.ArgumentParser()\nparser.add_argument('value')\nargs = parser.parse_args()\nprint(args.value)\n"},
        "multi-file-bugfix": {"fixed.txt": "fixed\n"},
        "config-migration": {"config.json": '{"version": 2, "enabled": true}\n'},
    }
    for filename, content in outputs.get(task_id, {}).items():
        (root / filename).write_text(content)
    usage = Usage(input=100 + len(task_id), output=40 + len(task_id), cached=0, requests=1,
                  first_token_ts=1.0, last_token_ts=2.0, wall_time=1.0)
    (root / ".mock_usage.json").write_text(json.dumps(usage.to_dict()))
    (root / ".agent_complete").write_text("true\n")
    return usage
