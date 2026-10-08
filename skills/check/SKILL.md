---
name: check
description: Run this repository's tests once through the Prove-It gate and show the result (green, red, or a configuration error).
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/tools/pytest_gate.py" *)
---

First, run this exact command with the Bash tool, once:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/pytest_gate.py" --check
```

Then summarise from its actual output and exit status only, in at most three lines. If the command did not run, say so and report nothing else. Do not change code or tests in this turn.
- Exit status 0: green.
- Exit status 1: red. Name the failing tests from the output.
- Exit status 2: `ERROR`. Quote it; usually `test_cmd` in `.claude/prove-it/config.json` needs fixing.
