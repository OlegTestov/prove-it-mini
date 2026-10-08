---
name: setup
description: Turn on the Prove-It pytest gate in this repository. Writes .claude/prove-it/config.json (test command), a short rules block in CLAUDE.md and AGENTS.md, and a git exclude line for the gate's log. Run once per repository.
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/tools/install_helpers.py" *)
---

First, run this exact command with the Bash tool, once:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/install_helpers.py" setup --kit "${CLAUDE_PLUGIN_ROOT}" --target "${CLAUDE_PROJECT_DIR}" --session "${CLAUDE_SESSION_ID}"
```

Then report from its actual output and exit status only, in at most four short lines. If the command did not run, say so and report nothing else. Do not edit any file yourself.
- Exit status not 0 (`REFUSED`): quote the reason. Nothing was changed. Tell them what the message says to do.
- Exit status 0: the gate is now on for this repository. Name the test command from the output, and say it can be changed in `.claude/prove-it/config.json`. Suggest `/prove-it-mini:check` to run the tests once now. From now on, when the agent tries to finish after changing code, the tests run and red tests send it back (at most 2 times).
