# SONiC Scout

Scout reads an incoming change to a SONiC repository and predicts what it puts at risk,
citing file and line, before an image is built. It is **advisory only and never blocks a
merge.**

Scout analyzes a separate repository, named on every invocation: `--repo-root` for a
checkout on disk, or `--remote owner/repo` to fetch one anonymously with no clone. It only
ever reads that tree. `sonic-net/sonic-buildimage` is the primary target and
`sonic-net/sonic-mgmt` is the second adapter.

## What it does

Two halves share one CLI, `python3 run_scout.py`.

**The review path.** `review` runs four stages end to end:

| Stage | Package | Output |
| --- | --- | --- |
| 0, ingest | `ingest.py`, `remote.py`, `source.py` | the change set |
| 1, deterministic static analysis, no model | `static/`, `detectors/` | `scout-brief.json`, the versioned contract stage 2 consumes |
| 2, an agent bounded by the brief | `agent/`, `provider.py`, `ollama.py` | adjudicated answers |
| 3 and 4, citation checks and the report | `agent/checks.py`, `report/` | `scout-report.json` and the rendered `scout-comment.md` |

Detector D6, the CI coverage gap, is the committed detector: it names the platforms a change
reaches that PR CI never builds. The static stage refuses to guess where the tree does not
say, and hands those questions to the agent with the rule's own answer beside them.

**Break-risk scoring and evaluation.**

- `mine-incidents`, `mine-git-dumps`, `mine-azure` and `mine-labels` build per-job gold labels
  from Azure: the image jobs for `sonic-buildimage`, the Pre_test and KVM Elastictest jobs for
  `sonic-mgmt`. The parent pipeline result is never a label.
- `score-pr` is the phase-0 heuristic: a per-job break risk from the paths a branch changes,
  offline, JSON on stdout.
- `dataset` and `ml` build a commit dataset with SZZ labels, train risk baselines against
  that heuristic, and keep an online ledger of scored PRs.
- `backtest` grades D6 on a seed corpus of `sonic-buildimage` reverts.

Mined data lives in the cache, `~/.cache/sonic-scout/repos` by default (`--cache-dir`), not
in this directory.

## Install

Python 3.10 or later, and `git` on `PATH`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime plus pytest, jsonschema, ruff, flake8
.venv/bin/pip install -r requirements.txt       # or runtime only
```

## Quick start

```bash
# The static brief over a pinned tree fixture: no network, no clone, no model
python3 run_scout.py brief --fixture tests/fixtures/trees/sonic-buildimage-master-62cfe50.json --stdout

# A full review of a recorded PR, offline, replaying the model's recorded answers
python3 run_scout.py review --fixture tests/fixtures/demo/prestera-20860/review.json \
    --provider replay --output-dir /tmp/scout-review

# A pull request on upstream, fetched anonymously; --provider none skips the model
python3 run_scout.py --remote sonic-net/sonic-buildimage review --pr 20860 \
    --provider none --output-dir /tmp/scout-review

# Phase-0 per-job break risk of a branch in a clone (needs mine-labels to have run)
python3 run_scout.py --repo-root ~/data/git/sonic-buildimage score-pr --base origin/master
```

`review` exits 0 even when it runs degraded, for example with no model reachable: the brief
and the deterministic findings still ship. A live model is a local ollama server
(`--provider ollama`, the default).

### Demos

```bash
./demo-setup.sh sonic-buildimage   # calibration, score-pr, dataset, models, online ledger, backtest
./demo-serve.sh                    # optional, in another terminal: the ollama server for live answers
./demo.sh sonic-buildimage         # whole-tree headline, example PRs, how good it is
./demo-clean.sh --all              # back to a clean slate
```

Each setup step resumes from cache, and each demo part skips cleanly when its setup step has
not run. `SCOUT_DEMO_PROVIDER=replay ./demo.sh sonic-buildimage` replays recorded answers
instead of asking a live model. All four scripts take `--help`.

## Documentation

The index is [docs/README.md](docs/README.md).

| Document | Covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Install, clones, first runs |
| [CLI reference](docs/cli-reference.md) | Every command and flag |
| [Calibration and labels](docs/calibration-and-labels.md) | Mining, Azure gold labels, the `model-*` join |
| [Scoring](docs/scoring.md) | The phase-0 `score-pr` heuristic |
| [Models and evaluation](docs/models-and-evaluation.md) | Commit dataset, risk models, online ledger, backtest |
| [Demos](docs/demos.md) | `demo-setup.sh`, `demo.sh`, `demo-serve.sh`, `demo-clean.sh` |
| [Adding a repo adapter](docs/adding-a-repo-adapter.md) | Bringing a third repository in |
| [Development](docs/development.md) | Tests, lint, conventions |
| [High-level design](docs/scout-hld.md) | The review path's design |

## Tests and lint

```bash
python3 -m pytest -q          # offline, no model, no network
python3 -m flake8
ruff check --select E,F .
```

Network tests (`integration_test_*.py`) run only when named on the command line with
`SCOUT_NETWORK_TESTS=1`. Two history tests need a `sonic-mgmt` clone, taken from
`SCOUT_TARGET_REPO` or `../sonic-mgmt`, and skip without one.
