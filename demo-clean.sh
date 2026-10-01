#!/usr/bin/env bash
# Delete Scout-generated artifacts so ./demo-setup.sh can run from a clean slate.
#
#   ./demo-clean.sh              repo outputs, corpus, models, caches under this tree
#   ./demo-clean.sh --calibration  also <eval cache>/calibration (Azure labels; older builds are gone from Azure)
#   ./demo-clean.sh --online       also <eval cache>/online (the ledger ml watch / walk-forward append to)
#   ./demo-clean.sh --backtest     also <eval cache>/backtest (pinned corpus and fixtures; recapture takes minutes)
#   ./demo-clean.sh --venv         also remove .venv (demo-setup will recreate it)
#   ./demo-clean.sh --all          all of the above + pytest/py caches
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
EVAL_CACHE="${SCOUT_EVAL_CACHE:-${SCOUT_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/sonic-scout/repos}/eval}"

CALIBRATION=0
ONLINE=0
BACKTEST=0
VENV=0
PYCACHE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --calibration) CALIBRATION=1; shift ;;
        --online) ONLINE=1; shift ;;
        --backtest) BACKTEST=1; shift ;;
        --venv) VENV=1; shift ;;
        --all) CALIBRATION=1; ONLINE=1; BACKTEST=1; VENV=1; PYCACHE=1; shift ;;
        -h|--help)
            sed -n '2,10p' "$0"
            exit 0
            ;;
        *) echo "demo-clean: unknown option: $1" >&2; exit 2 ;;
    esac
done

rm -rf "$ROOT/.scout-cache"

if [[ -d "$ROOT/corpus" ]]; then
    find "$ROOT/corpus" -mindepth 1 ! -name .gitkeep -exec rm -rf {} + 2>/dev/null || true
fi

if [[ -d "$ROOT/models" ]]; then
    rm -rf "$ROOT/models"/*
fi

rm -f "$ROOT"/scout-brief*.json "$ROOT"/scout-report*.json "$ROOT"/scout-run*.jsonl \
    "$ROOT"/scout-corpus*.jsonl "$ROOT"/scout-score-pr*.json \
    "$ROOT"/changeset.json "$ROOT"/sync.json

if [[ "$PYCACHE" -eq 1 ]]; then
    find "$ROOT" -type d -name __pycache__ -not -path '*/.venv/*' -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$ROOT/.pytest_cache" "$ROOT/scout"
fi

removed=()
[[ "$CALIBRATION" -eq 1 ]] && removed+=(calibration)
[[ "$ONLINE" -eq 1 ]] && removed+=(online)
[[ "$BACKTEST" -eq 1 ]] && removed+=(backtest)
for name in "${removed[@]}"; do
    rm -rf "${EVAL_CACHE:?}/$name"
done

if [[ "$VENV" -eq 1 ]]; then
    rm -rf "$ROOT/.venv"
fi

printf 'demo-clean: removed generated artifacts under %s\n' "$ROOT"
[[ ${#removed[@]} -gt 0 ]] && printf 'demo-clean: removed %s under %s\n' "${removed[*]}" "$EVAL_CACHE"
[[ "$VENV" -eq 1 ]] && printf 'demo-clean: removed .venv\n'
printf 'Next: ./demo-setup.sh [sonic-mgmt|sonic-buildimage]\n'
