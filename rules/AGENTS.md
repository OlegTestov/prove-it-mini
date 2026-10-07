<!-- prove-it:begin (added by Prove-It Mini; ./install.sh --uninstall removes this block) -->
## Done means tests pass (Codex)

- Before you report a task as done, run the test command from `.claude/prove-it/config.json` (default `python3 -m pytest -q`) and include the last lines of its output in your answer.
- Or run `python3 .claude/prove-it/pytest_gate.py --check`: exit 0 means green; exit 1 (tests red) or 2 (gate config error) means you are not done.
- Red tests: fix the code, not the tests. Do not skip, delete or loosen tests. If a test is wrong, say so.
- End with a short **Not verified** list: what you did not check and why.
<!-- prove-it:end -->
