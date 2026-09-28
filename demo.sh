#!/usr/bin/env bash
# Replays the two recorded demo cases, then the first one with no model at all.
# Needs no network, no model and no checkout; the fixtures carry everything.
set -euo pipefail
cd "$(dirname "$0")"

out="${1:-$(mktemp -d -t scout-demo.XXXXXX)}"
mkdir -p "$out"

review() {
    local case="$1" provider="$2"
    local dir="$out/$case-$provider"
    if ! python3 run_scout.py review --fixture "tests/fixtures/demo/$case/review.json" \
            --provider "$provider" --output-dir "$dir" >"$dir.log" 2>&1; then
        cat "$dir.log" >&2
        echo "demo: review failed for $case with provider $provider" >&2
        exit 1
    fi
    printf '\n=============== %s, provider %s ===============\n\n' "$case" "$provider"
    cat "$dir/scout-comment.md"
}

review pmon-24811 replay
review prestera-20860 replay
review pmon-24811 none

printf '\nBriefs, reports, comments and run logs: %s\n' "$out"
