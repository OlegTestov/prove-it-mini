#!/usr/bin/env bash
# Run the kit's own test suite. No network, no model calls, no API keys; uses temp directories only.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/tests"
python3 -m unittest discover -s . -p 'test_*.py' "$@"
