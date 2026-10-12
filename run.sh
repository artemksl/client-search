#!/usr/bin/env bash
set -euo pipefail
CLIENT_SEARCH_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CLIENT_SEARCH_ROOT"
if [[ ! -x .venv/bin/python ]]; then
  echo "Run ./setup.sh first." >&2
  exit 1
fi
export PYTHONPATH="$CLIENT_SEARCH_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec .venv/bin/python -u -m client_search.app "$@"
