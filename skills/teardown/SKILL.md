---
name: teardown
description: Turn off the Prove-It pytest gate in this repository. Removes the rules blocks it added and its manifest; keeps .claude/prove-it/config.json and the .prove-it/ log.
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/tools/install_helpers.py" *)
---

First, run this exact command with the Bash tool, once:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/install_helpers.py" teardown --kit "${CLAUDE_PLUGIN_ROOT}" --target "${CLAUDE_PROJECT_DIR}"
```

Then report from its actual output and exit status only, in at most three short lines. If the command did not run, say so and report nothing else. Do not edit any file yourself.
- Exit status 0: the gate is off in this repository (the plugin stays installed and does nothing here); `config.json` and the `.prove-it/` log were kept.
- Exit status not 0 (`REFUSED`): quote the reason.
