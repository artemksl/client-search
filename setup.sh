#!/usr/bin/env bash
set -euo pipefail
CLIENT_SEARCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CLIENT_SEARCH_ROOT"
CLIENT_SEARCH_PYTHON="${CLIENT_SEARCH_PYTHON:-python3}"
"$CLIENT_SEARCH_PYTHON" -c 'import sys; assert sys.version_info >= (3, 12), "Use Python 3.12 or later (CLIENT_SEARCH_PYTHON=python3.12 ./setup.sh)."'
"$CLIENT_SEARCH_PYTHON" -m venv .venv
.venv/bin/python -m pip install --find-links vendor -e '.[test]'
if [[ "${1:-}" == "--browser-tests" ]]; then
  .venv/bin/python -m playwright install chromium
fi
echo "Ready. Run ./run.sh, then open http://127.0.0.1:8766/visibility-testing#clientResearch"
