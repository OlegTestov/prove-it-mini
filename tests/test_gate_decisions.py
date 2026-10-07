"""Gate decisions: when the Stop hook blocks, allows, gives up or reports NOT verified."""
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
    def test_no_tests_collected_warns_without_blocking(self):
        self.touch()
        fake_cli(self.bin, "pytest", "import sys\nprint('no tests ran')\nsys.exit(5)\n")
        out, _ = self.gate(f"{self.bin / 'pytest'} -q")  # exit 5 means "no tests" only for a direct pytest call
        self.assertNotIn("decision", out)
        self.assertIn("no tests", out["systemMessage"])

    def test_manual_check_exit_codes(self):
        self.touch()
        _, r = self.gate(self.suite(1, "FAILED m.py::t"), args=("--check",))
        self.assertEqual(r.returncode, 1)
        self.assertIn('"decision": "block"', r.stdout)

    def test_manual_check_runs_on_a_clean_tree_and_is_not_a_catch(self):
        _, r = self.gate(self.suite(1, "FAILED m.py::t"), args=("--check",))
        self.assertEqual(r.returncode, 1)
        _, r = self.gate(self.suite(1, "FAILED m.py::t"), args=("--check",))
        self.assertEqual([e["decision"] for e in self.events()], ["check", "check"])
        self.assertIn("CHECK", (self.root / ".prove-it/gate-log.md").read_text())
        _, r = self.gate(self.suite(0), args=("--check",))
        self.assertEqual(r.returncode, 0)

    @unittest.skipUnless(importlib.util.find_spec("pytest"), "pytest not installed for this interpreter")
    def test_with_real_pytest(self):
        self.touch()
        (self.root / "test_app.py").write_text("import app\n\ndef test_f():\n    assert app.f() == 1\n")
        out, _ = self.gate(f"{PY} -m pytest -q")
        self.assertIn("test_app.py::test_f", out["reason"])



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
    def test_manual_check_always_runs_the_tests(self):
        self.assertEqual(self.check("true").returncode, 0)
        r = self.check("echo 'FAILED t.py::a'; exit 1")
        self.assertEqual(r.returncode, 1, "a stale green result was reused for a different test command")

    def test_gate_records_test_content_hash(self):
        (self.root / "tests").mkdir()
        (self.root / "tests/test_app.py").write_text("def test_a():\n    assert False\n")
        self.hook("echo 'FAILED tests/test_app.py::test_a'; exit 1")
        (self.root / "tests/test_app.py").write_text("def test_a():\n    assert True\n")
        self.hook("true")
        events = [json.loads(x) for x in (self.root / ".prove-it/gate.jsonl").read_text().splitlines()]
        hashes = [e.get("tests_hash") for e in events]
        self.assertTrue(all(hashes), "test-content hash is not recorded")
        self.assertNotEqual(hashes[0], hashes[1])



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
    def test_exit_5_means_no_tests_only_for_pytest(self):
        (self.root / "app.py").write_text("x = 2\n")
        runner = fake_cli(self.bin, "runner.py", "import sys\nprint('FAILED suite::a')\nsys.exit(5)\n")
        out, _ = self.hook(runner)
        self.assertEqual(out.get("decision"), "block", "a non-pytest exit 5 was treated as 'no tests'")



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
    def test_compound_command_exit_5_is_a_failure(self):
        (self.root / "app.py").write_text("x = 2\n")
        five = fake_cli(self.bin, "five.py", "import sys\nprint('FAILED integration')\nsys.exit(5)\n")
        out, _ = self.hook(f"true pytest && {five}")
        self.assertEqual(out.get("decision"), "block", "a compound command's exit 5 was read as 'no tests'")

    def test_sigterm_kills_the_test_process_group(self):
        marker = self.bin / "late-write"
        slow = fake_cli(self.bin, "slow.py", f"""
            import subprocess, sys
            subprocess.Popen([sys.executable, "-c", "import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('x')"])
            import time; time.sleep(30)
        """)
        (self.root / "app.py").write_text("x = 2\n")
        p = subprocess.Popen([PY, GATE], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env={**os.environ, **self.env(slow)})
        p.stdin.write('{"session_id": "sig"}')
        p.stdin.close()
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "a test child kept running after the hook was terminated")



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
    def test_one_deadline_for_the_whole_gate(self):
        editor = fake_cli(self.bin, "slow_editor.py", """
            import pathlib, time
            time.sleep(1.5)
            p = pathlib.Path("app.py"); p.write_text(p.read_text() + "# touched\\n")
        """)
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(editor, PROVE_IT_GATE_DEADLINE="2.5")
        self.assertIn("no time for a rerun", out.get("systemMessage", ""))



class GateDeadlineAndBaselinesTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "pkg/mod.py": "y = 1\n",
                              "tests/test_app.py": "def test_a():\n    assert True\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g6-"))
    def env(self, cmd, **extra):
        return {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": cmd, **extra}
    def hook(self, cmd, session="s", **extra):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd, **extra))
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
    def test_failed_run_invalidates_starting_fingerprint(self):
        flag = self.bin / "broken"
        suite = fake_cli(self.bin, "suite.py", f"""
            import pathlib, sys
            if pathlib.Path({str(flag)!r}).exists():
                p = pathlib.Path("app.py"); p.write_text(p.read_text() + "# touched by a failing run\\n")
                print("FAILED t.py::a"); sys.exit(1)
        """)
        (self.root / "app.py").write_text("x = 2\n")             # state X
        self.assertEqual(self.hook(suite)[0], {})                 # X is green and cached
        flag.write_text("x")
        self.assertEqual(run([PY, GATE, "--check"], cwd=self.root, env=self.env(suite)).returncode, 1)
        (self.root / "app.py").write_text("x = 2\n")             # back to X's bytes
        out, _ = self.hook(suite)
        self.assertEqual(out.get("decision"), "block", "X stayed green although a run starting from X failed")



if __name__ == "__main__":
    unittest.main()
