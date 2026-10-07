"""Installer: clean-state install, refusals, transaction and rollback, git exclude handling."""
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


class InstallerSafetyTests(unittest.TestCase):
    def test_no_shell_injection_through_target_path(self):
        base = Path(tempfile.mkdtemp(prefix="proveit-f1-"))
        target = base / "repo'; touch PWNED; #"
        target.mkdir()
        run(["git", "init", "-q"], cwd=target)
        r = install(target, cwd=base)
        self.assertFalse((base / "PWNED").exists(), "command in the path was executed")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((target / ".claude/prove-it/pytest_gate.py").exists())

    def test_existing_user_files_are_never_replaced_or_removed(self):
        root = git_repo()
        kitdir = root / ".claude/prove-it"
        kitdir.mkdir(parents=True)
        (kitdir / "pytest_gate.py").write_text("# MY OWN GATE\n")
        (kitdir / "my_script.py").write_text("print('mine')\n")
        r = install(root)
        self.assertNotEqual(r.returncode, 0, "install replaced a user file instead of refusing")
        self.assertEqual((kitdir / "pytest_gate.py").read_text(), "# MY OWN GATE\n")
        install(root, "--uninstall")
        self.assertEqual((kitdir / "pytest_gate.py").read_text(), "# MY OWN GATE\n")
        self.assertTrue((kitdir / "my_script.py").exists(), "uninstall removed a user file")

    def test_symlinks_outside_the_repo_are_refused(self):
        outside = Path(tempfile.mkdtemp(prefix="proveit-outside-"))
        (outside / "shared.md").write_text("OTHER PROJECT RULES\n")
        root = git_repo()
        (root / "CLAUDE.md").symlink_to(outside / "shared.md")
        r = install(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((outside / "shared.md").read_text(), "OTHER PROJECT RULES\n")
        root2 = git_repo()
        (root2 / ".claude").mkdir()
        (root2 / ".claude/prove-it").symlink_to(outside, target_is_directory=True)
        r = install(root2)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["shared.md"])

    def test_user_hooks_that_mention_the_kit_path_are_kept(self):
        root = git_repo()
        (root / ".claude").mkdir()
        mine = {"type": "command", "command": "/usr/local/bin/sec-wrapper --audit /.claude/prove-it/"}
        (root / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [mine]}]}}))
        install(root)
        install(root, "--uninstall")
        self.assertIn("sec-wrapper", (root / ".claude/settings.json").read_text())

    def test_a_second_install_changes_nothing_and_the_original_survives(self):
        root = git_repo({"CLAUDE.md": "ORIGINAL RULES\n"})
        self.assertEqual(install(root).returncode, 0)
        after_first = (root / "CLAUDE.md").read_text()
        r = install(root)
        self.assertNotEqual(r.returncode, 0, "a second install did not refuse")
        self.assertEqual((root / "CLAUDE.md").read_text(), after_first)
        install(root, "--uninstall")
        self.assertEqual((root / "CLAUDE.md").read_text(), "ORIGINAL RULES\n")

    def test_malformed_markers_are_refused(self):
        text = "# Mine\n<!-- prove-it:begin (old, never closed)\nUSER RULE 1\nUSER RULE 2\n"
        root = git_repo({"CLAUDE.md": text})
        r1 = install(root)
        install(root)
        self.assertIn("USER RULE 2", (root / "CLAUDE.md").read_text())
        self.assertNotEqual(r1.returncode, 0, "a file with an unmatched marker was modified")
        self.assertEqual((root / "CLAUDE.md").read_text(), text)



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
    def test_installs_only_the_gate(self):
        self.install()
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(sorted(s["hooks"]), ["SessionStart", "Stop"])
        cmds = [h["command"] for g in s["hooks"].values() for x in g for h in x["hooks"]]
        self.assertTrue(all("pytest_gate.py" in c for c in cmds), cmds)
        self.assertEqual(sorted(p.name for p in (self.root / ".claude/prove-it").iterdir()),
                         ["config.json", "install-manifest.json", "pytest_gate.py"])
        self.assertIn("Done means tests pass", (self.root / "CLAUDE.md").read_text())
        self.assertTrue((self.root / "CLAUDE.md").read_text().startswith("# Mine\n"))
        self.assertFalse((self.root / ".prove-it/backup").exists())       # nothing is replaced, so no backups
        self.assertFalse((self.root / ".prove-it/install.lock").exists())  # the lock is released

    def test_dry_run_writes_nothing(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if ".git" not in p.parts)
        r = run(["bash", INSTALL, str(self.root), "--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if ".git" not in p.parts)
        self.assertEqual(before, after)
        self.assertEqual((self.root / ".claude/settings.json").read_text(), '{"model": "opus"}')

    def test_refused_install_releases_the_lock_and_leaves_no_temp_files(self):
        (self.root / ".claude/settings.json").write_text('{"hooks": "not an object"}')
        r = run(["bash", INSTALL, str(self.root)])
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.root / ".prove-it/install.lock").exists())
        (self.root / ".claude/settings.json").write_text('{"model": "opus"}')
        self.install()
        self.install("--uninstall")
        leftovers = [p for p in self.root.rglob("*prove-it-tmp*")] + [p for p in self.root.rglob(".gate-state.*")]
        self.assertEqual(leftovers, [])

    def test_settings_file_created_by_the_installer_is_removed_again(self):
        root = git_repo({"app.py": "x = 1\n"})
        self.assertEqual(run(["bash", INSTALL, str(root)]).returncode, 0)
        self.assertEqual(run(["bash", INSTALL, str(root), "--uninstall"]).returncode, 0)
        self.assertFalse((root / ".claude/settings.json").exists(), "an empty settings.json was left behind")

    def test_installed_gate_blocks_red_and_passes_green(self):
        self.install()
        stop = next(h["command"] for g in self.settings()["hooks"]["Stop"] for h in g["hooks"])
        env = {"CLAUDE_PROJECT_DIR": str(self.root)}
        (self.root / "app.py").write_text("x = 2\n")
        r = run(stop, cwd=self.root, env={**env, "PROVE_IT_TEST_CMD": "echo 'FAILED t.py::a'; exit 1"},
                stdin='{"session_id": "m"}')
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        r = run(stop, cwd=self.root, env={**env, "PROVE_IT_TEST_CMD": "true"}, stdin='{"session_id": "m"}')
        self.assertEqual(r.stdout, "")
        log = (self.root / ".prove-it/gate-log.md").read_text()
        self.assertIn("BLOCKED", log)
        self.assertIn("PASS", log)

    def test_example_project_is_red_before_the_feature(self):
        """The shipped example must really fail: HALF is not implemented yet."""
        ex = Path(tempfile.mkdtemp()) / "shop"
        shutil.copytree(KIT / "example/shop", ex)
        r = run([shutil.which("python3"), "-c",
                 "import sys; sys.path.insert(0, '.'); from shop.pricing import order_total as t; "
                 "assert t([(150.0, 1)], 'HALF') == 75.0"], cwd=ex)
        self.assertNotEqual(r.returncode, 0)



class CleanStateInstallTests(unittest.TestCase):
    def test_predictable_temp_symlink_never_overwrites_outside_files(self):
        outside = Path(tempfile.mkdtemp()) / "precious.txt"
        outside.write_text("PRECIOUS\n")
        root = git_repo()
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_text("{}\n")
        (root / ".claude/settings.json.prove-it-tmp").symlink_to(outside)
        (root / "CLAUDE.md.prove-it-tmp").symlink_to(outside)
        install(root)
        self.assertEqual(outside.read_text(), "PRECIOUS\n", "an outside file was overwritten through a temp symlink")

    def test_manifest_paths_outside_the_repo_are_never_touched(self):
        root = git_repo()
        r = install(root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        victim_dir = Path(tempfile.mkdtemp())
        victim = victim_dir / "victim.txt"
        victim.write_text("DO NOT DELETE\n")
        manifest = root / ".claude/prove-it/install-manifest.json"
        data = json.loads(manifest.read_text())
        rel = os.path.relpath(victim, root)
        data["files"][rel] = sha("DO NOT DELETE\n")
        data["files"][str(victim)] = sha("DO NOT DELETE\n")
        manifest.write_text(json.dumps(data))
        r = install(root, "--uninstall")
        self.assertTrue(victim.exists(), "uninstall deleted a file outside the repository")
        self.assertNotEqual(r.returncode, 0, "a manifest with outside paths was accepted")

    def test_symlinked_managed_paths_are_refused_inside_the_repo_too(self):
        root = git_repo({"local_gate.py": "# MY LOCAL GATE\n", "AGENTS.md": "# agents\n"})
        (root / ".claude/prove-it").mkdir(parents=True)
        (root / ".claude/prove-it/pytest_gate.py").symlink_to(root / "local_gate.py")
        r = install(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((root / "local_gate.py").read_text(), "# MY LOCAL GATE\n")
        root2 = git_repo({"AGENTS.md": "# agents\n"})
        (root2 / "CLAUDE.md").symlink_to(root2 / "AGENTS.md")
        r = install(root2)
        self.assertNotEqual(r.returncode, 0, "a symlinked rules file was modified")
        self.assertEqual((root2 / "AGENTS.md").read_text(), "# agents\n")

    def test_existing_gate_file_is_refused_not_replaced(self):
        root = git_repo()
        (root / ".claude/prove-it").mkdir(parents=True)
        (root / ".claude/prove-it/pytest_gate.py").write_text("# MY OWN GATE\n")
        r = install(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((root / ".claude/prove-it/pytest_gate.py").read_text(), "# MY OWN GATE\n")

    def test_invalid_settings_schema_is_refused_before_any_write(self):
        root = git_repo()
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_text('{"hooks": {"Stop": [42]}}')
        r = install(root)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists(), "partial install after a schema error")
        self.assertFalse((root / "CLAUDE.md").exists())

    def test_linked_worktree_excludes_prove_it_state(self):
        main = git_repo()
        wt = main.parent / f"{main.name}-wt"
        r = run(["git", "worktree", "add", "-q", str(wt)], cwd=main)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = install(wt)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (wt / ".prove-it/secret-log.txt").write_text("x\n")
        status = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=wt).stdout
        self.assertNotIn(".prove-it/", status, "kit state would be committed from a linked worktree")

    def test_a_running_install_blocks_another(self):
        root = git_repo()
        (root / ".prove-it").mkdir()
        (root / ".prove-it/install.lock").write_text("12345\n")
        r = install(root)
        self.assertNotEqual(r.returncode, 0, "install ignored the lock of another install")
        self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists())
        self.assertIn("lock", (r.stdout + r.stderr).lower())

    def test_incomplete_opening_marker_is_refused(self):
        text = "# Mine\n<!-- prove-it:begin (unfinished\nUSER RULE 1\n<!-- prove-it:end -->\nUSER RULE 2\n"
        root = git_repo({"CLAUDE.md": text})
        r = install(root)
        self.assertEqual((root / "CLAUDE.md").read_text(), text, "user rules inside a broken marker were replaced")
        self.assertNotEqual(r.returncode, 0)



class InstallTransactionTests(unittest.TestCase):
    def test_symlinked_state_paths_are_refused_at_install(self):
        root = git_repo()
        (root / ".prove-it").mkdir()
        (root / ".prove-it/gate-log.md").symlink_to(root / "README.md")
        r = install(root)
        self.assertNotEqual(r.returncode, 0, "install accepted a symlinked gate log")

    def test_unreadable_exclude_is_refused_before_any_write(self):
        root = git_repo()
        exclude = root / ".git/info/exclude"
        exclude.write_bytes(b"\xff\xfe broken \x80\n")
        r = install(root)
        if r.returncode == 0:  # bytes-safe handling is acceptable too, but then the file must stay intact
            self.assertTrue(exclude.read_bytes().startswith(b"\xff\xfe broken \x80\n"))
        else:
            self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists(), "partial install")

    def test_failed_write_rolls_everything_back(self):
        root = git_repo({"CLAUDE.md": "ORIGINAL\n"})
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_text('{"model": "x"}\n')
        r = install(root, env={"PROVE_IT_INSTALL_FAULT_AFTER": "3"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((root / "CLAUDE.md").read_text(), "ORIGINAL\n")
        self.assertEqual((root / ".claude/settings.json").read_text(), '{"model": "x"}\n')
        self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists())
        self.assertFalse((root / ".claude/prove-it/install-manifest.json").exists())
        self.assertEqual(install(root).returncode, 0, "a clean retry is possible after the rollback")

    def test_refused_install_leaves_no_state_folder(self):
        root = git_repo()
        (root / ".claude").mkdir()
        (root / ".claude/settings.json").write_text("[not json")
        install(root)
        self.assertFalse((root / ".prove-it").exists(), "a refused install left .prove-it/ behind")

    def test_install_outside_the_git_top_level_is_refused(self):
        root = git_repo({"pkg/app.py": "x = 1\n"})
        r = install(root / "pkg")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((root / "pkg/.claude").exists())

    def test_negated_exclude_is_detected(self):
        root = git_repo({".gitignore": "!.prove-it/\n!.prove-it/**\n"})
        (root / ".git/info/exclude").write_text(".prove-it/\n")
        r = install(root)
        if r.returncode == 0:
            (root / ".prove-it/gate.jsonl").write_text("{}\n")
            status = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root).stdout
            self.assertNotIn(".prove-it/", status, "install reported success but the gate log would be committed")

    def test_tracked_state_files_are_refused(self):
        root = git_repo({".prove-it/gate.jsonl": "{}\n"})
        r = install(root)
        self.assertNotEqual(r.returncode, 0, "install ignored tracked .prove-it/ files")



class InstallScopeTests(unittest.TestCase):
    def test_installer_output_promises_test_runs_only(self):
        root = git_repo()
        r = run(["bash", INSTALL, str(root)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Every attempt", r.stdout)

    def test_folder_outside_git_is_refused(self):
        root = Path(tempfile.mkdtemp(prefix="proveit-nogit-"))
        r = run(["bash", INSTALL, str(root)])
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((root / ".claude").exists())
        self.assertFalse((root / ".prove-it").exists())



class InstallOrderTests(unittest.TestCase):
    def test_exclude_is_in_place_before_the_gate_and_hooks(self):
        root = git_repo()
        run(["bash", INSTALL, str(root)], env={"PROVE_IT_INSTALL_CRASH_AFTER": "2"})
        exclude = (root / ".git/info/exclude").read_text()
        self.assertIn(".prove-it/", exclude, "the gate could be installed before its logs were excluded from git")
        self.assertFalse((root / ".claude/prove-it/pytest_gate.py").exists())



if __name__ == "__main__":
    unittest.main()
