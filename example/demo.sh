#!/usr/bin/env bash
# Demo GIF for the README: the example from EXAMPLE.md, a real run recorded with asciinema and agg.
# Needs: asciinema 3, agg, claude (logged in), git, python3. Uses the plugin from this checkout (--plugin-dir).
# Run from the repository root:
#   bash example/demo.sh prep
#   asciinema rec --headless --window-size 96x24 --overwrite -c "bash example/demo.sh show" /tmp/prove-it-demo.cast
#   agg --speed 1.15 --idle-time-limit 1.5 --last-frame-duration 5 /tmp/prove-it-demo.cast example/demo.gif
# The idle time limit shortens the wait for the agent; everything shown is the real output.
# claude runs without the recorder's user settings (--setting-sources project,local), as on a fresh machine.
# The model may read the tests and pass the first time; then the log shows a single PASS. Re-record if so.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO=/tmp/prove-it-demo

run() {   # print the command as if typed, then run it for real
  local delay="${2:-0.03}"
  printf '\033[1;32m$\033[0m '
  for ((i = 0; i < ${#1}; i++)); do printf '%s' "${1:i:1}"; sleep "$delay"; done
  sleep 0.4; printf '\n'
  eval "$1" || true   # a red test run is part of the story
  sleep 1.5
}

case "${1:-}" in
  prep)
    rm -rf "$DEMO" && cp -R "$KIT/example/shop" "$DEMO" && cd "$DEMO"
    git init -q && git add -A && git commit -qm base
    python3 -m venv .venv && .venv/bin/pip install -q pytest
    claude --setting-sources project,local -p "/prove-it-mini:setup" --plugin-dir "$KIT" --model haiku > /dev/null
    test -f .claude/prove-it/config.json   # setup prints the absolute path, so it runs before the recording
    ;;
  show)
    claude() { command claude --setting-sources project,local "$@"; }   # leave out the recorder's own ~/.claude settings, hooks and CLAUDE.md
    cd "$DEMO" && export KIT && clear
    run '# Prove-It Mini is set up (/prove-it-mini:setup). 3 new tests for discount code HALF:'
    run '.venv/bin/python -m pytest -q | tail -1'
    run 'claude -p "In shop/pricing.py add discount code HALF: 50% off for orders whose subtotal is over 100. Do not open or run anything in tests/; edit only shop/pricing.py and finish quickly. Answer in one line." --plugin-dir "$KIT" --model haiku --permission-mode acceptEdits --allowedTools "Bash(.venv/bin/python -m pytest:*)"' 0.005
    run 'cat .prove-it/gate-log.md'
    sleep 2
    run '.venv/bin/python -m pytest -q | tail -1'
    ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
