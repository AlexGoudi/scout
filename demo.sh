#!/usr/bin/env bash
# Show everything Scout does, as if reviewing PRs right now (advisory only).
#
#   ./demo.sh                    both repos
#   ./demo.sh sonic-buildimage   #24811 and #20860 from pinned fixtures, plus buildimage evaluation
#   ./demo.sh sonic-mgmt         sonic-net/sonic-mgmt#23052 from the clone, plus mgmt evaluation
#   ./demo.sh TARGET [OUT_DIR]   keep the artifacts in OUT_DIR (default: a temp dir)
#
# Three parts, each skipped cleanly when its ./demo-setup.sh step has not run:
#   1. tree     the static stage over a whole tree: platforms PR CI never builds
#   2. review   per PR: the comment, brief, agent questions, phase-0 job scores, commit risk
#   3. eval     how good it is: D6 backtest, model cards against baselines, online ledger
#
# With a provider of ollama, run ./demo-serve.sh in another terminal first. With no server
# answering, the review still runs but degraded: the brief and the deterministic findings only.
#
# Environment (see demo-env.sh):
#   SCOUT_DEMO_PARTS           which parts, comma-separated (default: tree,review,eval)
#   SCOUT_DEMO_PROVIDER        buildimage fixtures: ollama | replay | none (default: ollama)
#   SCOUT_DEMO_MGMT_PROVIDER   live mgmt #23052: none | ollama (default: none)
#   SCOUT_OLLAMA_MODEL / SCOUT_OLLAMA_URL   used when provider is ollama
#   SCOUT_BUILDIMAGE_CLONE     sonic-buildimage checkout (default: ~/data/git/sonic-buildimage)
#   SCOUT_TARGET_REPO, else SCOUT_CLONE     sonic-mgmt checkout (default: ~/data/git/sonic-mgmt)
#   PYTHON
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
case "${1:-}" in
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
esac
# shellcheck source=demo-env.sh
source "$ROOT/demo-env.sh"
TARGET="${1:-all}"
PARTS=" ${SCOUT_DEMO_PARTS:-tree,review,eval} "
PARTS="${PARTS//,/ }"
FIXTURE_PROVIDER="$SCOUT_DEMO_PROVIDER"
MGMT_PROVIDER="$SCOUT_DEMO_MGMT_PROVIDER"

BI_REPO="${SCOUT_BUILDIMAGE_CLONE:-$HOME/data/git/sonic-buildimage}"
MGMT_REPO="${SCOUT_TARGET_REPO:-${SCOUT_CLONE:-$HOME/data/git/sonic-mgmt}}"
# sonic-net/sonic-mgmt#23052 — merge 627d89cfbf (2026-06-02)
MGMT_PR=23052
MGMT_PR_URL="https://github.com/sonic-net/sonic-mgmt/pull/${MGMT_PR}"
MGMT_PR_TITLE="Infra changes to support generic HWSKU"
MGMT_PR_RANGE='b9ae67707e^..627d89cfbf'
MGMT_PR_HEAD='627d89cfbf'

out="${2:-$(mktemp -d -t scout-demo.XXXXXX)}"
mkdir -p "$out"

part() {
    [[ "$PARTS" == *" $1 "* ]]
}

# Appends --model and --ollama-url when provider is ollama (nameref to caller's array).
demo_ollama_review_args() {
    local -n _out=$1
    local provider=$2
    _out=()
    if [[ "$provider" == "ollama" ]]; then
        _out=(--model "${SCOUT_OLLAMA_MODEL:-qwen2.5-coder:7b}" --ollama-url "${SCOUT_OLLAMA_URL:-http://127.0.0.1:11435}")
    fi
}

section() {
    printf '\n████ %s\n' "$1"
}

pr_banner() {
    local repo_slug="$1" pr="$2" url="$3" title="$4"
    printf '\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
    printf 'Assessing PR: %s #%s\n' "$repo_slug" "$pr"
    printf '%s\n' "$url"
    printf '%s\n' "$title"
    printf '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
}

sub() {
    printf '\n── %s\n' "$1"
}

# ---------------------------------------------------------------------------------------------
# Part 1: the static stage over a whole tree
# ---------------------------------------------------------------------------------------------

show_tree() {
    local fixture="$1" label="$2"
    local brief="$out/tree-$label.json"
    "$PYTHON" run_scout.py --quiet brief --fixture "$fixture" --output "$brief" 2>"$brief.log"
    BRIEF="$brief" LABEL="$label" "$PYTHON" - <<'EOF'
import json
import os

brief = json.load(open(os.environ["BRIEF"]))
head = brief["brief"]
cov = brief["coverage"]
platforms = cov["platforms_in_tree"]
uncovered, ambiguous = len(cov["uncovered"]), len(cov["ambiguous"])
print(f"{os.environ['LABEL']} at {str(head.get('head_sha') or head.get('rev') or '')[:9]} (pinned fixture, no network):")
print(f"  {cov['declarations_in_tree']} platform declarations; {len(cov['excluded_as_non_platform'])} shared "
      f"directories excluded, {cov['aliased_platforms']} symlinked platforms added -> {platforms} platforms")
print(f"  PR CI job groups: {cov['job_groups']} ({', '.join(cov['job_group_names'])})")
print(f"  built by PR CI: {len(cov['covered'])}   never built: {uncovered}   ambiguous architecture: {ambiguous}")
print(f"  never built, string equality: {uncovered + ambiguous} ({100 * (uncovered + ambiguous) / platforms:.0f}%); "
      f"the brief hands the {ambiguous} ambiguous ones to the agent with the rule's answer")
EOF
}

run_tree() {
    section "Part 1: the static stage over a whole tree"
    show_tree tests/fixtures/trees/sonic-buildimage-master-62cfe50.json sonic-buildimage
}

# ---------------------------------------------------------------------------------------------
# Part 2: reviewing PRs
# ---------------------------------------------------------------------------------------------

print_brief_summary() {
    local brief_path="$1"
    BRIEF="$brief_path" "$PYTHON" - <<'EOF'
import json
import os

brief = json.load(open(os.environ["BRIEF"]))
cov = brief.get("coverage", {})
print(f"Static brief: {cov.get('platforms_in_tree', '?')} entities in tree, {cov.get('job_groups', '?')} CI job groups")
print(f"  affected / covered / uncovered / ambiguous: {len(cov.get('affected', []))} / {len(cov.get('covered', []))} / "
      f"{len(cov.get('uncovered', []))} / {len(cov.get('ambiguous', []))}")
print("  hotspots (score = sum of its components):")
for h in brief.get("hotspots", [])[:8]:
    print(f"    {h.get('score', 0):6.2f}  {h['path']}  ({h['path_class']})")
more = len(brief.get("hotspots", [])) - 8
if more > 0:
    print(f"    ... {more} more")
EOF
}

print_questions_summary() {
    local out_dir="$1"
    local brief_path="$out_dir/scout-brief.json"
    local report_path="$out_dir/scout-report.json"
    [[ -f "$brief_path" ]] || return 0
    BRIEF="$brief_path" REPORT="$report_path" "$PYTHON" - <<'EOF'
import json
import os
import textwrap

brief = json.load(open(os.environ["BRIEF"]))
questions = brief.get("questions", [])
report = json.load(open(os.environ["REPORT"])) if os.path.isfile(os.environ["REPORT"]) else {}
run = report.get("run", {})
print(f"Agent: {run.get('questions_answered', 0)} of {run.get('questions_asked', len(questions))} question(s) answered"
      f"{', degraded: ' + report['degraded_reason'] if report.get('degraded_reason') else ''}")
for q in questions:
    rule = f"  [{q['rule']}]" if q.get("rule") else ""
    print(f"  {q.get('id', '?')}{rule}: {textwrap.shorten(q.get('ask', ''), width=100, placeholder='…')}")
EOF
}

# Phase-0 per-job break risk from the calibration join; needs demo-setup's calibration step.
print_score_summary() {
    local repo_type="$1" clone="$2" brief_path="$3"
    local root_args=()
    [[ -d "$clone/.git" ]] && root_args=(--repo-root "$clone")
    if ! "$PYTHON" run_scout.py "${root_args[@]}" --repo-type "$repo_type" score-pr \
            --brief "$brief_path" >"$brief_path.score.json" 2>"$brief_path.score.err"; then
        echo "Phase-0 job risk: skipped (run ./demo-setup.sh $repo_type for calibration)"
        return 0
    fi
    SCORE="$brief_path.score.json" PLAN="$SCOUT_EVAL_CACHE/calibration/$repo_type/scoring-plan.json" \
        "$PYTHON" - <<'EOF'
import json
import os

p = json.load(open(os.environ["SCORE"]))
plan = json.load(open(os.environ["PLAN"])) if os.path.isfile(os.environ["PLAN"]) else {}
job_base = plan.get("job_base") or {}
jobs = sorted(p.get("score_by_job", {}).items(), key=lambda x: -x[1])
files = "every changed file" if p.get("files_complete") else "the brief's hotspots only (no checkout)"
print(f"Phase-0 job risk (P(gold job fails), from {files}; {len(p.get('files', []))} path(s), base rates from "
      f"{p.get('job_base_source')}):")
raised = 0
for job, score in jobs[:6]:
    base = job_base.get(job)
    delta = f"  base {base:.4f}, raised" if base is not None and score > base + 1e-9 else ""
    raised += bool(delta)
    print(f"  {job:<24} {score:.4f}  {'#' * max(1, round(score * 200))}{delta}")
if len(jobs) > 6:
    print(f"  ... {len(jobs) - 6} more job(s), lowest {jobs[-1][1]:.4f}")
if job_base and not any(score > job_base.get(job, 1) + 1e-9 for job, score in jobs):
    print("  No changed path lifted any job above its base rate: every score is that job's train-split failure rate.")
EOF
}

# Commit risk and similar history from the ML baseline; needs demo-setup's dataset and models steps.
print_commit_risk() {
    local repo_type="$1" clone="$2" commit="$3" dir="$4"
    local model="models/$repo_type/risk-bug_introducing-final.joblib"
    local card="models/$repo_type/risk-bug_introducing-final.model_card.json"
    if [[ ! -f "$model" || ! -d "$clone/.git" ]]; then
        echo "Commit risk: skipped (run ./demo-setup.sh $repo_type for the dataset and models)"
        return 0
    fi
    local dataset
    dataset="corpus/$("$PYTHON" -c "import json; print(json.load(open('$card'))['dataset']['path_name'])")"
    if ! "$PYTHON" run_scout.py --quiet ml score --dataset "$dataset" --model "$model" --repo "$clone" \
            --commit "$commit" -k 3 >"$dir/ml-score.json" 2>"$dir/ml-score.err"; then
        echo "Commit risk: skipped ($(tail -1 "$dir/ml-score.err"))"
        if grep -q BitGenerator "$dir/ml-score.err"; then
            echo "  The model was saved by another numpy: run with the interpreter that trained it, or retrain."
        fi
        return 0
    fi
    SCORE="$dir/ml-score.json" "$PYTHON" - <<'EOF'
import json
import os
import textwrap

b = json.load(open(os.environ["SCORE"]))
risk, commit = b["risk"], b["commit"]
where = f"in the dataset, {commit['split']} split" if commit["in_dataset"] else "outside the dataset"
print(f"Commit risk ({risk['label']}, {risk['model']}): P = {risk['probability']:.3f}, "
      f"percentile {risk['percentile']:.0f}, base rate {risk['base_rate']:.3f}  [{commit['sha'][:9]}, {where}]")
for reason in risk.get("reasons", [])[:3]:
    print(f"  {reason['direction']:<6} {reason['feature']} ({reason['contribution']:+.2f})")
print("Most similar earlier commits, with outcomes as known when this one landed:")
for n in b.get("similar", [])[:3]:
    known = n["outcomes_known_at_query"]
    flags = [name for name, value in known.items() if value]
    print(f"  {n['score']:.2f}  {n['sha'][:9]}  {textwrap.shorten(n['subject'], 70, placeholder='…')}"
          f"  {'<- ' + ', '.join(flags) if flags else ''}")
EOF
}

assess_fixture_pr() {
    local case="$1" repo_slug="$2" pr="$3" url="$4" title="$5"
    pr_banner "$repo_slug" "$pr" "$url" "$title"
    local dir="$out/${case}-${FIXTURE_PROVIDER}"
    mkdir -p "$dir"
    local ollama_extra=()
    demo_ollama_review_args ollama_extra "$FIXTURE_PROVIDER"
    if ! "$PYTHON" run_scout.py review --fixture "tests/fixtures/demo/$case/review.json" \
            --provider "$FIXTURE_PROVIDER" "${ollama_extra[@]}" --output-dir "$dir" >"$dir.log" 2>&1; then
        cat "$dir.log" >&2
        echo "demo: review failed for PR #$pr ($case)" >&2
        exit 1
    fi
    sub "PR comment (scout-comment.md)"
    cat "$dir/scout-comment.md"
    sub "What the stages produced"
    print_brief_summary "$dir/scout-brief.json"
    print_questions_summary "$dir"
    sub "Break risk"
    print_score_summary sonic-buildimage "$BI_REPO" "$dir/scout-brief.json"
    local head
    head="$("$PYTHON" -c "import json; print(json.load(open('tests/fixtures/demo/$case/changeset.json'))['head_sha'])")"
    print_commit_risk sonic-buildimage "$BI_REPO" "$head" "$dir"
}

assess_mgmt_pr_23052() {
    pr_banner "sonic-net/sonic-mgmt" "$MGMT_PR" "$MGMT_PR_URL" "$MGMT_PR_TITLE"
    if [[ ! -d "$MGMT_REPO/.git" ]]; then
        echo "demo: no checkout at $MGMT_REPO; clone sonic-mgmt or set SCOUT_CLONE (skipping this PR)" >&2
        return 0
    fi
    local dir="$out/mgmt-pr-${MGMT_PR}-${MGMT_PROVIDER}"
    mkdir -p "$dir"
    local ollama_extra=()
    demo_ollama_review_args ollama_extra "$MGMT_PROVIDER"
    if ! "$PYTHON" run_scout.py --repo-root "$MGMT_REPO" --repo-type sonic-mgmt \
            review --range "$MGMT_PR_RANGE" --provider "$MGMT_PROVIDER" "${ollama_extra[@]}" \
            --repo-name sonic-net/sonic-mgmt --output-dir "$dir" >"$dir.log" 2>&1; then
        cat "$dir.log" >&2
        echo "demo: review failed for PR #$MGMT_PR" >&2
        exit 1
    fi
    sub "PR comment (scout-comment.md)"
    cat "$dir/scout-comment.md"
    sub "What the stages produced"
    print_brief_summary "$dir/scout-brief.json"
    print_questions_summary "$dir"
    sub "Break risk"
    print_score_summary sonic-mgmt "$MGMT_REPO" "$dir/scout-brief.json"
    print_commit_risk sonic-mgmt "$MGMT_REPO" "$MGMT_PR_HEAD" "$dir"
}

# ---------------------------------------------------------------------------------------------
# Part 3: how good is it
# ---------------------------------------------------------------------------------------------

show_eval() {
    local repo="$1"
    section "Part 3: how good is it ($repo)"
    if [[ "$repo" == sonic-buildimage && -f "$SCOUT_EVAL_CACHE/backtest/$repo/manifest.json" ]]; then
        sub "Backtest: replaying D6 over the pinned seed corpus, offline"
        "$PYTHON" run_scout.py --quiet backtest >"$out/backtest.log" 2>&1 || tail -3 "$out/backtest.log"
    fi
    REPO="$repo" EVAL="$SCOUT_EVAL_CACHE" "$PYTHON" - <<'EOF'
import json
import os
from pathlib import Path

repo, ev = os.environ["REPO"], Path(os.environ["EVAL"])


def load(path):
    return json.loads(path.read_text()) if path.is_file() else None


def ci(m):
    return f"{m['k']}/{m['n']} = {m['rate']:.2f} (95% CI {m['ci95'][0]:.2f}-{m['ci95'][1]:.2f})"


print()
if repo == "sonic-buildimage":
    bt = load(ev / "backtest" / repo / "scorecard.json")
    if bt:
        print(f"D6 backtest ({bt['items']['incidents']} incidents, {bt['items']['controls']} controls, {bt['adjudication']}):")
        for reading in ("string_equality", "architecture_aware"):
            m = bt["metrics"][reading]
            print(f"  {reading:<19} recall {ci(m['recall'])}   control flags {ci(m['control_flag_rate'])}")
    else:
        print("D6 backtest: not captured yet (./demo-setup.sh sonic-buildimage --only backtest)")

cal = load(ev / "calibration" / repo / "model-index.json")
if cal:
    b = cal["class_balance"]
    print(f"\nAzure gold labels: {b['prs']} PRs, {b['pr_job_rows']} (PR, job) rows, {b['pr_job_positives']} failures "
          f"({100 * b['positive_rate_job']:.1f}% of job rows)")

models = Path("models") / repo
for name, title in (("risk-bug_introducing", "Commit risk (bug_introducing)"),
                    ("risk-reverted_within_90d", "Revert risk (measurement only, never a scorer's y)")):
    # A --final card's test metrics are measured before the refit, so they are held out too.
    card = load(models / f"{name}.model_card.json") or load(models / f"{name}-final.model_card.json")
    if not card:
        continue
    identity = [f for f in card["features"] if f.startswith(("author_", "committer_", "file_prior_authors"))]
    if identity:
        title += f" [trained with {len(identity)} author feature(s): retrain]"
    test = card["rows"]["test"]
    print(f"\n{title}: test split {test['rows']} commits, {test['positives']} positive; selected {card['selected_model']}")
    print(f"  {'model':<18} {'PR-AUC':>7} {'ROC-AUC':>8} {'recall@20% effort':>18}")
    for model, metrics in card["test_metrics"].items():
        star = "*" if model == card["selected_model"] else " "
        values = [metrics.get(key, {}).get("value") for key in ("pr_auc", "roc_auc", "recall_at_20pct_effort")]
        print(f" {star}{model:<18}" + "".join(f"{v:>9.3f}" if v is not None else f"{'-':>9}" for v in values))

pr_job = load(models / f"pr-job-{repo}.json")
if pr_job:
    m = pr_job["metrics"]
    print(f"\nAzure job model vs the phase-0 heuristic (PR-AUC): valid {m['validation']['model_pr_auc']} vs "
          f"{m['validation']['heuristic_pr_auc']}, test {m['test']['model_pr_auc']} vs {m['test']['heuristic_pr_auc']}"
          f" -> serving {pr_job['selected']}")

online = load(ev / "online" / repo / "online-scorecard.json")
if online:
    print(f"\nOnline ledger: {online['rows']} row(s)")
    for name, entry in online["models"].items():
        g = entry["graded"]
        grain = entry.get("job_grain")
        extra = f"; job grain n={grain['n']} ROC-AUC {grain['roc_auc']}" if grain else ""
        print(f"  {name:<34} graded {g['n']} ({g['positives']} positive), pending {entry['pending']}, "
              f"ROC-AUC {g['roc_auc']} PR-AUC {g['pr_auc']}{extra}")
else:
    print("\nOnline ledger: empty (./demo-setup.sh --only online)")
EOF
}

# ---------------------------------------------------------------------------------------------

run_repo() {
    local repo="$1"
    if part review; then
        section "Part 2: reviewing PRs ($repo)"
        if [[ "$repo" == sonic-buildimage ]]; then
            assess_fixture_pr pmon-24811 sonic-net/sonic-buildimage 24811 \
                "https://github.com/sonic-net/sonic-buildimage/pull/24811" \
                "Shared Arista pmon_daemon_control.json (symlink reach)"
            assess_fixture_pr prestera-20860 sonic-net/sonic-buildimage 20860 \
                "https://github.com/sonic-net/sonic-buildimage/pull/20860" \
                "Marvell-prestera platform_asic renames across architectures"
        else
            assess_mgmt_pr_23052
        fi
    fi
    if part eval; then
        show_eval "$repo"
    fi
}

printf 'demo providers: buildimage=%s  mgmt=%s' "$FIXTURE_PROVIDER" "$MGMT_PROVIDER"
if [[ "$FIXTURE_PROVIDER" == "ollama" || "$MGMT_PROVIDER" == "ollama" ]]; then
    printf '  (ollama %s @ %s)' "${SCOUT_OLLAMA_MODEL:-qwen2.5-coder:7b}" "${SCOUT_OLLAMA_URL:-http://127.0.0.1:11435}"
fi
printf '\nparts:%s\n' "$PARTS"

case "$TARGET" in
    sonic-buildimage|sonic-mgmt|all) ;;
    *)
        echo "demo: unknown target: $TARGET (use sonic-buildimage, sonic-mgmt, or all)" >&2
        exit 2
        ;;
esac

part tree && run_tree
case "$TARGET" in
    sonic-buildimage) run_repo sonic-buildimage ;;
    sonic-mgmt) run_repo sonic-mgmt ;;
    all)
        run_repo sonic-buildimage
        run_repo sonic-mgmt
        ;;
esac

printf '\nArtifacts: %s\n' "$out"
printf 'Refresh the data behind parts 2 and 3: ./demo-setup.sh [sonic-mgmt|sonic-buildimage|all]\n'
