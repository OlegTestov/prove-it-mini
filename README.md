# Prove-It Mini

A pytest Stop hook for Claude Code: two repair attempts, then an explicit unverified result.

Runs your tests after code changes, remembers verified states, and flags changed tests.
- When the agent tries to finish after changing code, Mini runs your test command.
- Red tests block the stop and hand the failure back to the agent, at most 2 times.
- If the tests are still red after that, the agent may stop, and you see "NOT verified" instead of a quiet "done".

No signup. No reviewer model. MIT.

![Recorded run: blocked on a red test, repaired, green](example/demo.gif)

## Install (Claude Code plugin)

In Claude Code, inside your Python repo:

```
/plugin marketplace add OlegTestov/prove-it-mini
/plugin install prove-it-mini@prove-it-mini
/prove-it-mini:setup
```

Run `/prove-it-mini:setup` once in each repo you want gated: the plugin does nothing in repos that didn't run it. If the command doesn't show up right after the install, restart Claude Code. Requirements: macOS or Linux (Windows via WSL), git, Python 3.9+, pytest in your project.

**Limits:** passing tests don't prove the code is correct. Test edits are flagged, but Mini can't prevent them.

Useful on your repo? Star it, or report a failure case.

## Before / after

From the [recorded example](example/EXAMPLE.md) (Claude Haiku 4.5, one new requirement only in the tests; the prompt told the agent not to look at the tests):

```
without:  agent> Done. I've added the HALF discount code to shop/pricing.py.    # 1 of 3 new tests fails
with:     agent> Done. I've added the HALF discount code to shop/pricing.py.
          gate>  you cannot finish yet. Tests fail (exit 1). ... test_codes_are_case_insensitive
          agent> (reads the failure, makes codes case-insensitive, runs the tests)
          gate>  tests green (1.5s): the agent may finish
log:      BLOCKED cycle 1/2 -> PASS. Cost of the catch: one fix cycle, 13 seconds.
```

## Similar tools

Facts from their READMEs as of 07.10.2026; "—" means the README doesn't cover it.

| | Prove-It Mini | [Nonna](https://github.com/kapadias/nonna) | [TDD Guard](https://github.com/nizos/tdd-guard) | [Probity](https://github.com/nizos/probity) |
|---|---|---|---|---|
| When it checks | when the agent tries to finish after changing code | same, plus git commit/push and file access | when the agent changes code | before every file write and shell command |
| What it checks | your test command passes | your test command passes; branch and secret guards | TDD: a failing test first | your rules (TDD, patterns, custom) |
| After a red run | blocks up to 2 times, then "NOT verified" | blocks once, then tells the agent to say it is not done | — | — |
| Changed test files | named in every result | rules forbid weakening; "no gate checks yet" | — | — |
| Model calls | none | none | yes | none for pattern rules; AI rules optional |
| Agents | Claude Code (Codex: AGENTS.md only) | Claude Code; Codex and Copilot plugins (not yet run end to end); rules for others | Claude Code | Claude Code, Codex, Copilot CLI |

Nonna does more (git hooks, secret and branch guards, many agents). Mini does one thing: the pytest check at the end of a turn, with bounded retries and named test edits. TDD Guard and Probity enforce a way of working on each action; TDD Guard's README recommends Probity for new projects.

**Codex:** Mini currently integrates with Codex through AGENTS.md instructions. It does not install Codex's native Stop hook. The `AGENTS.md` block tells Codex to run `pytest_gate.py --check` and paste the output before reporting done; Mini can't enforce that.

<details>
<summary>What plugin setup does</summary>

`/prove-it-mini:setup` runs the same installer code as `install.sh`, with the same clean-state rules and the same all-or-nothing transaction (see below). It writes `.claude/prove-it/config.json`, the rules block in `CLAUDE.md` and `AGENTS.md`, the manifest (marked as a plugin setup), and the `.prove-it/` line in git's `info/exclude`. It doesn't copy the gate and doesn't touch `.claude/settings.json`: the plugin brings its own hooks.

- The plugin's hooks act only in a repo whose manifest says the plugin setup completed. Everywhere else they exit at once without writing anything. A `config.json` left behind by an uninstall doesn't turn them on.
- The session that runs setup isn't blocked by tests that were already red: setup records the state at that moment, also for a session that ran teardown earlier. The gate runs the tests only after the code changes.
- `/prove-it-mini:check` runs your tests now. `/prove-it-mini:teardown` removes what setup added, like `install.sh --uninstall`.
- Use either the plugin or `install.sh` in a repo, not both: each refuses while the other is installed. Setup also refuses while `.claude/settings.json` still holds Prove-It hook entries from an earlier `install.sh`.

</details>

## Install without the plugin (install.sh)

```sh
git clone https://github.com/OlegTestov/prove-it-mini && cd prove-it-mini
./install.sh /path/to/your/python/repo            # the repo's top-level folder; --dry-run to preview, --uninstall to remove
cd /path/to/your/python/repo
python3 .claude/prove-it/pytest_gate.py --check   # always runs your tests: exit 0 green, 1 red, 2 config error
```

Then restart Claude Code in the repo.

<details>
<summary>What the installer does, and what it refuses</summary>


It installs only into a clean state, at the top level of a git repository (subfolders and folders outside git are refused), as one transaction. It never overwrites a file you own, so nothing needs a backup. It adds:

| Path | What |
|---|---|
| `.claude/prove-it/pytest_gate.py` | the gate (new file) |
| `.claude/prove-it/config.json` | created only if missing: test command (`.venv/bin/python -m pytest -q` if `.venv` exists, else `python3 -m pytest -q`), `timeout` 300 |
| `.claude/settings.json` | two hook entries appended (`Stop`, `SessionStart`); everything else in the file is kept |
| `CLAUDE.md`, `AGENTS.md` | one rules block appended between `<!-- prove-it:begin ... -->` and `<!-- prove-it:end -->` lines |
| `.claude/prove-it/install-manifest.json` | the gate file's path and SHA-256, which rules files got a block, and whether `settings.json` was created by the installer |
| git's `info/exclude` | the line `.prove-it/` (the right file in linked worktrees too). Afterwards git is asked whether `.prove-it/` is really ignored; if not, everything is rolled back |

It checks everything and computes every final file before the first write, and **changes nothing** if any of these is true:
- the target is a subfolder of a git repository, not its top level;
- the gate file, the manifest, the Prove-It hooks or a rules block is already there: reinstall or upgrade with `--uninstall`, then install;
- a managed path, a folder above it, or a `.prove-it/` state file is a symlink;
- `settings.json` is not valid JSON or its `hooks` section doesn't have the expected lists and objects. Mini doesn't judge your other hooks; it keeps them unchanged;
- a rules file has an incomplete or stray `prove-it` marker;
- a file under `.prove-it/` is tracked by git;
- another install or uninstall holds the lock `.prove-it/install.lock`. If none is running, delete the lock.

Every write goes to a fresh temporary file in the same folder and then replaces the target in one step. If any write fails, or git still doesn't ignore `.prove-it/` afterwards, every file is put back to its original bytes. A refused install doesn't even leave an empty `.prove-it/` behind.

The manifest is written first, marked "pending", and marked "complete" at the end. If the installer is killed midway, a new install says "a previous install was interrupted; run ./install.sh --uninstall first" (delete a leftover `.prove-it/install.lock` if no install is running).

`--uninstall` needs the manifest (pending or complete) and refuses without it. It then:
- keeps an edited gate file (and reports it); removes it only if it's the kit's file; a file the interrupted install never wrote is simply skipped;
- removes the exact hook entries;
- removes the managed rules block only from the files listed in the manifest, **including your edits inside that block** (text outside the block is kept);
- deletes the manifest, and `settings.json` too if the installer created it and it's empty again.

It never copies files back and only touches the paths above. Your `config.json` and the `.prove-it/` log stay.

</details>

## How it decides

- At session start it records the SHA-256 of every relevant file's working-tree bytes, the test command, and the content of every test file.
- When the agent stops and no relevant file changed, no tests run. That covers chat-only sessions and docs-only changes, committed or not. Changing code does count, whether committed, staged or not, and even for files marked `assume-unchanged`. If no baseline was recorded (e.g. the hook was installed mid-session), the tests run.
- After a change it runs the test command. Green → finish. Red → back to the agent with the failure summary.
- If test files changed or were deleted since the session started (committed or not), they are named on every outcome: in the block message (with a reminder not to weaken them), and in the warning shown on a pass, a give-up or a no-tests run.
- After the test run the gate takes the fingerprint again. If relevant files changed during the run, it reruns once; if they change again, it reports "NOT verified: files changed during the test run". The whole gate has one 570-second budget, including that rerun; if too little time is left, it reports "NOT verified: no time for a rerun".
- A pass counts as green only if no newer run of the same state failed in the meantime; otherwise it reports "NOT verified". A failing run marks the state it started from as red, and also the state it left behind if it changed files.
- Every relevant path is resolved first, including symlinked files and symlinked parent folders, and checked by the content it really points to. Anything that resolves outside the repository, or can't be read, can't be vouched for: the tests always run and the result is never cached.
- Once a state is green, it's remembered for that test command, so tests don't rerun on every message. A later red run of the same state, including a manual `--check`, cancels that.
- `--check` always runs the tests, even when the hook is switched off with `PROVE_IT_GATE_DISABLE=1`, and reports test-file changes against the most recently recorded session baseline.
- A stop that reuses an earlier green result still names changed test files.
- The tests run in their own process group. On a timeout, or when the hook itself is stopped, that process group is killed. Processes that detach into a new session aren't covered.
- The gate aims to finish within the hook's 600-second limit: the test timeout is 1–570 seconds, the whole gate (including a rerun) shares one 570-second budget, and no test run is started once that budget is used up ("NOT verified: no time to run the tests"). A broken config never looks green: `--check` exits 2, and the hook says "NOT verified". An empty `test_cmd`, or an invalid timeout, is a config error.
- Exit code 5 counts as "no tests collected" (a warning, not a block) only for a direct pytest call: `pytest ...`, `python -m pytest ...` or `<path>/python -m pytest ...`, with no `&&`, `;`, `|` or other shell operators. Anything else that exits 5 is a failure.
- The fix-cycle limit is fixed at 2.
- The gate never follows a symlink in `.prove-it/`: if its log or state file is a symlink, it refuses and reports "NOT verified".

Relevant files are matched by `watch` globs. The default is Python, config and data files (`*.py`, `*.toml`, `*.cfg`, `*.ini`, `*.json`, `*.yaml`, `*.yml`, `requirements*.txt`, ...), not Markdown.

Log: test runs are logged best-effort in `.prove-it/gate-log.md` (readable) and `.prove-it/gate.jsonl` (one JSON line each), with the fingerprint the run started from, exit code and duration. That includes runs whose result was discarded because files changed, runs whose post-run check failed, and runs stopped by an interruption ("interrupted"). A run killed by the hook's own timeout, or one that ends while the gate's state files can't be written, may have no record. A stop that needs no test run is not logged. The gate opens `.prove-it/` once without following symlinks and does all its file work through that handle. A FIFO or other special file there is rejected without blocking.

Settings: edit `.claude/prove-it/config.json` (`test_cmd`, `timeout`, `watch`), or use the env vars `PROVE_IT_TEST_CMD`, `PROVE_IT_TIMEOUT`, `PROVE_IT_WATCH`. `PROVE_IT_GATE_DISABLE=1` switches the hook off.

**Limits:**
- Passing tests don't prove the code is correct: Mini checks only what your tests check.
- The gate makes test edits visible but can't stop a determined agent from weakening a test.
- Threat model: Mini does not defend against another local process that concurrently swaps folders for symlinks or plants special files in the repository while the installer or the gate is running.
- Native Windows is not tested.

## Tests

`bash run_tests.sh` (or `pytest` at the kit root) runs the kit's own tests (no network, no API calls).

## Prove-It Pro

Pro is planned and not on sale: a verification report and mutation checks for changed Python code. To hear when it ships: Watch → Custom → Releases on this repo.

License: MIT (see `LICENSE`).
