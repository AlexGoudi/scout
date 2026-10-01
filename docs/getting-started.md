# Getting started

This page takes you from a fresh checkout to a brief, a review and a break-risk score. Each step
links to the page that explains it in depth.

## Install

From the root of a checkout of this repository:

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt      # add requirements-dev.txt for tests
```

Python 3.10 or later and `git` are required. The commands below use `python3`; run them with the
virtual environment's interpreter, or activate it first.

## 1. A brief, offline

The repository ships pinned tree fixtures, so the static stage runs with no network and no clone:

```bash
python3 run_scout.py brief --fixture tests/fixtures/trees/sonic-buildimage-master-62cfe50.json \
  --output scout-brief.json
```

The log line reports how the platform count was derived: declarations, less shared directories,
plus symlinked platforms, then how many of those platforms PR CI builds, never builds, or builds
only on some architectures. The brief itself is `scout-brief.json`
([scout-hld.md](scout-hld.md) section 4.4).

## 2. A review, offline

A recorded review case replays the model's answers from disk:

```bash
python3 run_scout.py review --fixture tests/fixtures/demo/pmon-24811/review.json \
  --provider replay --output-dir out/
```

This writes `scout-brief.json`, `scout-report.json`, `scout-comment.md` and `scout-run.jsonl` into
`out/`. Open `scout-comment.md`: it separates **facts**, computed from the tree and the pipeline,
from **judgements**, the model's reading of the evidence. `--provider none` skips the model and
reports the facts only.

## 3. A review of a real pull request

Against a remote, Scout fetches only trees and the blobs it reads, never a full clone:

```bash
python3 run_scout.py --remote sonic-net/sonic-buildimage review --pr 24811 --provider none \
  --output-dir out-24811/
```

For model judgements, run an ollama server and use `--provider ollama` (default model
`qwen2.5:7b-instruct`; see `--model` and `--ollama-url` in [cli-reference.md](cli-reference.md#review)).
`./demo-serve.sh` starts one; see [demos.md](demos.md).

A local clone works too: `--repo-root ~/data/git/sonic-buildimage review --range BASE..HEAD`.

## 4. Break-risk scoring

Per-job break-risk scores need the calibration data mined from git history and Azure DevOps job
results. With a full upstream clone:

```bash
git clone https://github.com/sonic-net/sonic-mgmt.git ~/data/git/sonic-mgmt
CLONE=~/data/git/sonic-mgmt
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-mgmt mine-git-dumps --revision origin/master
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-mgmt mine-azure
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-mgmt mine-labels
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-mgmt score-pr --base origin/master
```

`score-pr` prints one probability per gold job for the clone's `HEAD` against `origin/master`. The
mining steps are explained in [calibration-and-labels.md](calibration-and-labels.md) and the
score in [scoring.md](scoring.md). Mined data goes to `~/.cache/sonic-scout/repos/eval/` unless
`--cache-dir` or `SCOUT_CACHE_DIR` says otherwise.

## 5. Everything at once

```bash
./demo-setup.sh sonic-buildimage     # mine, label, build the dataset, train, backtest
./demo.sh sonic-buildimage           # show the review path and the evaluation
```

See [demos.md](demos.md) for options, and [models-and-evaluation.md](models-and-evaluation.md) for
what gets trained and how it is graded.

## Credentials

None are required. Azure DevOps and git fetches are anonymous. GitHub's REST API, used by
`ml watch`, allows about 60 unauthenticated requests an hour; export `GITHUB_TOKEN` or `GH_TOKEN`,
or put `GITHUB_TOKEN=...` in a `.env` file in the repository root for the demo scripts. `.env` is
gitignored; never commit it.
