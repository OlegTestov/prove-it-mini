"""Gate log and messages: what is recorded and which changed test files are named."""
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
    def test_editing_tests_is_flagged(self):
        red = self.suite(1, "FAILED tests/test_app.py::test_x")
        self.baseline(session="s1", test_cmd=red)
        self.touch()
        self.touch("def test_x():\n    assert False\n", "tests/test_app.py")
        out, _ = self.gate(red)
        self.assertIn("Do not weaken, skip or delete", out["reason"])
        self.assertEqual(self.events()[-1]["tests_touched"], ["tests/test_app.py"])



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
    def test_runtime_log_symlink_is_never_followed(self):
        victim = self.root / "settings-victim.json"
        victim.write_text('{"keep": true}\n')
        (self.root / ".prove-it").mkdir()
        (self.root / ".prove-it/gate-log.md").symlink_to(victim)
        (self.root / "app.py").write_text("x = 2\n")
        self.hook(RED)
        self.assertEqual(victim.read_text(), '{"keep": true}\n', "the gate wrote through a symlinked log")

    def test_committed_test_edits_are_flagged(self):
        self.baseline(RED)
        (self.root / "tests/test_app.py").write_text("def test_a():\n    pass  # weakened\n")
        (self.root / "app.py").write_text("x = 2\n")
        run(["git", "commit", "-qam", "weaken"], cwd=self.root)
        out, _ = self.hook(RED)
        self.assertIn("tests/test_app.py", out.get("reason", ""), "a committed test edit was not flagged")

    def test_deleted_tests_are_flagged(self):
        self.baseline(RED)
        (self.root / "tests/test_app.py").unlink()
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(RED)
        self.assertIn("tests/test_app.py", out.get("reason", ""))



class GateConcurrencyAndPathsTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "tests/test_app.py": "def test_a():\n    assert True\n",
                              ".gitignore": "local.cfg\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g4-"))
    def env(self, cmd, **extra):
        return {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": cmd, **extra}
    def hook(self, cmd, session="s", **extra):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd, **extra))
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
    def events(self, root=None):
        f = (root or self.root) / ".prove-it/gate.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    def test_test_edit_warning_on_a_passing_run(self):
        self.baseline("true")
        (self.root / "tests/test_app.py").write_text("def test_a():\n    pass  # weakened\n")
        out, _ = self.hook("true")
        self.assertIn("tests/test_app.py", out.get("systemMessage", ""), "a weakened test that passes went unmentioned")

    def test_test_edit_warning_on_give_up(self):
        self.baseline(RED)
        (self.root / "tests/test_app.py").write_text("def test_a():\n    assert 1 == 2\n")
        outs = [self.hook(RED)[0] for _ in range(3)]
        self.assertIn("tests/test_app.py", outs[2].get("systemMessage", ""))

    def test_interrupted_runs_are_logged(self):
        slow = fake_cli(self.bin, "slow.py", "import time\ntime.sleep(30)\n")
        (self.root / "app.py").write_text("x = 2\n")
        p = subprocess.Popen([PY, GATE], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env={**os.environ, **self.env(slow)})
        p.stdin.write('{"session_id": "int"}')
        p.stdin.close()
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        self.assertEqual([e["decision"] for e in self.events()][-1:], ["interrupted"])

    def test_edits_behind_a_symlink_are_seen(self):
        (self.root / "local.cfg").write_text("value = 1\n")
        (self.root / "settings.py").symlink_to("local.cfg")
        run(["git", "add", "settings.py"], cwd=self.root)
        run(["git", "commit", "-qm", "link"], cwd=self.root)
        self.baseline(RED)
        (self.root / "local.cfg").write_text("value = 'broken'\n")
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "an edit behind a tracked symlink was not seen")



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
    def test_manual_check_reports_test_edits_against_the_session_baseline(self):
        self.baseline("true", session="agent")
        (self.root / "tests/test_app.py").write_text("def test_a():\n    pass  # weakened\n")
        r = run([PY, GATE, "--check"], cwd=self.root, env=self.env("true"))
        self.assertIn("tests/test_app.py", r.stdout + r.stderr, "--check ignored the session's baseline")

    def test_test_edits_made_by_a_failing_run_are_reported(self):
        self.baseline(RED)
        deleter = fake_cli(self.bin, "deleter.py", """
            import pathlib, sys
            pathlib.Path("tests/test_app.py").unlink()
            print("FAILED x"); sys.exit(1)
        """)
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(deleter)
        self.assertIn("tests/test_app.py", out.get("reason", ""))

    def test_every_attempt_is_logged(self):
        once = self.bin / "once"
        editor = fake_cli(self.bin, "editor_once.py", f"""
            import pathlib
            flag = pathlib.Path({str(once)!r})
            if not flag.exists():
                flag.write_text("x"); p = pathlib.Path("app.py"); p.write_text(p.read_text() + "# formatted\\n")
        """)
        (self.root / "app.py").write_text("x = 2\n")
        self.hook(editor)
        runs = [e for e in self.events() if "exit" in e]
        self.assertEqual(len(runs), 2, "the first (discarded) test run was not logged")
        self.assertTrue(all("fingerprint" in e and "seconds" in e for e in runs))



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
    def test_no_attempt_starts_after_the_deadline(self):
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook("true", PROVE_IT_GATE_DEADLINE="0.0001")
        self.assertIn("no time", out.get("systemMessage", ""))
        log = self.root / ".prove-it/gate.jsonl"
        runs = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        self.assertEqual([e for e in runs if "exit" in e], [], "a test run started after the deadline")

    def test_cached_pass_still_names_changed_tests(self):
        self.baseline("true")
        (self.root / "tests/test_app.py").write_text("def test_a():\n    pass  # weakened\n")
        first, _ = self.hook("true")
        second, _ = self.hook("true")                              # cached green: no new run
        self.assertIn("tests/test_app.py", first.get("systemMessage", ""))
        self.assertIn("tests/test_app.py", second.get("systemMessage", ""), "the cached pass dropped the warning")



class GateRedStateTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "pkg/mod.py": "y = 1\n",
                              "tests/test_app.py": "def test_a():\n    assert True\n", "tests/cases.txt": "1 2\n"})
        self.bin = Path(tempfile.mkdtemp(prefix="proveit-g7-"))
    def env(self, cmd, **extra):
        return {"CLAUDE_PROJECT_DIR": str(self.root), "PROVE_IT_TEST_CMD": cmd, **extra}
    def hook(self, cmd, session="s"):
        r = run([PY, GATE], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
        return (json.loads(r.stdout) if r.stdout.strip() else {}), r
    def baseline(self, cmd, session="s"):
        run([PY, GATE, "--baseline"], cwd=self.root, stdin=json.dumps({"session_id": session}), env=self.env(cmd))
    def events(self):
        f = self.root / ".prove-it/gate.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
    def test_interruption_record_is_complete(self):
        slow = fake_cli(self.bin, "slow.py", """
            import pathlib, time
            pathlib.Path("tests/test_app.py").unlink()
            time.sleep(30)
        """)
        self.baseline(slow)
        (self.root / "app.py").write_text("x = 2\n")
        p = subprocess.Popen([PY, GATE], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             env={**os.environ, **self.env(slow)})
        p.stdin.write('{"session_id": "s"}')
        p.stdin.close()
        time.sleep(1.2)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
        rec = [e for e in self.events() if e.get("decision") == "interrupted"][-1]
        self.assertEqual(rec.get("exit"), 128 + signal.SIGTERM)
        self.assertIn("seconds", rec)
        self.assertIn("tests/test_app.py", rec.get("tests_touched", []))

    def test_unchanged_watched_state_still_names_changed_test_files(self):
        self.baseline("true")
        (self.root / "tests/cases.txt").write_text("1 3\n")              # a test fixture outside the watch globs
        out, _ = self.hook("true")
        self.assertIn("tests/cases.txt", out.get("systemMessage", ""))



if __name__ == "__main__":
    unittest.main()
