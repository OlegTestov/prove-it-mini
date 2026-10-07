<!-- prove-it:begin (added by Prove-It Mini; ./install.sh --uninstall removes this block) -->
## Done means tests pass

- Before you say "done", "fixed" or "works": the test suite must pass. A Stop hook (`.claude/prove-it/pytest_gate.py`) runs the tests when you try to finish after changing code, and sends you back if they fail.
- When the gate sends you back: fix the **code**. Do not edit, skip, delete or loosen tests to get green. If a test is wrong, stop and say so.
- Claims need evidence the user can re-run: a command and its output, a test name, a file:line. "The code looks right" is not evidence.
- End every task with a short **Not verified** list: what you did not check and why.
<!-- prove-it:end -->
