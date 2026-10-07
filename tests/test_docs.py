"""Docs and packaging: README, rules and pytest setup promise only what the kit does."""
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
    def test_docs_only_change_does_not_run_tests(self):
        red = self.suite(1)
        self.baseline(session="s1", test_cmd=red)
        self.touch("new docs\n", "README.md")
        out, _ = self.gate(red)
        self.assertEqual((out, self.events()), ({}, []))



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
    def test_committed_docs_only_change_does_not_run_tests(self):
        red = "echo 'FAILED t.py::a'; exit 1"
        self.baseline(red)
        (self.root / "README.md").write_text("better docs\n")
        run(["git", "commit", "-qam", "docs"], cwd=self.root)
        out, _ = self.hook(red)
        self.assertEqual(out, {}, "a committed docs-only change was gated")



class DocsTests(unittest.TestCase):
    def test_docs_promise_only_what_the_gate_does(self):
        text = (KIT / "README.md").read_text() + (KIT / "rules/CLAUDE.md").read_text()
        self.assertNotIn("whole test process tree", text)
        self.assertNotIn("Every attempt is logged", text)

    def test_root_pytest_runs_only_the_kit_tests(self):
        ini = KIT / "pytest.ini"
        self.assertTrue(ini.exists(), "plain `pytest` at the kit root would collect example/ and fail")
        text = ini.read_text()
        self.assertIn("testpaths = tests", text)
        self.assertIn("example", text)



class InstallScopeTests(unittest.TestCase):
    def test_docs_describe_what_uninstall_removes(self):
        readme = (KIT / "README.md").read_text()
        self.assertIn("including your edits inside", readme)



class GateEdgeCasesTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "pkg/mod.py": "y = 1\n",
                              "tests/test_app.py": "def test_a():\n    assert True\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g5-"))
    def env(self, cmd, **extra):
        return {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": cmd, **extra}
    def hook(self, cmd, session="s", timeout=60, **extra):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd, **extra),
                timeout=timeout)
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
    def events(self):
        f = self.root / ".prove-it/gate.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    def test_threat_model_is_stated(self):
        self.assertIn("another local process", (KIT / "README.md").read_text())



class LoggingDocsTests(unittest.TestCase):
    def test_logging_is_described_as_best_effort(self):
        texts = {p: p.read_text() for p in (KIT / "README.md", KIT / "install.sh", KIT / "rules/CLAUDE.md",
                                             KIT / "rules/AGENTS.md")}
        if LANDING.exists():
            texts[LANDING] = LANDING.read_text()
        for path, text in texts.items():
            self.assertIsNone(re.search(r"(?i)every test run (is|gets|logged)|of every test run", text),
                              f"{path.name} still promises that every test run is logged")
        self.assertIn("best-effort", texts[KIT / "README.md"])



if __name__ == "__main__":
    unittest.main()
