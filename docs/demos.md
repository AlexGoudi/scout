# Demo scripts

Five shell scripts in the repository root wrap the CLI for a guided tour. They call only
`python3 run_scout.py`; anything they show can be reproduced with the commands in
[cli-reference.md](cli-reference.md).

| Script | Purpose |
| --- | --- |
| `demo-env.sh` | Sourced by the others: picks the interpreter, installs requirements, loads `.env`, sets defaults |
| `demo-setup.sh` | Mines, labels, builds the dataset and trains every model for one or both repositories |
| `demo.sh` | Shows the review path on example PRs, then the evaluation results |
| `demo-serve.sh` | Runs a local ollama server for `review --provider ollama` |
| `demo-clean.sh` | Deletes generated outputs, and optionally the evaluation cache |

## Prerequisites

- Upstream clones at `~/data/git/sonic-mgmt` and `~/data/git/sonic-buildimage`, cloned from
  `https://github.com/sonic-net/<repo>` with full history (the commit dataset refuses a shallow
  clone). Other locations are set with the variables below.
- Python 3.10 or later. Unless `PYTHON` is set, `demo-env.sh` creates `.venv/` and installs
  `requirements.txt` into it the first time.
- Optionally, a `.env` file in the repository root holding `GITHUB_TOKEN=...`. It is gitignored,
  read only when neither `GITHUB_TOKEN` nor `GH_TOKEN` is already set, and never printed. Without a
  token, `ml watch` is skipped.
- Optionally, [ollama](https://ollama.com/download) for live reviews.

## `demo-setup.sh`

```bash
./demo-setup.sh [sonic-mgmt|sonic-buildimage|all] [--offline] [--only STEPS] [--skip STEPS]
```

The default repository is `sonic-mgmt` (or `$SCOUT_REPO`). `all` runs both in turn. Steps run in
this order; `--only` and `--skip` take a comma-separated subset:

| Step | Runs |
| --- | --- |
| `fetch` | `git fetch origin master` in the clone |
| `calibration` | `mine-incidents`, `mine-git-dumps --revision origin/master`, `mine-azure`, `mine-labels`, `check-fidelity` |
| `score-pr` | `score-pr --base origin/master` on the clone's `HEAD`, written to `scout-score-pr-<repo>.json` |
| `dataset` | `dataset build --repo-type <repo> --rev origin/master` into `corpus/<repo>-<sha9>/` |
| `models` | `ml train-risk` (held out, then `--final`), `ml train-risk --label reverted_within_90d` (measurement only), `ml similar --evaluate`, `ml train-pr-job` |
| `online` | `ml walk-forward` with the held-out model, `ml watch` (needs a token), `ml grade` |
| `backtest` | `sonic-buildimage` only: `backtest --capture` the first time, then capture of any missing fixture and replay |

`--offline` skips everything that needs the network: `fetch`, `mine-azure`, `ml watch` and corpus
capture. Steps that can fail on thin data (`train-pr-job`, the revert model, the online steps,
the backtest) are reported in a recap instead of aborting the run. Every step resumes from its
cache, so a rerun is cheap.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCOUT_REPO` | `sonic-mgmt` | Target when no repository argument is given |
| `SCOUT_CLONE` | `~/data/git/<repo>` | Clone path |
| `SCOUT_AZURE_WORKERS` | 8 | Passed to `mine-azure --workers` |
| `SCOUT_WATCH_MAX_PRS` | 20 | Passed to `ml watch --max-prs` |
| `PYTHON` | `.venv/bin/python3` | Interpreter |

Outputs: the calibration, online and backtest data in the evaluation cache, `corpus/<repo>-<sha9>/`,
`models/<repo>/`, `scout-corpus-<repo>.jsonl` and `scout-score-pr-<repo>.json` in the repository root.

## `demo.sh`

```bash
./demo.sh [sonic-buildimage|sonic-mgmt|all] [OUT_DIR]
```

The default target is `all`. Artifacts go to `OUT_DIR`, or a new temporary directory whose path is
printed at the end. The demo has three parts, selected by `SCOUT_DEMO_PARTS` (default
`tree,review,eval`); a part whose `demo-setup.sh` step has not run is skipped with a hint.

1. **tree**: `brief --fixture tests/fixtures/trees/sonic-buildimage-master-62cfe50.json`, the static
   stage over a pinned `sonic-buildimage` tree, offline. It prints the declaration and platform
   counts, the PR CI job groups and how many platforms PR CI never builds.
2. **review**: per example PR, the `scout-comment.md` comment, a brief summary, the agent's
   questions, the phase-0 job scores (`score-pr --brief`) and, when models exist, the commit risk
   (`ml score`).
   - `sonic-buildimage`: PRs 24811 and 20860, replayed from `tests/fixtures/demo/pmon-24811/` and
     `tests/fixtures/demo/prestera-20860/` with `review --fixture`.
   - `sonic-mgmt`: PR 23052, reviewed live from the clone as `review --range b9ae67707e^..627d89cfbf`.
3. **eval**: the D6 backtest replay and scorecard, the Azure class balance, the model cards against
   their baselines, the PR-job model against the heuristic, and the online scorecard.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCOUT_DEMO_PARTS` | `tree,review,eval` | Which parts to run |
| `SCOUT_DEMO_PROVIDER` | `ollama` | Provider for the `sonic-buildimage` fixture PRs: `ollama`, `replay` or `none` |
| `SCOUT_DEMO_MGMT_PROVIDER` | `none` | Provider for the live `sonic-mgmt` PR: `none` or `ollama` (it has no recorded answers to replay) |
| `SCOUT_OLLAMA_MODEL` | `qwen2.5-coder:7b` | Passed as `review --model` when the provider is `ollama` |
| `SCOUT_OLLAMA_URL` | `http://127.0.0.1:11435` | Passed as `review --ollama-url` |
| `SCOUT_BUILDIMAGE_CLONE` | `~/data/git/sonic-buildimage` | Clone used for `sonic-buildimage` scores and commit risk |
| `SCOUT_TARGET_REPO`, else `SCOUT_CLONE` | `~/data/git/sonic-mgmt` | Clone used for the `sonic-mgmt` PR |
| `PYTHON` | `.venv/bin/python3` | Interpreter |

With `ollama` selected and no server answering, the review still completes, degraded to the brief
and the deterministic findings. For a fully offline run with recorded answers, use
`SCOUT_DEMO_PROVIDER=replay ./demo.sh sonic-buildimage`.

The demo's default model, `qwen2.5-coder:7b`, differs from the CLI's own default for
`review --model`, `qwen2.5:7b-instruct`.

## `demo-serve.sh`

```bash
./demo-serve.sh [--no-pull]
```

Starts `ollama serve` on the host and port of `SCOUT_OLLAMA_URL` (default `127.0.0.1:11435`; ollama's own
default port is 11434), and pulls `SCOUT_OLLAMA_MODEL` if it
is missing. `--no-pull` only reports a missing model. If a server is already answering at that URL,
the script checks the model and exits. Leave it running in a second terminal; Ctrl-C stops it.

## `demo-clean.sh`

```bash
./demo-clean.sh [--calibration] [--online] [--backtest] [--venv] [--all]
```

Always removes `.scout-cache/`, the contents of `corpus/` and `models/`, and generated
`scout-*.json`, `scout-*.jsonl`, `changeset.json` and `sync.json` files in the repository root.

| Option | Also removes |
| --- | --- |
| `--calibration` | `<eval cache>/calibration` (Azure keeps PR runs only briefly, so older labels cannot be re-fetched) |
| `--online` | `<eval cache>/online`, the ledger |
| `--backtest` | `<eval cache>/backtest`, the pinned corpus and fixtures |
| `--venv` | `.venv/` |
| `--all` | all of the above, plus `__pycache__/` and `.pytest_cache/` |

The evaluation cache is `$SCOUT_EVAL_CACHE`, else `<cache root>/eval` with the cache root
resolved as in [cli-reference.md](cli-reference.md#global-options).
