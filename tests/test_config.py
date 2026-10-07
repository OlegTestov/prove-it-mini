"""Gate configuration: test command, timeout, watch globs, fixed fix-cycle limit."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import KIT, PY, TOOLS, fake_cli, git_repo, run

sys.path.insert(0, str(TOOLS))
import pytest_gate  # noqa: E402

GATE = str(TOOLS / "pytest_gate.py")
INSTALL = str(KIT / "install.sh")
RED = "echo 'FAILED t.py::a'; exit 1"
LANDING = KIT.parent / "landing" / "index.html"
SPAWNER = """
import subprocess, sys, time
child = subprocess.Popen(["sleep", "30"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(30)
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def install(target, *args, cwd=None, env=None):
    return run(["bash", INSTALL, str(target), *args], cwd=cwd, env=env)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_gate():
    spec = importlib.util.spec_from_file_location("pg5", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GateTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "def f():\n    return 1\n", "README.md": "x\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-gatebin-"))
    def suite(self, exit_code=0, output="1 passed"):
        """A fake test command: prints pytest-like output and exits with the given code."""
        return fake_cli(self.bin, f"suite{exit_code}.py", f"""
            import sys
            print({output!r})
            sys.exit({exit_code})
        """)
    def gate(self, test_cmd, session="s1", extra_env=None, args=()):
        env = {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": test_cmd, **(extra_env or {})}
        r = run([PY, GATE, *args], cwd=self.root, stdin=json.dumps({"session_id": session}), env=env)
        self.assertEqual(r.returncode if not args else 0, 0, r.stderr)
        return (json.loads(r.stdout) if r.stdout.strip() and not args else {}), r
    def events(self):
        f = self.root / ".prove-it/gate.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    def touch(self, body="def f():\n    return 2\n", name="app.py"):
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(body)
    def baseline(self, session="s1", test_cmd="true"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}),
            env={"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": test_cmd})
    def test_timeout_blocks_with_clear_reason(self):
        self.touch()
        slow = fake_cli(self.bin, "slow.py", "import time\ntime.sleep(5)\n")
        out, _ = self.gate(slow, extra_env={"PROVE_IT_TIMEOUT": "1"})
        self.assertIn("timed out after 1s", out["reason"])

    def test_config_file_and_bad_config(self):
        self.touch()
        cfg = self.root / ".claude/prove-it/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"test_cmd": self.suite(1, "FAILED c.py::z")}))
        r = run([PY, GATE], cwd=self.root, stdin='{"session_id": "c"}', env={"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertIn("fix cycle 1 of 2", json.loads(r.stdout)["reason"])
        cfg.write_text("{broken")
        r = run([PY, GATE], cwd=self.root, stdin='{"session_id": "d"}', env={"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual(r.returncode, 0)

    def test_disable_switch_and_garbage_input(self):
        self.touch()
        out, _ = self.gate(self.suite(1), extra_env={"PROVE_IT_GATE_DISABLE": "1"})
        self.assertEqual(out, {})
        r = run([PY, GATE], cwd=self.root, stdin="not json",
                env={"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": self.suite(0)})
        self.assertEqual(r.returncode, 0)

    def test_default_command_prefers_project_venv(self):
        self.assertIn("-m pytest -q", pytest_gate.default_test_cmd(self.root))
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/python").write_text("")
        self.assertEqual(pytest_gate.default_test_cmd(self.root), ".venv/bin/python -m pytest -q")



class GateCommandsTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-gf-"))
    def hook(self, test_cmd, session="s", extra=None):
        env = {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": test_cmd, **(extra or {})}
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=env)
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def check(self, test_cmd=None, extra=None):
        env = {"CLAUDE_PROJECT_DIR": str(self.root), **(extra or {})}
        if test_cmd:
            env["PROVE_IT_TEST_CMD"] = test_cmd
        return run([PY, GATE, "--check"], cwd=self.root, env=env)
    def hook_no_env(self):
        r = run([PY, GATE], cwd=self.root, stdin='{"session_id": "cfg"}', env={"CLAUDE_PROJECT_DIR": str(self.root)})
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def test_gate_timeout_kills_the_test_process_group(self):
        (self.root / "app.py").write_text("x = 2\n")
        spawner = self.bin / "spawner.py"
        spawner.write_text(SPAWNER)
        pidfile = self.bin / "pid"
        self.hook(f"{PY} {spawner} {pidfile}", extra={"PROVE_IT_TIMEOUT": "1"})
        time.sleep(0.5)
        pid = int(pidfile.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertFalse(alive(pid), "the test process kept running after the gate timed out")



class GateConfigAndCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "README.md": "docs\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g2-"))
    def env(self, test_cmd=None, **extra):
        e = {"CLAUDE_PROJECT_DIR": str(self.root), **extra}
        if test_cmd is not None:
            e["PROVE_IT_TEST_CMD"] = test_cmd
        return e
    def hook(self, test_cmd, session="s", **extra):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(test_cmd, **extra))
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, test_cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(test_cmd))
    def check(self, test_cmd=None, **extra):
        return run([PY, GATE, "--check"], cwd=self.root, env=self.env(test_cmd, **extra))
    def test_empty_or_invalid_config_values_are_errors(self):
        cfg = self.root / ".claude/prove-it/config.json"
        cfg.parent.mkdir(parents=True)
        for text in ('{"test_cmd": ""}', '{"test_cmd": "   "}', '{"test_cmd": null}', '{"test_cmd": 0}',
                     '{"test_cmd": "true", "timeout": Infinity}', '{"test_cmd": "true", "timeout": NaN}',
                     '{"test_cmd": "true", "max_cycles": true}'):
            cfg.write_text(text)
            r = self.check()
            self.assertEqual(r.returncode, 2, f"config {text} was not rejected")
        cfg.write_text('{"test_cmd": "true"}')
        self.assertEqual(self.check(test_cmd="   ").returncode, 2, "a whitespace PROVE_IT_TEST_CMD was accepted")



class GateRunsTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "tests/test_app.py": "def test_a():\n    assert True\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g3-"))
    def env(self, cmd, **extra):
        return {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": cmd, **extra}
    def hook(self, cmd, session="s", **extra):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd, **extra))
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
    def events(self):
        f = self.root / ".prove-it/gate.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    def test_timeout_must_stay_below_the_hook_timeout(self):
        cfg = self.root / ".claude/prove-it/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"test_cmd": "true", "timeout": 900}')
        r = run([PY, GATE, "--check"], cwd=self.root, env={"CLAUDE_PROJECT_DIR": str(self.root)})
        self.assertEqual(r.returncode, 2, "a test timeout longer than the hook's own timeout was accepted")

    def test_fix_cycles_are_fixed_at_two(self):
        (self.root / "app.py").write_text("x = 2\n")
        outs = [self.hook(RED, PROVE_IT_MAX_CYCLES="5")[0] for _ in range(3)]
        self.assertEqual([o.get("decision") for o in outs[:2]], ["block", "block"])
        self.assertNotIn("decision", outs[2], "more than two fix cycles were allowed")



if __name__ == "__main__":
    unittest.main()
