# Development

This page covers the code layout, the test suite, linting and the conventions a change must keep.
For what the code does, start with [scout-hld.md](scout-hld.md).

## Setup

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements-dev.txt
```

Python 3.10 or later and a `git` binary on `PATH` are required. `requirements.txt` holds the runtime
dependencies (PyYAML, numpy, pandas, pyarrow, scikit-learn, scipy, joblib); `requirements-dev.txt`
adds pytest, jsonschema, ruff, flake8 and flake8-pyproject. The demo scripts create `.venv/` with
runtime dependencies only, so install the dev requirements into it before running tests there.

## Layout

| Path | Contents |
| --- | --- |
| `run_scout.py` | The only entry point; calls `scout_impl.cli.run` |
| `scout_impl/cli.py` | Review-path, mining and scoring commands; forwards `mine`, `dataset` and `ml` to `mining_cli.py` |
| `scout_impl/mining_cli.py` | The `mine`, `dataset` and `ml` groups |
| `scout_impl/ingest.py`, `remote.py`, `source.py`, `gitcmd.py`, `diffparse.py`, `models.py` | Stage 0: change sets, local and remote sources, git, diff parsing |
| `scout_impl/static/` | Stage 1: entity index, pipeline parser, coverage query, hotspots, brief |
| `scout_impl/detectors/` | Detector registry and D6 |
| `scout_impl/agent/`, `provider.py`, `ollama.py` | Stage 2: toolbox, budget, checks, prompts, providers |
| `scout_impl/report/` | Stage 4: report builder, confidence bands, comment rendering |
| `scout_impl/core/` | Orchestration of the static stage and the full review, run log |
| `scout_impl/repos/` | Repository adapters, calibration tables, commit-dataset taxonomies |
| `scout_impl/incidents.py`, `scout_impl/eval/` | Revert mining, calibration miners, the label join, `score-pr`, fidelity, corpus and backtest |
| `scout_impl/mining/`, `scout_impl/dataset/`, `scout_impl/ml/` | Commit records, dataset build, risk models, similarity, online ledger |
| `schemas/` | JSON schemas: `scout-brief-1.0.json`, `scout-report-2.0.json`, `scout-commit-1.1.json` |
| `tests/` | Unit tests, the conformance suite and pinned fixtures |
| `corpus/`, `models/`, `.scout-cache/` | Generated; gitignored |

Mined data does not live in the repository; it lives in the cache described in
[calibration-and-labels.md](calibration-and-labels.md#cache-layout).

## Tests

```bash
python3 -m pytest -q
```

`pytest.ini` collects `test_*.py` and `unit_test_*.py` under `tests/`. The default run is offline:
no network, no model server. Tests that need a real `sonic-mgmt` history use the clone named by
`SCOUT_TARGET_REPO` (default: `../sonic-mgmt` beside this repository) and skip when there is none.

Integration tests are named `integration_test_*.py`, are not collected by default, and skip unless
opted in:

| File | Opt-in | Other variables |
| --- | --- | --- |
| `integration_test_remote_fetch.py` | `SCOUT_NETWORK_TESTS=1` | `SCOUT_NETWORK_REMOTE` (default `sonic-net/sonic-buildimage`) |
| `integration_test_ollama.py` | `SCOUT_OLLAMA_TESTS=1` | `SCOUT_OLLAMA_URL` (default `http://127.0.0.1:11435`), `SCOUT_OLLAMA_MODEL` (default `qwen2.5-coder:7b`) |
| `integration_test_review_ollama.py` | `SCOUT_OLLAMA_TESTS=1` | `SCOUT_OLLAMA_URL`, `SCOUT_BUILDIMAGE_REPO`; `SCOUT_RECORD_DEMO=1` re-records `tests/fixtures/demo/<case>/` |

```bash
SCOUT_NETWORK_TESTS=1 python3 -m pytest tests/integration_test_remote_fetch.py -q -s
```

A re-recorded demo fixture is replayed offline immediately, and the recording is refused unless the
replay reproduces the live report apart from timings.

### Fixtures

- `tests/fixtures/trees/`: pinned tree fixtures, captured with `tests/fixtures/capture_tree.py`
  from a remote or checkout. Brief and coverage tests read them offline.
- `tests/fixtures/demo/<case>/`: recorded review cases (change set, trees, model responses,
  artifacts) used by `review --fixture` and `demo.sh`.
- `tests/fixtures/replay/`: recorded provider responses.

## Lint

```bash
python3 -m flake8
ruff check --select E,F .
```

Both must be clean. Configuration is in `pyproject.toml` (with a `[flake8]` mirror in `.flake8`);
the line length is 120, and the calibration tables and `eval/join_labels.py` are exempt from E501.
`pyproject.toml` also selects ruff's `I` (import order) and `UP` (pyupgrade) rules; plain
`ruff check .` reports many such findings that predate this guide. Fix them in one dedicated,
mechanical commit, never mixed into a behavioural change.

## Conventions

- **Imports at the top of the module.** An inline import needs a documented circular-dependency
  reason.
- **Standard library only in miners and evaluation code** (`scout_impl/eval/`, `incidents.py`):
  `urllib`, `json`, `subprocess`. `scout_impl/ml/` and `scout_impl/dataset/` may use the packages in
  `requirements.txt`. Add no dependency without agreement.
- **One concern per JSON file.** Raw dumps stay separate; only `model-*` files join them.
- **`score-pr` stays pure**: a function of the changed paths and `scoring-plan.json`, with no
  Azure or GitHub call, and one output schema.
- **Labels and features.** Never use the parent pipeline result, reverts, or author or vendor
  identity as a scorer's label; never use identity as a feature (`IDENTITY_PREFIXES` in
  `scout_impl/ml/matrix.py`); never train on test PRs or test commits. Split chronologically, by PR
  for the Azure join.
- **Repository specifics live on the adapter.** Path buckets, Azure definition ids, job names and
  gold-label rules belong in `scout_impl/repos/`, not in shared code.

## Versions to bump

| You changed | Bump | Then rerun |
| --- | --- | --- |
| The PR label join or calibration rules | `JOIN_VERSION` in `scout_impl/eval/join_labels.py` | `mine-labels`, PR-job models |
| Commit labels | `LABELS_VERSION` in `scout_impl/dataset/labels.py` | `dataset build`, risk models |
| Commit-record extraction | `EXTRACTOR_VERSION` in `scout_impl/mining/__init__.py` | `dataset build`, risk models |
| Calibration cache mechanics | `CODE_VERSION` in `scout_impl/eval/_cache.py` | every mining step |
| The brief or report shape | The schema file in `schemas/` and its version | tests and fixtures that pin it |

After touching the join, check that `model-schema.json` still describes what `join_labels.py` writes.
More detail is in [calibration-and-labels.md](calibration-and-labels.md#versions-and-what-to-rerun).

## Git

Use conventional commit messages (`feat`, `fix`, `chore`, `docs`, `refactor`, `test`). Never commit
`.env` (it holds `GITHUB_TOKEN`) or any other credential. The committed `.gitignore` covers virtual
environments, run outputs, `corpus/`, `models/`, `.scout-cache/` and `.env`.
