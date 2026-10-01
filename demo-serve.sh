#!/usr/bin/env bash
# Run the ollama server ./demo.sh talks to when a provider is ollama. Leave it running in a
# second terminal; Ctrl-C stops it.
#
#   ./demo-serve.sh             serve on SCOUT_OLLAMA_URL and make sure SCOUT_OLLAMA_MODEL is present
#   ./demo-serve.sh --no-pull   serve only; report a missing model instead of pulling it
#
# Environment (same defaults as demo-env.sh):
#   SCOUT_OLLAMA_URL     where to listen (default: http://127.0.0.1:11435)
#   SCOUT_OLLAMA_MODEL   model demo.sh asks for (default: qwen2.5-coder:7b)
set -euo pipefail

PULL=1
case "${1:-}" in
    "") ;;
    --no-pull) PULL=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "demo-serve: unknown option: $1" >&2; exit 2 ;;
esac

URL="${SCOUT_OLLAMA_URL:-http://127.0.0.1:11435}"
MODEL="${SCOUT_OLLAMA_MODEL:-qwen2.5-coder:7b}"
HOSTPORT="${URL#*://}"
HOSTPORT="${HOSTPORT%%/*}"

if ! command -v ollama >/dev/null 2>&1; then
    echo "demo-serve: ollama is not installed (https://ollama.com/download)" >&2
    exit 1
fi

up() {
    curl -fsS --max-time 2 "$URL/api/tags" >/dev/null 2>&1
}

has_model() {
    curl -fsS --max-time 5 "$URL/api/tags" | grep -q "\"name\":\"${MODEL}\"\|\"name\":\"${MODEL}:latest\""
}

ensure_model() {
    if has_model; then
        echo "demo-serve: $MODEL is available at $URL"
    elif [[ "$PULL" -eq 1 ]]; then
        echo "demo-serve: pulling $MODEL (first time only)"
        OLLAMA_HOST="$HOSTPORT" ollama pull "$MODEL"
    else
        echo "demo-serve: $MODEL is missing at $URL; run: OLLAMA_HOST=$HOSTPORT ollama pull $MODEL" >&2
    fi
}

if up; then
    echo "demo-serve: an ollama server is already answering at $URL; not starting another"
    ensure_model
    exit 0
fi

# Once the server answers, check the model, then tell the other terminal what to run.
(
    for _ in $(seq 1 60); do
        up && break
        sleep 1
    done
    if up; then
        ensure_model
        echo "demo-serve: ready. In another terminal: SCOUT_DEMO_PROVIDER=ollama ./demo.sh sonic-buildimage"
    else
        echo "demo-serve: server did not answer at $URL within 60s" >&2
    fi
) &
watcher=$!
trap 'kill "$watcher" 2>/dev/null || true' EXIT

echo "demo-serve: starting ollama on $HOSTPORT (Ctrl-C to stop)"
OLLAMA_HOST="$HOSTPORT" ollama serve
