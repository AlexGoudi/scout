#!/usr/bin/env bash
# Shared by demo-setup.sh and demo.sh: pick .venv python and install requirements.txt once.
set -euo pipefail
_DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${PYTHON:-}" ]]; then
    if [[ ! -x "$_DEMO_ROOT/.venv/bin/python3" ]]; then
        python3 -m venv "$_DEMO_ROOT/.venv"
    fi
    PYTHON="$_DEMO_ROOT/.venv/bin/python3"
fi

if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
    echo "Installing runtime deps from requirements.txt (PyYAML, sklearn, …)" >&2
    "$PYTHON" -m pip install -q -U pip
    "$PYTHON" -m pip install -q -r "$_DEMO_ROOT/requirements.txt"
fi

export PYTHON

# GITHUB_TOKEN for ml watch and GitHub metadata; .env is gitignored and never echoed.
if [[ -f "$_DEMO_ROOT/.env" && -z "${GITHUB_TOKEN:-}${GH_TOKEN:-}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$_DEMO_ROOT/.env"
    set +a
fi

# Where mine-*, backtest and the online ledger keep their data (run_scout.py --cache-dir default).
export SCOUT_EVAL_CACHE="${SCOUT_EVAL_CACHE:-${SCOUT_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/sonic-scout/repos}/eval}"

# ./demo.sh review provider for the sonic-buildimage fixture PRs: ollama asks a live model
# (start ./demo-serve.sh first; with no server the review runs degraded, deterministic findings
# only), replay serves the fixtures' recorded answers offline, none skips the agent stage.
export SCOUT_DEMO_PROVIDER="${SCOUT_DEMO_PROVIDER:-ollama}"
# The live sonic-mgmt review has no recorded answers to replay: none or ollama.
export SCOUT_DEMO_MGMT_PROVIDER="${SCOUT_DEMO_MGMT_PROVIDER:-none}"
# When a provider is ollama: a tag from `ollama list`.
export SCOUT_OLLAMA_MODEL="${SCOUT_OLLAMA_MODEL:-qwen2.5-coder:7b}"
export SCOUT_OLLAMA_URL="${SCOUT_OLLAMA_URL:-http://127.0.0.1:11435}"
