#!/usr/bin/env bash
# Prove-It Mini installer: a pytest gate for Claude Code (and rules for Codex). Safe to run again.
# Every check runs before anything is written; files that exist before they are replaced are copied
# to a new .prove-it/backup/<time>-XXXX/ folder first.
#
#   ./install.sh [TARGET_REPO] [--dry-run] [--no-codex] [--uninstall]
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="."
ACTION="install"
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --uninstall) ACTION="uninstall" ;;
    --dry-run|--no-codex) ARGS+=("$arg") ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) TARGET="$arg" ;;
  esac
done

command -v python3 >/dev/null || { echo "python3 (3.9+) is required" >&2; exit 2; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "python3 3.9+ is required, found $(python3 --version)" >&2; exit 2; }

if [ "$ACTION" = "uninstall" ]; then
  DRY=()
  for arg in "$@"; do if [ "$arg" = "--dry-run" ]; then DRY=("--dry-run"); fi; done
  exec python3 "$KIT_DIR/tools/install_helpers.py" uninstall --kit "$KIT_DIR" --target "$TARGET" ${DRY[@]+"${DRY[@]}"}
fi

python3 "$KIT_DIR/tools/install_helpers.py" install --edition mini --kit "$KIT_DIR" --target "$TARGET" \
  ${ARGS[@]+"${ARGS[@]}"}

cat <<'NEXT'

Installed. Next:
  1. Check the test command in .claude/prove-it/config.json, then run:
       python3 .claude/prove-it/pytest_gate.py --check
  2. Restart Claude Code in this folder. From now on, when it tries to finish after changing code, pytest runs (at most 2 repair attempts).
  3. Test runs are logged (best-effort) in .prove-it/gate-log.md.
NEXT
