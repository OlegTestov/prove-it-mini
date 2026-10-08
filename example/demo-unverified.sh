#!/usr/bin/env bash
# Second demo GIF: the gate's explicit "NOT verified" after 2 fix cycles. A real run, recorded with asciinema and agg.
# Setup: the shop from EXAMPLE.md plus tests/test_payments.py, an integration test that charges a HALF order in a
# payments sandbox. It reads PAYMENTS_SANDBOX_URL, which only CI has, so it fails on this machine whatever the agent
# does in shop/pricing.py. The agent may edit only shop/pricing.py; the gate sends it back twice, then lets it stop
# with "NOT verified". The gate's message reaches the user as a system message, so the run uses stream-json and jq.
# Needs: asciinema 3, agg, claude (logged in), git, python3, jq. Uses the plugin from this checkout (--plugin-dir).
# Run from the repository root:
#   bash example/demo-unverified.sh prep
#   asciinema rec --headless --window-size 100x26 --overwrite -c "bash example/demo-unverified.sh show" /tmp/prove-it-unverified.cast
#   agg --speed 1.15 --idle-time-limit 1.5 --last-frame-duration 6 /tmp/prove-it-unverified.cast example/demo-unverified.gif
# The idle time limit shortens the wait for the agent; everything shown is the real output.
# claude runs without the recorder's user settings (--setting-sources project,local), as on a fresh machine.
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO=/tmp/prove-it-unverified

run() {   # print the command as if typed, then run it for real
  local delay="${2:-0.03}"
  printf '\033[1;32m$\033[0m '
  for ((i = 0; i < ${#1}; i++)); do printf '%s' "${1:i:1}"; sleep "$delay"; done
  sleep 0.4; printf '\n'
  eval "$1" || true   # red test runs are part of the story
  sleep 1.5
}

case "${1:-}" in
  prep)
    rm -rf "$DEMO" && cp -R "$KIT/example/shop" "$DEMO" && cd "$DEMO"
    cat > tests/test_payments.py <<'PY'
import os
import urllib.request

from shop.pricing import order_total


def test_half_order_is_charged_in_payments_sandbox():
    url = os.environ["PAYMENTS_SANDBOX_URL"]  # set in CI; not on dev machines
    body = f'{{"amount": {order_total([(150.0, 1)], "HALF")}}}'.encode()
    with urllib.request.urlopen(url + "/charge", data=body, timeout=5) as resp:
        assert resp.status == 200
PY
    git init -q && git add -A && git commit -qm base
    python3 -m venv .venv && .venv/bin/pip install -q pytest
    claude --setting-sources project,local -p "/prove-it-mini:setup" --plugin-dir "$KIT" --model haiku < /dev/null > /dev/null
    test -f .claude/prove-it/config.json   # setup prints the absolute path, so it runs before the recording
    ;;
  show)
    claude() { command claude --setting-sources project,local "$@"; }   # leave out the recorder's own ~/.claude settings, hooks and CLAUDE.md
    cd "$DEMO" && export KIT && clear
    run '# Prove-It Mini is set up. tests/test_payments.py needs PAYMENTS_SANDBOX_URL (set only in CI)'
    run '.venv/bin/python -m pytest -q | tail -1'
    run 'claude -p "In shop/pricing.py add discount code HALF: 50% off for orders whose subtotal is over 100. Edit only shop/pricing.py." --plugin-dir "$KIT" --model haiku --permission-mode acceptEdits --allowedTools "Bash(.venv/bin/python -m pytest:*)" --output-format stream-json --verbose | jq -r '"'"'select(.subtype == "informational").content'"'"'' 0.005
    run 'cat .prove-it/gate-log.md'
    sleep 2
    ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
