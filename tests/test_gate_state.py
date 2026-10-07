"""Gate state: baselines, the green cache, red results and concurrent sessions."""
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
    def test_green_allows_and_is_cached(self):
        self.touch()
        out, _ = self.gate(self.suite(0))
        self.assertEqual(out, {})
        self.gate(self.suite(0))
        self.assertEqual([e["decision"] for e in self.events()], ["pass"])  # second stop: same files, no rerun
        self.assertIn("PASS", (self.root / ".prove-it/gate-log.md").read_text())

    def test_red_blocks_twice_then_gives_up_visibly(self):
        self.touch()
        red = self.suite(1, "FAILED tests/test_app.py::test_f - assert 2 == 1\n1 failed")
        out1, _ = self.gate(red)
        self.assertEqual(out1["decision"], "block")
        self.assertIn("tests/test_app.py::test_f", out1["reason"])
        self.assertIn("fix cycle 1 of 2", out1["reason"])
        out2, _ = self.gate(red)
        self.assertIn("fix cycle 2 of 2", out2["reason"])
        out3, _ = self.gate(red)
        self.assertNotIn("decision", out3)
        self.assertIn("NOT verified", out3["systemMessage"])
        out4, _ = self.gate(red)
        self.assertEqual(out4["decision"], "block")                 # a new attempt gets fresh cycles
        self.assertEqual([e["decision"] for e in self.events()], ["block", "block", "gave_up", "block"])

    def test_fix_after_block_resets_cycles(self):
        self.touch()
        self.gate(self.suite(1, "FAILED t.py::a"))
        self.touch("def f():\n    return 3\n")
        out, _ = self.gate(self.suite(0))
        self.assertEqual(out, {})
        self.touch("def f():\n    return 4\n")
        out, _ = self.gate(self.suite(1, "FAILED t.py::a"))
        self.assertIn("fix cycle 1 of 2", out["reason"])

    def test_sessions_are_counted_separately(self):
        self.touch()
        red = self.suite(1, "FAILED t.py::a")
        self.gate(red, session="a")
        self.gate(red, session="a")
        out, _ = self.gate(red, session="b")
        self.assertIn("fix cycle 1 of 2", out["reason"])

    def test_committing_red_code_does_not_bypass_the_gate(self):
        self.baseline()
        self.touch()
        run(["git", "commit", "-qam", "agent commits its work"], cwd=self.root)
        out, _ = self.gate(self.suite(1, "FAILED t.py::a"))
        self.assertEqual(out["decision"], "block")

    def test_chat_only_session_is_not_gated_even_if_tests_are_red(self):
        self.touch()                                    # pre-existing uncommitted work, tests red
        red = self.suite(1, "FAILED t.py::a")
        self.baseline(test_cmd=red)
        out, _ = self.gate(red)
        self.assertEqual((out, self.events()), ({}, []))

    def test_baseline_is_kept_on_resume_and_compaction(self):
        self.baseline()
        self.touch()
        self.baseline()                                 # SessionStart fires again after compaction
        out, _ = self.gate(self.suite(1, "FAILED t.py::a"))
        self.assertEqual(out["decision"], "block")



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
    def test_changing_the_test_command_invalidates_the_green_cache(self):
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook("true")
        self.assertEqual(out, {})
        out, _ = self.hook("echo 'FAILED t.py::a'; exit 1")
        self.assertEqual(out.get("decision"), "block")

    def test_config_errors_never_look_green(self):
        cfg = self.root / ".claude/prove-it/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"test_cmd": "true", "timeout": "five minutes"}))
        r = self.check()
        self.assertNotEqual(r.returncode, 0, "--check reported success with a broken config")
        (self.root / "app.py").write_text("x = 2\n")
        out, r = self.hook_no_env()
        self.assertIn("NOT verified", out.get("systemMessage", ""), "the hook allowed completion silently")
        cfg.write_text("[1, 2]")
        self.assertNotEqual(self.check().returncode, 0)



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
    def test_manual_check_is_never_silently_green_when_the_hook_is_disabled(self):
        r = self.check("echo 'FAILED t.py::a'; exit 1", PROVE_IT_GATE_DISABLE="1")
        self.assertNotEqual(r.returncode, 0, "--check returned success without running the tests")

    def test_a_red_check_invalidates_a_cached_green(self):
        flag = self.bin / "service-down"
        suite = fake_cli(self.bin, "suite.py", f"""
            import pathlib, sys
            if pathlib.Path({str(flag)!r}).exists():
                print("FAILED tests/test_api.py::test_call"); sys.exit(1)
            print("1 passed")
        """)
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(suite)
        self.assertEqual(out, {})                       # green, cached for this state
        flag.write_text("x")                            # same code, the suite now fails
        self.assertEqual(self.check(suite).returncode, 1)
        out, _ = self.hook(suite)
        self.assertEqual(out.get("decision"), "block", "a known-red state was allowed from the green cache")



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
    def test_an_old_snapshot_cannot_resurrect_green(self):
        spec = importlib.util.spec_from_file_location("pg", GATE)
        pg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pg)
        a = pg.State(self.root, "a")
        a.s["last_green"] = "fpX"
        a.save()
        old = pg.State(self.root, "y")            # loads a snapshot that still says green
        manual = pg.State(self.root, "manual")
        manual.forget_green("fpX")
        manual.save()
        old.save()                                  # the slow run finishes later
        fresh = pg.State(self.root, "a")
        known_red = getattr(fresh, "known_red", lambda: set())()
        self.assertTrue(fresh.s.get("last_green") != "fpX" or "fpX" in known_red,
                        "an older snapshot brought back a green result for a known-red state")

    def test_missing_baseline_runs_the_tests(self):
        (self.root / "app.py").write_text("x = 3\n")
        run(["git", "commit", "-qam", "agent commits"], cwd=self.root)   # clean tree, no baseline recorded
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "without a baseline, committed code was not gated")



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
    def test_a_slow_pass_cannot_override_a_newer_red(self):
        flag = self.bin / "broken"
        suite = fake_cli(self.bin, "suite.py", f"""
            import pathlib, sys, time
            if pathlib.Path({str(flag)!r}).exists():
                print("FAILED tests/test_app.py::test_a"); sys.exit(1)
            time.sleep(3)
        """)
        (self.root / "app.py").write_text("x = 2\n")
        slow = subprocess.Popen([PY, GATE], cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                env={**os.environ, **self.env(suite)})
        slow.stdin.write('{"session_id": "A"}')
        slow.stdin.close()
        time.sleep(1.0)
        flag.write_text("x")                                                  # same files, the suite now fails
        r = run([PY, GATE, "--check"], cwd=self.root, env=self.env(suite))
        self.assertEqual(r.returncode, 1)
        slow_out = slow.stdout.read()
        slow.wait(timeout=30)
        nxt, _ = self.hook(suite, session="A")
        self.assertTrue("NOT verified" in slow_out or nxt.get("decision") == "block",
                        "a pass that started before a newer red run was cached as green")
        self.assertEqual(nxt.get("decision"), "block", "the known-red state was allowed from a stale green")



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
    def test_red_history_survives_a_later_green(self):
        pg = load_gate()
        a = pg.State(self.root, "A")                       # slow pass starts at generation 0
        b = pg.State(self.root, "B")
        b.forget_green("fp1")
        b.save()                                           # a newer red of the same state
        c = pg.State(self.root, "C")                       # starts after the red, passes, clears known-red
        c.mark_green("fp1")
        self.assertTrue(c.save())
        a.mark_green("fp1")
        self.assertFalse(a.save(), "a pass that began before a newer red was accepted after the red was cleared")



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
    def test_latest_baseline_wins_within_the_same_second(self):
        self.baseline("true", session="a")
        original = (self.root / "tests/test_app.py").read_text()
        (self.root / "tests/test_app.py").write_text("def test_a():\n    assert 2\n")
        self.baseline("true", session="b")                         # recorded after a, often in the same second
        (self.root / "tests/test_app.py").write_text(original)    # matches a, differs from b
        r = run([PY, GATE, "--check"], cwd=self.root, env=self.env("true"))
        self.assertIn("tests/test_app.py", r.stdout, "--check compared against an older baseline")



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
    def test_red_is_persisted_even_if_the_post_run_check_fails(self):
        flag = self.bin / "broken"
        index = self.root / ".git/index"
        suite = fake_cli(self.bin, "suite.py", f"""
            import os, pathlib, sys
            if pathlib.Path({str(flag)!r}).exists():
                os.chmod({str(index)!r}, 0)          # makes the post-run snapshot fail
                print("FAILED t.py::a"); sys.exit(1)
        """)
        (self.root / "app.py").write_text("x = 2\n")
        self.assertEqual(self.hook(suite)[0], {})                       # cached green for this state
        runs_before = len([e for e in self.events() if e.get("decision") in ("pass", "block")])
        flag.write_text("x")
        run([PY, GATE, "--check"], cwd=self.root, env=self.env(suite))
        os.chmod(index, 0o644)
        flag.unlink()
        out, _ = self.hook(suite)                                   # same state as the cached green
        runs_after = len([e for e in self.events() if e.get("decision") in ("pass", "block")])
        self.assertEqual(runs_after, runs_before + 1,
                         "the stop reused the cached green although a run from this state had failed")



if __name__ == "__main__":
    unittest.main()
