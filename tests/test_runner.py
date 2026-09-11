from __future__ import annotations

import json
from pathlib import Path
import tempfile
import textwrap
import time
import unittest

from aisubench.meters.base import Usage
from aisubench.runner import Runner
from aisubench.task import TaskSpec
from aisubench.verify import run_verify


class RunnerTests(unittest.TestCase):
    def make_task(self, root: Path, *, verify: str = "python3 verify.py",
                  timeout: float = 2, max_tokens: int = 1000,
                  verify_timeout: float | None = None,
                  solution: dict[str, str] | None = None) -> TaskSpec:
        task_dir = root / "task"
        task_dir.mkdir()
        extra = f"verify_timeout_sec = {verify_timeout}\n" if verify_timeout else ""
        (task_dir / "task.toml").write_text(
            f'id = "task"\ntier = "light"\nprompt = "write answer"\n'
            f'verify = {verify!r}\ntimeout_sec = {timeout}\nmax_tokens = {max_tokens}\n{extra}'
        )
        (task_dir / "verify.py").write_text(
            "from pathlib import Path\n"
            "assert Path('answer.txt').read_text() == 'ok\\n'\n"
        )
        if solution:
            solution_dir = task_dir / "solution"
            solution_dir.mkdir()
            for name, content in solution.items():
                (solution_dir / name).write_text(content)
        return TaskSpec.load(task_dir)

    def test_verify_is_not_copied_and_absolute_script_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.make_task(root, solution={"answer.txt": "ok\n"})
            output = root / "run"
            result = Runner(runs_dir=root / "runs", config={}).run(task, output_dir=output)
            self.assertTrue(result["converged"])
            self.assertFalse(Path(result["workspace"]).exists())
            self.assertFalse((output / "verify.py").exists())

    def test_template_replacement_allows_literal_braces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.make_task(root)
            script = root / "agent.py"
            script.write_text(textwrap.dedent("""
                from pathlib import Path
                import os, json
                Path('answer.txt').write_text('ok\\n')
                Path('.aisubench_usage.json').write_text(json.dumps({'input': 1, 'output': 2}))
                Path('.agent_complete').write_text('yes')
                print('{literal}')
            """))
            config = {"agents": {"fake": {"command": ["python3", str(script), "$workspace"]}}}
            result = Runner(runs_dir=root / "runs", config=config).run(task, "fake")
            self.assertTrue(result["converged"])
            self.assertEqual(result["usage"]["total"], 3)

    def test_timeout_preserves_usage_and_kills_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.make_task(root, timeout=1, max_tokens=1000)
            script = root / "agent.py"
            pid_file = root / "child.pid"
            script.write_text(textwrap.dedent(f"""
                import json, os, subprocess, sys, time
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
                open({str(pid_file)!r}, 'w').write(str(child.pid))
                open('.aisubench_usage.json', 'w').write(json.dumps({{'input': 4, 'output': 5}}))
                open('.agent_complete', 'w').write('yes')
                time.sleep(30)
            """))
            config = {"agents": {"fake": {"command": ["python3", str(script)]}}}
            result = Runner(runs_dir=root / "runs", config=config).run(task, "fake")
            self.assertTrue(result["agent_timed_out"])
            self.assertEqual(result["usage"]["total"], 9)
            child_pid = int(pid_file.read_text())
            for _ in range(20):
                try:
                    Path(f"/proc/{child_pid}").stat()
                except FileNotFoundError:
                    break
                time.sleep(0.02)
            else:
                self.fail("agent descendant survived timeout")

    def test_false_convergence_excludes_timeout_and_token_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.make_task(root, timeout=0.2, max_tokens=1)
            script = root / "agent.py"
            script.write_text("import json,time; open('.agent_complete','w').write('x'); open('.aisubench_usage.json','w').write(json.dumps({'input': 2})); time.sleep(2)")
            config = {"agents": {"fake": {"command": ["python3", str(script)]}}}
            result = Runner(runs_dir=root / "runs", config=config).run(task, "fake")
            self.assertFalse(result["false_convergence"])
            self.assertTrue(result["agent_timed_out"])

    def test_verify_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_verify("python3 -c 'import subprocess,time; subprocess.Popen([\"python3\",\"-c\",\"import time; time.sleep(30)\"]); time.sleep(30)'", directory, 0.2, "task")
            self.assertTrue(result.timed_out)
            self.assertFalse(result.passed)

    def test_mock_replays_solution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = self.make_task(root, solution={"answer.txt": "ok\n"})
            result = Runner(runs_dir=root / "runs", config={}).run(task)
            self.assertTrue(result["converged"])
            self.assertGreater(result["usage"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
