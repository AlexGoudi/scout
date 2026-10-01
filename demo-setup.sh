#!/usr/bin/env bash
# Prepare everything ./demo.sh shows: calibration labels, the phase-0 scorer, the commit dataset,
# every model, the online ledger and the D6 backtest. Safe to rerun: each step resumes from cache.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
# shellcheck source=demo-env.sh
source "$ROOT/demo-env.sh"

STEPS_ALL="fetch calibration score-pr dataset models online backtest"

usage() {
    cat <<EOF
usage: demo-setup.sh [sonic-mgmt|sonic-buildimage|all] [--offline] [--only STEPS] [--skip STEPS]

Steps, in order (comma-separated for --only / --skip):
  fetch        git fetch origin master in the clone
  calibration  mine-incidents, mine-git-dumps, mine-azure, mine-labels, check-fidelity
  score-pr     phase-0 per-job score of the clone's HEAD against origin/master
  dataset      commit dataset into corpus/<repo>-<sha>/
  models       train-risk (held-out and --final), train-risk reverted_within_90d (measurement
               only), ml similar --evaluate, train-pr-job (gated on beating the heuristic)
  online       ml walk-forward, ml watch (needs GITHUB_TOKEN), ml grade
  backtest     sonic-buildimage only: capture the seed corpus once, then replay D6 offline

  --offline    skip everything that needs the network: fetch, mine-azure, watch, backtest capture

Environment:
  SCOUT_REPO            target repo (default: sonic-mgmt)
  SCOUT_CLONE           checkout path (default: ~/data/git/\$REPO)
  SCOUT_AZURE_WORKERS   mine-azure concurrency (default: 8)
  SCOUT_WATCH_MAX_PRS   open PR heads ml watch scores per run (default: 20)
  GITHUB_TOKEN          read from .env when present; ml watch skips without it
  PYTHON                interpreter (default: .venv/bin/python3; creates .venv if missing)

Then run:  ./demo.sh [sonic-mgmt|sonic-buildimage|all]
EOF
}

REPO="${SCOUT_REPO:-sonic-mgmt}"
CLONE="${SCOUT_CLONE:-}"
WORKERS="${SCOUT_AZURE_WORKERS:-8}"
WATCH_MAX="${SCOUT_WATCH_MAX_PRS:-20}"
OFFLINE=0
ONLY=""
SKIP=""
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --offline) OFFLINE=1; PASSTHROUGH+=("$1"); shift ;;
        --only) ONLY="${2//,/ }"; PASSTHROUGH+=("$1" "$2"); shift 2 ;;
        --skip) SKIP="${2//,/ }"; PASSTHROUGH+=("$1" "$2"); shift 2 ;;
        sonic-mgmt|sonic-buildimage|all) REPO="$1"; shift ;;
        *)
            echo "demo-setup: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

for step in $ONLY $SKIP; do
    if [[ " $STEPS_ALL " != *" $step "* ]]; then
        echo "demo-setup: unknown step: $step (steps: $STEPS_ALL)" >&2
        exit 2
    fi
done

if [[ "$REPO" == all ]]; then
    SCOUT_CLONE="" "$0" sonic-buildimage "${PASSTHROUGH[@]}"
    SCOUT_CLONE="" "$0" sonic-mgmt "${PASSTHROUGH[@]}"
    exit 0
fi

[[ -z "$CLONE" ]] && CLONE="$HOME/data/git/$REPO"
if [[ ! -d "$CLONE/.git" ]]; then
    echo "demo-setup: clone not found at $CLONE" >&2
    echo "Clone upstream master there (git clone https://github.com/sonic-net/$REPO) or set SCOUT_CLONE." >&2
    exit 1
fi

wanted() {
    local step="$1"
    [[ -n "$ONLY" && " $ONLY " != *" $step "* ]] && return 1
    [[ " $SKIP " == *" $step "* ]] && return 1
    return 0
}

banner() {
    printf '\n======== %s (repo=%s) ========\n' "$1" "$REPO"
}

note() {
    printf '  -> %s\n' "$*"
}

scout() {
    "$PYTHON" run_scout.py --quiet --repo-root "$CLONE" --repo-type "$REPO" "$@"
}

# Run a step that may legitimately fail on thin data (no positives in a split, no token, ...)
# without aborting setup; the recap shows what exists.
soft() {
    local label="$1"
    shift
    if ! "$@"; then
        note "$label did not complete; continuing (see the messages above)"
        FAILED+=("$label")
    fi
}

FAILED=()
models_dir="models/${REPO}"
corpus_out="scout-corpus-${REPO}.jsonl"
score_out="scout-score-pr-${REPO}.json"
started=$SECONDS

if wanted fetch; then
    banner "fetch: git fetch origin master"
    if [[ "$OFFLINE" -eq 1 ]]; then
        note "skipped (--offline)"
    else
        git -C "$CLONE" fetch origin master
    fi
fi

snapshot="$(git -C "$CLONE" rev-parse origin/master)"
dataset="corpus/${REPO}-${snapshot:0:9}"
note "origin/master is ${snapshot:0:12}; dataset dir $dataset"

if wanted calibration; then
    banner "calibration 1/5: mine-incidents (revert history -> seed incidents)"
    scout mine-incidents --output "$corpus_out"
    banner "calibration 2/5: mine-git-dumps (numstat, revert rates, path priors)"
    scout mine-git-dumps --revision origin/master
    banner "calibration 3/5: mine-azure (PR builds and job timelines, up to 3 attempts per PR)"
    if [[ "$OFFLINE" -eq 1 ]]; then
        note "skipped (--offline); the join uses the cached timelines"
    else
        note "Azure keeps PR runs about 10 days: 404s are builds past retention"
        scout mine-azure --workers "$WORKERS"
    fi
    banner "calibration 4/5: mine-labels (join -> model-*, scoring-plan.json)"
    scout mine-labels
    banner "calibration 5/5: check-fidelity (adapter job groups vs Azure job keys)"
    scout check-fidelity
fi

if wanted score-pr; then
    banner "score-pr: phase-0 score of HEAD...origin/master in the clone"
    # score-pr JSON goes to stdout; do not pass --quiet or the redirect file is empty.
    "$PYTHON" run_scout.py --repo-root "$CLONE" --repo-type "$REPO" score-pr --base origin/master >"$score_out"
    note "wrote $score_out"
fi

if wanted dataset; then
    banner "dataset: commit records, SZZ labels, features.parquet"
    "$PYTHON" run_scout.py --quiet dataset build --repo-type "$REPO" --repo "$CLONE" --rev origin/master
fi

if wanted models; then
    banner "models 1/5: train-risk bug_introducing, held out (honest test metrics)"
    "$PYTHON" run_scout.py --quiet ml train-risk --dataset "$dataset" --out "$models_dir"
    banner "models 2/5: train-risk bug_introducing --final (refit on every split, for ml score)"
    "$PYTHON" run_scout.py --quiet ml train-risk --dataset "$dataset" --out "$models_dir" --final
    banner "models 3/5: train-risk reverted_within_90d (measurement only, never a scorer's y)"
    soft "train-risk reverted_within_90d" \
        "$PYTHON" run_scout.py --quiet ml train-risk --dataset "$dataset" --out "$models_dir" \
        --label reverted_within_90d
    banner "models 4/5: ml similar --evaluate (neighbour lift on the test split)"
    "$PYTHON" run_scout.py --quiet ml similar --dataset="$dataset" --evaluate
    banner "models 5/5: train-pr-job (Azure job model; kept only if it beats heuristic_p)"
    soft "train-pr-job" \
        "$PYTHON" run_scout.py --quiet ml train-pr-job --repo-type "$REPO" --repo-root "$CLONE" --out "$models_dir"
fi

if wanted online; then
    banner "online 1/3: ml walk-forward (held-out commits into the ledger)"
    soft "walk-forward" \
        "$PYTHON" run_scout.py --quiet ml walk-forward --repo-type "$REPO" --dataset "$dataset" \
        --model "$models_dir/risk-bug_introducing.joblib"
    banner "online 2/3: ml watch (open PR heads, phase-0 score, once each)"
    if [[ "$OFFLINE" -eq 1 ]]; then
        note "skipped (--offline)"
    elif [[ -z "${GITHUB_TOKEN:-}${GH_TOKEN:-}" ]]; then
        note "skipped: set GITHUB_TOKEN (or put it in .env); unauthenticated GitHub allows 60 calls an hour"
    else
        soft "watch" \
            "$PYTHON" run_scout.py --quiet ml watch --repo-type "$REPO" --remote "sonic-net/$REPO" \
            --max-prs "$WATCH_MAX"
    fi
    banner "online 3/3: ml grade (fill outcomes from Azure, score every model)"
    soft "grade" "$PYTHON" run_scout.py --quiet ml grade --repo-type "$REPO"
fi

if wanted backtest; then
    banner "backtest: detector D6 on the seed corpus"
    if [[ "$REPO" != sonic-buildimage ]]; then
        note "skipped: the seed corpus exists for sonic-buildimage only"
    elif [[ -f "$SCOUT_EVAL_CACHE/backtest/sonic-buildimage/manifest.json" ]]; then
        note "pinned corpus found; capturing any missing fixtures, then replaying"
        if [[ "$OFFLINE" -eq 1 ]]; then
            soft "backtest" "$PYTHON" run_scout.py --quiet backtest
        else
            soft "backtest" "$PYTHON" run_scout.py --quiet backtest --capture
        fi
    elif [[ "$OFFLINE" -eq 1 ]]; then
        note "skipped (--offline): no pinned corpus yet, and pinning one needs the network"
    else
        note "first run: mining the corpus and capturing 40 fixtures takes a few minutes"
        soft "backtest" "$PYTHON" run_scout.py --quiet backtest --capture
    fi
    SCORECARD="$SCOUT_EVAL_CACHE/backtest/sonic-buildimage/scorecard.json"
    if [[ "$REPO" == sonic-buildimage && -f "$SCORECARD" ]]; then
        SCORECARD="$SCORECARD" "$PYTHON" - <<'EOF'
import json
import os

card = json.load(open(os.environ["SCORECARD"]))
rows = card["rows"]
m = card["metrics"]["architecture_aware"]
for kind in ("incident", "control"):
    mine = [r for r in rows if r["kind"] == kind]
    reach = sum(1 for r in mine if r["affected"])
    flagged = sum(1 for r in mine if r["architecture_aware"]["flagged"])
    print(f"  -> {kind}s: {len(mine)}; reach any platform {reach}; D6 flagged {flagged}")
print(f"  -> recall {m['recall']['k']}/{m['recall']['n']}, control flag rate "
      f"{m['control_flag_rate']['k']}/{m['control_flag_rate']['n']}. A change reaching no platform is invisible "
      "to D6 by design: it only looks for platforms PR CI never builds.")
EOF
    fi
fi

banner "recap"
REPO="$REPO" MODELS="$models_dir" DATASET="$dataset" EVAL="$SCOUT_EVAL_CACHE" "$PYTHON" - <<'EOF'
import json
import os
from pathlib import Path

repo, models, dataset, ev = (os.environ[k] for k in ("REPO", "MODELS", "DATASET", "EVAL"))


def load(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.is_file() else None


def mark(ok):
    return "ok " if ok else "-- "


cal = Path(ev) / "calibration" / repo
index = load(cal / "model-index.json") or {}
balance = index.get("class_balance") or {}
print(f"{mark(balance)}calibration  {balance.get('prs', 0)} PRs, {balance.get('pr_job_rows', 0)} (PR, job) rows, "
      f"{balance.get('pr_job_positives', 0)} failures")
card = load(Path(dataset) / "dataset_card.json")
print(f"{mark(card)}dataset      {card['commits'] if card else 0} commits in {dataset}")
for name in ("risk-bug_introducing", "risk-bug_introducing-final", "risk-reverted_within_90d"):
    model = load(Path(models) / f"{name}.model_card.json")
    if model:
        best = model["test_metrics"].get(model["selected_model"], {})
        print(f"ok  {name:<28} test PR-AUC {best.get('pr_auc', {}).get('value')}  "
              f"ROC-AUC {best.get('roc_auc', {}).get('value')}")
    else:
        print(f"--  {name}")
pr_job = load(Path(models) / f"pr-job-{repo}.json")
if pr_job:
    test = pr_job["metrics"]["test"]
    print(f"ok  pr-job model               test PR-AUC {test['model_pr_auc']} vs heuristic {test['heuristic_pr_auc']}; "
          f"serving {pr_job['selected']}")
else:
    print("--  pr-job model")
online = load(Path(ev) / "online" / repo / "online-scorecard.json")
print(f"{mark(online)}online       {online['rows'] if online else 0} ledger rows over "
      f"{len(online['models']) if online else 0} model(s)")
if repo == "sonic-buildimage":
    bt = load(Path(ev) / "backtest" / repo / "scorecard.json")
    if bt:
        rec = bt["metrics"]["architecture_aware"]["recall"]
        ctl = bt["metrics"]["architecture_aware"]["control_flag_rate"]
        print(f"ok  backtest     D6 recall {rec['k']}/{rec['n']}, control flags {ctl['k']}/{ctl['n']}")
    else:
        print("--  backtest")
EOF

if [[ ${#FAILED[@]} -gt 0 ]]; then
    printf '\nSteps that did not complete: %s\n' "${FAILED[*]}"
fi
printf '\ndemo-setup: done in %ss (repo=%s, clone=%s)\n' "$((SECONDS - started))" "$REPO" "$CLONE"
printf 'Show it: ./demo.sh %s\n' "$REPO"
