"""Uninstall: manifest-driven removal that only touches what the kit installed."""
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


class InstalledKitTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"CLAUDE.md": "# Mine\n", "app.py": "x = 1\n"})
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.json").write_text('{"model": "opus"}')
    def install(self, *args):
        r = run(["bash", INSTALL, str(self.root), *args])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r
    def settings(self):
        return json.loads((self.root / ".claude/settings.json").read_text())
    def test_second_install_refuses_and_uninstall_restores(self):
        self.install()
        before = (self.root / ".claude/settings.json").read_text(), (self.root / "CLAUDE.md").read_text()
        r = run(["bash", INSTALL, str(self.root)])
        self.assertEqual(r.returncode, 2)
        self.assertIn("--uninstall, then install", r.stderr + r.stdout)
        self.assertEqual(before, ((self.root / ".claude/settings.json").read_text(), (self.root / "CLAUDE.md").read_text()))
        self.install("--uninstall")
        self.assertEqual(self.settings(), {"model": "opus"})
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Mine\n")
        self.assertFalse((self.root / "AGENTS.md").exists())

    def test_uninstall_keeps_a_gate_file_the_user_edited(self):
        self.install()
        gate = self.root / ".claude/prove-it/pytest_gate.py"
        gate.write_text(gate.read_text() + "# my tweak\n")
        r = run(["bash", INSTALL, str(self.root), "--uninstall"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(gate.exists())
        self.assertIn("it changed after installation", r.stdout)



class InstallTransactionTests(unittest.TestCase):
    def test_uninstall_leaves_blocks_it_did_not_install(self):
        custom = "# Agents\n<!-- prove-it:begin (my own copy) -->\nMY CUSTOM BLOCK\n<!-- prove-it:end -->\n"
        root = git_repo({"AGENTS.md": custom})
        r = install(root, "--no-codex")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        install(root, "--uninstall")
        self.assertEqual((root / "AGENTS.md").read_text(), custom, "uninstall removed a block it never installed")

    def test_uninstall_without_manifest_refuses(self):
        text = "# Mine\n<!-- prove-it:begin (x) -->\nKEEP\n<!-- prove-it:end -->\n"
        root = git_repo({"CLAUDE.md": text})
        r = install(root, "--uninstall")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((root / "CLAUDE.md").read_text(), text)



class InterruptedInstallTests(unittest.TestCase):
    def test_crashed_install_is_recognised_and_uninstallable(self):
        root = git_repo({"CLAUDE.md": "# Mine\n"})
        r = run(["bash", INSTALL, str(root)], env={"PROVE_IT_INSTALL_CRASH_AFTER": "3"})
        self.assertNotEqual(r.returncode, 0)
        manifest = root / ".claude/prove-it/install-manifest.json"
        self.assertTrue(manifest.exists(), "no manifest was written before the managed files")
        self.assertEqual(json.loads(manifest.read_text()).get("status"), "pending")
        (root / ".prove-it/install.lock").unlink(missing_ok=True)  # the crashed process left its lock
        r = run(["bash", INSTALL, str(root)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("interrupted", r.stdout + r.stderr)
        r = run(["bash", INSTALL, str(root), "--uninstall"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists())
        self.assertFalse(manifest.exists())
        self.assertEqual((root / "CLAUDE.md").read_text(), "# Mine\n")
        self.assertEqual(run(["bash", INSTALL, str(root)]).returncode, 0, "a clean install is possible afterwards")



if __name__ == "__main__":
    unittest.main()
