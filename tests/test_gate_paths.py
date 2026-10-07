"""Gate fingerprint and paths: which file changes count, symlinks, special files."""
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
    def test_no_change_means_no_test_run(self):
        red = self.suite(1)
        self.baseline(session="s1", test_cmd=red)      # SessionStart records the baseline
        out, _ = self.gate(red)
        self.assertEqual(out, {})
        self.assertEqual(self.events(), [])

    def test_relevant_ignores_tool_and_cache_dirs(self):
        files = ["app.py", ".claude/prove-it/x.py", "pkg/__pycache__/a.py", ".venv/lib/x.py", "docs/a.md", "pyproject.toml"]
        self.assertEqual(pytest_gate.relevant(files, pytest_gate.DEFAULT_WATCH), ["app.py", "pyproject.toml"])



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
    def test_non_ascii_python_file_names_are_gated(self):
        (self.root / "café.py").write_text("x = 1\n")
        run(["git", "add", "café.py"], cwd=self.root)
        run(["git", "commit", "-qm", "add"], cwd=self.root)
        (self.root / "café.py").write_text("x = 2\n")
        out, _ = self.hook("echo 'FAILED t.py::a'; exit 1")
        self.assertEqual(out.get("decision"), "block", "an edited café.py bypassed the gate")



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
    def test_assume_unchanged_files_are_still_seen(self):
        run(["git", "update-index", "--assume-unchanged", "app.py"], cwd=self.root)
        self.baseline(RED)
        (self.root / "app.py").write_text("x = 'edited'\n")
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "an edit to an assume-unchanged file was not seen")

    def test_staging_identical_bytes_does_not_rerun(self):
        (self.root / "app.py").write_text("x = 2\n")
        self.hook("true")
        run(["git", "add", "app.py"], cwd=self.root)
        self.hook("true")
        self.assertEqual(len([e for e in self.events() if e["decision"] == "pass"]), 1, "staging forced a rerun")



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
    def test_state_dir_swapped_during_the_run_is_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix="proveit-outside-"))
        swap = fake_cli(self.bin, "swap.py", f"""
            import os
            os.rename(".prove-it", ".prove-it-moved")
            os.symlink({str(outside)!r}, ".prove-it")
        """)
        (self.root / ".prove-it").mkdir()
        (self.root / "app.py").write_text("x = 2\n")
        self.hook(swap)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [], "the gate wrote into the swapped-in directory")

    def test_files_changed_during_the_run_are_not_verified(self):
        editor = fake_cli(self.bin, "editor.py", """
            import pathlib
            p = pathlib.Path("app.py"); p.write_text(p.read_text() + "# touched by the run\\n")
        """)
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(editor)
        self.assertIn("changed during the test run", out.get("systemMessage", ""))
        self.assertEqual(len([e for e in self.events() if e["decision"] == "pass"]), 0)

    def test_one_change_then_stable_is_verified_after_one_rerun(self):
        once = self.bin / "once"
        editor = fake_cli(self.bin, "editor_once.py", f"""
            import pathlib
            flag = pathlib.Path({str(once)!r})
            if not flag.exists():
                flag.write_text("x"); p = pathlib.Path("app.py"); p.write_text(p.read_text() + "# formatted\\n")
        """)
        (self.root / "app.py").write_text("x = 2\n")
        out, _ = self.hook(editor)
        self.assertEqual(out, {})
        self.assertEqual(self.events()[-1]["decision"], "pass")

    def test_symlink_outside_the_repo_is_never_trusted(self):
        outside = Path(tempfile.mkdtemp()) / "shared.py"
        outside.write_text("x = 1\n")
        (self.root / "shared.py").symlink_to(outside)
        run(["git", "add", "shared.py"], cwd=self.root)
        run(["git", "commit", "-qm", "link"], cwd=self.root)
        self.baseline(RED)
        out, _ = self.hook(RED)                       # nothing changed, but the target cannot be vouched for
        self.assertEqual(out.get("decision"), "block")



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
    def test_files_reached_through_a_symlinked_folder_are_never_trusted(self):
        outside = Path(tempfile.mkdtemp(prefix="proveit-outside-")) / "pkg"
        shutil.move(str(self.root / "pkg"), str(outside))
        (self.root / "pkg").symlink_to(outside, target_is_directory=True)
        self.baseline(RED)
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "a file behind a symlinked folder outside the repo was trusted")

    def test_special_state_files_never_hang_the_gate(self):
        (self.root / ".prove-it").mkdir()
        os.mkfifo(self.root / ".prove-it/gate-state.json")
        (self.root / "app.py").write_text("x = 2\n")
        try:
            out, _ = self.hook("true", timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("the gate hung on a FIFO in .prove-it/")
        self.assertIn("NOT verified", out.get("systemMessage", ""))



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
    def test_missing_file_behind_an_outside_symlink_is_never_trusted(self):
        outside = Path(tempfile.mkdtemp(prefix="proveit-out-")) / "pkg"
        outside.mkdir()                                            # mod.py is absent there
        shutil.rmtree(self.root / "pkg")
        (self.root / "pkg").symlink_to(outside, target_is_directory=True)
        self.baseline(RED)
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "a missing file behind an outside symlink looked 'deleted'")



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
    def test_dangling_parent_symlink_inside_the_repo_is_unverifiable(self):
        import shutil
        shutil.rmtree(self.root / "pkg")
        (self.root / "pkg").symlink_to(self.root / "nowhere", target_is_directory=True)
        self.baseline(RED)
        out, _ = self.hook(RED)
        self.assertEqual(out.get("decision"), "block", "a dangling parent symlink was treated as a stable deletion")



if __name__ == "__main__":
    unittest.main()
