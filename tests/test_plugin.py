"""Claude Code plugin: marketplace layout, opt-in hooks, and the setup / teardown skills."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from helpers import KIT, PY, TOOLS, git_repo, run

HELPERS = str(TOOLS / "install_helpers.py")
INSTALL = str(KIT / "install.sh")
RED = "echo 'FAILED tests/test_app.py::test_a'; exit 1"


def plugin_hooks() -> dict:
    data = json.loads((KIT / "hooks/hooks.json").read_text())
    return {event: groups[0]["hooks"][0]["command"] for event, groups in data["hooks"].items()}


def run_hook(command: str, root: Path, test_cmd: str = RED, session: str = "p"):
    env = {"CLAUDE_PLUGIN_ROOT": str(KIT), "CLAUDE_PROJECT_DIR": str(root), "PROVE_IT_TEST_CMD": test_cmd}
    return run(["bash", "-c", command], cwd=root, stdin=json.dumps({"session_id": session}), env=env)


def assert_silent(case: unittest.TestCase, r, why: str = "") -> None:
    """A no-op hook: exit 0 AND empty stdout (a crashing hook is also silent on stdout)."""
    case.assertEqual((r.returncode, r.stdout), (0, ""), f"{why}\nstderr: {r.stderr}")


def setup(root: Path, *args, env=None):
    return run([PY, HELPERS, "setup", "--kit", str(KIT), "--target", str(root), *args], env=env)


def teardown(root: Path, *args):
    return run([PY, HELPERS, "teardown", "--kit", str(KIT), "--target", str(root), *args])


class PluginLayoutTests(unittest.TestCase):
    def test_marketplace_lists_the_plugin_at_the_repo_root(self):
        m = json.loads((KIT / ".claude-plugin/marketplace.json").read_text())
        p = json.loads((KIT / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(m["name"], "prove-it-mini")
        self.assertTrue(m["owner"]["name"])
        self.assertEqual([e["name"] for e in m["plugins"]], ["prove-it-mini"])
        self.assertEqual(m["plugins"][0]["source"], "./")
        self.assertEqual(p["name"], "prove-it-mini")

    def test_plugin_hooks_call_the_shared_gate_in_plugin_mode(self):
        hooks = plugin_hooks()
        self.assertEqual(sorted(hooks), ["SessionStart", "Stop"])
        for cmd in hooks.values():
            self.assertIn('"${CLAUDE_PLUGIN_ROOT}/tools/pytest_gate.py"', cmd)
            self.assertIn("--plugin", cmd)
        self.assertIn("--baseline", hooks["SessionStart"])

    def test_setup_teardown_and_check_are_user_invoked_skills(self):
        for name in ("setup", "teardown", "check"):
            text = (KIT / f"skills/{name}/SKILL.md").read_text()
            front = text.split("---")[1]
            self.assertIn(f"name: {name}", front)
            self.assertIn("disable-model-invocation: true", front)
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", text)


    def test_skills_tell_the_agent_to_run_their_command_with_bash(self):
        for name in ("setup", "teardown", "check"):
            text = (KIT / f"skills/{name}/SKILL.md").read_text()
            self.assertNotRegex(text, r"(?m)^```!|(^|\s)!`", f"{name}: relies on command injection")
            self.assertIn("run this exact command with the Bash tool", text, name)
            self.assertIn("actual output and exit status", text, name)
            cmd = re.search(r"(?s)```bash\n(.*?)\n```", text).group(1)
            allowed = re.search(r"allowed-tools: Bash\((.*) \*\)", text).group(1)
            self.assertTrue(cmd.startswith(allowed + " "), f"{name}: the command is not covered by allowed-tools")
        setup_text = (KIT / "skills/setup/SKILL.md").read_text()
        self.assertIn('--session "${CLAUDE_SESSION_ID}"', setup_text)


class PluginOptInTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"app.py": "x = 1\n", "tests/test_app.py": "def test_a():\n    assert True\n"})
        (self.root / "app.py").write_text("x = 2\n")       # a code change with red tests

    def test_hooks_do_nothing_in_a_repo_that_did_not_opt_in(self):
        hooks = plugin_hooks()
        r = run_hook(hooks["SessionStart"], self.root)
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = run_hook(hooks["Stop"], self.root)
        self.assertEqual((r.returncode, r.stdout), (0, ""), "the plugin gate acted in a repo that never opted in")
        self.assertFalse((self.root / ".prove-it").exists(), "the plugin wrote state into a repo that never opted in")

    def test_a_kept_config_alone_does_not_opt_in(self):
        cfg = self.root / ".claude/prove-it/config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{"test_cmd": "false"}')               # e.g. left behind by an uninstall
        assert_silent(self, run_hook(plugin_hooks()["Stop"], self.root))

    def test_setup_opts_in_and_the_stop_hook_blocks_on_red(self):
        r = setup(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        hooks = plugin_hooks()
        (self.root / "app.py").write_text("x = 3\n")       # the agent changes code after setup
        out = json.loads(run_hook(hooks["Stop"], self.root).stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("tests/test_app.py::test_a", out["reason"])

    def test_the_setup_turn_itself_is_not_blocked_by_already_red_tests(self):
        self.assertEqual(setup(self.root, env={"PROVE_IT_TEST_CMD": RED}).returncode, 0)   # same command as the hook
        r = run_hook(plugin_hooks()["Stop"], self.root, session="the-session-that-ran-setup")
        assert_silent(self, r, "nothing changed since setup, yet the gate ran the (already red) tests")
        (self.root / "app.py").write_text("x = 4\n")
        out = json.loads(run_hook(plugin_hooks()["Stop"], self.root, session="the-session-that-ran-setup").stdout)
        self.assertEqual(out["decision"], "block")

    def test_the_same_repo_reached_by_a_symlinked_path_keeps_its_baseline(self):
        link = Path(tempfile.mkdtemp()) / "via-link"
        link.symlink_to(self.root)                          # e.g. /tmp vs /private/tmp on macOS
        self.assertEqual(setup(self.root, env={"PROVE_IT_TEST_CMD": RED}).returncode, 0)
        r = run_hook(plugin_hooks()["Stop"], link, session="other-path")
        assert_silent(self, r, "a different spelling of the repo path changed the fingerprint")

    def test_setup_again_in_a_session_that_kept_an_older_baseline_does_not_block_its_own_turn(self):
        self.assertEqual(setup(self.root, env={"PROVE_IT_TEST_CMD": RED}).returncode, 0)
        hooks = plugin_hooks()
        assert_silent(self, run_hook(hooks["SessionStart"], self.root, session="S"))   # baseline A
        self.assertEqual(teardown(self.root).returncode, 0)
        (self.root / "app.py").write_text("x = 5\n")       # state B, tests already red
        r = setup(self.root, "--session", "S", env={"PROVE_IT_TEST_CMD": RED})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        assert_silent(self, run_hook(hooks["Stop"], self.root, session="S"),
                      "setup in the same session blocked its own turn on tests that were red before setup")
        (self.root / "app.py").write_text("x = 6\n")
        self.assertEqual(json.loads(run_hook(hooks["Stop"], self.root, session="S").stdout)["decision"], "block")

    def test_setup_again_gives_the_session_a_fresh_repair_budget(self):
        hooks, red = plugin_hooks(), {"PROVE_IT_TEST_CMD": RED}
        self.assertEqual(setup(self.root, env=red).returncode, 0)
        assert_silent(self, run_hook(hooks["SessionStart"], self.root, session="S"))
        for n in (1, 2):                                 # both repair attempts used up
            (self.root / "app.py").write_text(f"x = 1{n}\n")
            self.assertIn(f"fix cycle {n} of 2", json.loads(run_hook(hooks["Stop"], self.root, session="S").stdout)["reason"])
        self.assertEqual(teardown(self.root).returncode, 0)
        (self.root / "app.py").write_text("x = 20\n")
        self.assertEqual(setup(self.root, "--session", "S", env=red).returncode, 0)
        (self.root / "app.py").write_text("x = 21\n")
        out = json.loads(run_hook(hooks["Stop"], self.root, session="S").stdout)
        self.assertEqual(out.get("decision"), "block", f"the first red change after setup was not blocked: {out}")
        self.assertIn("fix cycle 1 of 2", out["reason"])

    def test_an_active_session_survives_many_newer_sessions(self):
        hooks = plugin_hooks()
        self.assertEqual(setup(self.root, env={"PROVE_IT_TEST_CMD": RED}).returncode, 0)
        assert_silent(self, run_hook(hooks["SessionStart"], self.root, session="S"))
        for batch in range(2):                             # e.g. many short `claude -p` runs while S stays in use
            for n in range(49):
                run([PY, str(TOOLS / "pytest_gate.py"), "--baseline"], cwd=self.root,
                    stdin=json.dumps({"session_id": f"other-{batch}-{n}"}), env={"CLAUDE_PROJECT_DIR": str(self.root)})
            assert_silent(self, run_hook(hooks["Stop"], self.root, session="S"),
                          "S lost its baseline to newer sessions and was blocked on tests it never touched")

    def test_check_from_a_subfolder_uses_the_repo_config(self):
        self.assertEqual(setup(self.root).returncode, 0)
        (self.root / ".claude/prove-it/config.json").write_text(json.dumps({"test_cmd": RED, "timeout": 30}))
        (self.root / "pkg").mkdir()
        r = run([PY, str(TOOLS / "pytest_gate.py"), "--check"], cwd=self.root / "pkg")    # no CLAUDE_PROJECT_DIR
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("tests/test_app.py::test_a", r.stdout)
        self.assertFalse((self.root / "pkg/.prove-it").exists())

    def test_plugin_hooks_stay_quiet_where_the_project_install_runs_the_gate(self):
        r = run(["bash", INSTALL, str(self.root)])
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_hook(plugin_hooks()["Stop"], self.root)
        assert_silent(self, r, "the gate would run twice: once from the project hook, once from the plugin")


class PluginSetupTests(unittest.TestCase):
    def setUp(self):
        self.root = git_repo({"CLAUDE.md": "# Mine\n", "app.py": "x = 1\n"})

    def test_setup_writes_config_rules_exclude_and_no_hooks_or_gate_copy(self):
        r = setup(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.root / ".claude/prove-it/config.json").is_file())
        self.assertIn("prove-it:begin", (self.root / "CLAUDE.md").read_text())
        self.assertIn("prove-it:begin", (self.root / "AGENTS.md").read_text())
        self.assertIn(".prove-it/", (self.root / ".git/info/exclude").read_text())
        self.assertFalse((self.root / ".claude/prove-it/pytest_gate.py").exists())
        self.assertFalse((self.root / ".claude/settings.json").exists(), "the plugin provides the hooks itself")
        manifest = json.loads((self.root / ".claude/prove-it/install-manifest.json").read_text())
        self.assertEqual((manifest["edition"], manifest["status"]), ("plugin", "complete"))

    def test_teardown_restores_the_repo_and_turns_the_hooks_off(self):
        setup(self.root)
        r = teardown(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Mine\n")
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".claude/prove-it/install-manifest.json").exists())
        (self.root / "app.py").write_text("x = 2\n")
        assert_silent(self, run_hook(plugin_hooks()["Stop"], self.root), "the gate acted after teardown")

    def test_setup_and_install_sh_refuse_each_other(self):
        self.assertEqual(setup(self.root).returncode, 0)
        r = run(["bash", INSTALL, str(self.root)])
        self.assertNotEqual(r.returncode, 0)
        teardown(self.root)
        self.assertEqual(run(["bash", INSTALL, str(self.root)]).returncode, 0)
        r = setup(self.root)
        self.assertNotEqual(r.returncode, 0)

    def test_setup_follows_the_clean_state_rules(self):
        (self.root / "CLAUDE.md").write_text("# Mine\n<!-- prove-it:begin (broken\nKEEP\n")
        r = setup(self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Mine\n<!-- prove-it:begin (broken\nKEEP\n")
        self.assertFalse((self.root / ".claude/prove-it/config.json").exists())

    def test_setup_refuses_leftover_project_hooks_in_settings(self):
        self.assertEqual(run(["bash", INSTALL, str(self.root)]).returncode, 0)
        for rel in (".claude/prove-it/pytest_gate.py", ".claude/prove-it/install-manifest.json", "AGENTS.md"):
            (self.root / rel).unlink()                     # half-removed by hand: only the settings hooks are left
        (self.root / "CLAUDE.md").write_text("# Mine\n")
        r = setup(self.root)
        self.assertNotEqual(r.returncode, 0, "setup ignored project hooks that would run a missing gate")
        self.assertIn("settings.json", r.stderr)
        self.assertFalse((self.root / ".claude/prove-it/install-manifest.json").exists())
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Mine\n")

    def test_setup_leaves_an_unparseable_settings_file_alone(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.json").write_text("{ // mine\n}\n")
        r = setup(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.root / ".claude/settings.json").read_text(), "{ // mine\n}\n")

    def test_setup_always_reports_the_test_command_even_with_a_kept_config(self):
        r = setup(self.root)
        self.assertIn("Test command: python3 -m pytest -q", r.stdout)
        self.assertEqual(teardown(self.root).returncode, 0)
        (self.root / ".claude/prove-it/config.json").write_text('{"test_cmd": "tox -e py -q", "timeout": 300}\n')
        r = setup(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Test command: tox -e py -q", r.stdout, "re-setup kept config.json but did not name its command")

    def test_teardown_ignores_an_unparseable_settings_file(self):
        self.assertEqual(setup(self.root).returncode, 0)
        (self.root / ".claude/settings.json").write_text("{ // mine\n}\n")
        r = teardown(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.root / ".claude/prove-it/install-manifest.json").exists())
        self.assertEqual((self.root / ".claude/settings.json").read_text(), "{ // mine\n}\n")

    def test_failed_setup_rolls_back(self):
        r = setup(self.root, env={"PROVE_IT_INSTALL_FAULT_AFTER": "3"})
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Mine\n")
        self.assertFalse((self.root / ".claude/prove-it/install-manifest.json").exists())
        self.assertEqual(setup(self.root).returncode, 0)

    def test_setup_outside_the_git_top_level_is_refused(self):
        sub = self.root / "pkg"
        sub.mkdir()
        self.assertNotEqual(setup(sub).returncode, 0)
        nogit = Path(tempfile.mkdtemp())
        self.assertNotEqual(setup(nogit).returncode, 0)


class ReadmeTests(unittest.TestCase):
    def test_readme_starts_with_the_plugin_install(self):
        text = (KIT / "README.md").read_text()
        top = text[: text.index("## ", text.index("/prove-it-mini:setup"))] if "/prove-it-mini:setup" in text else ""
        for cmd in ("/plugin marketplace add OlegTestov/prove-it-mini", "/plugin install prove-it-mini@prove-it-mini",
                    "/prove-it-mini:setup"):
            self.assertIn(cmd, top)
        self.assertRegex(text, r"(?s)TDD Guard.*Probity")


if __name__ == "__main__":
    unittest.main()
