# SONiC Scout documentation

SONiC Scout reads an incoming change to a SONiC repository and predicts what it puts at risk,
citing file and line, before an image is built. It is advisory only and never blocks a merge. It
works on upstream `sonic-net/sonic-buildimage` (will this PR break image assembly?) and
`sonic-net/sonic-mgmt` (will this PR break required tests?).

| Page | Read it to |
| --- | --- |
| [Getting started](getting-started.md) | Install Scout and run a brief, a review and a score |
| [CLI reference](cli-reference.md) | Look up any command, flag, default or exit code |
| [High-level design](scout-hld.md) | Understand the review path: ingest, static brief, agent, verification, report |
| [Calibration and labels](calibration-and-labels.md) | Mine git and Azure DevOps data, and see how gold labels and the `model-*` files are made |
| [Scoring](scoring.md) | Understand the phase-0 `score-pr` rule and its output |
| [Models and evaluation](models-and-evaluation.md) | Build the commit dataset, train and gate models, grade them online, run the D6 backtest |
| [Demos](demos.md) | Use `demo-setup.sh`, `demo.sh`, `demo-serve.sh` and `demo-clean.sh` |
| [Adding a repo adapter](adding-a-repo-adapter.md) | Bring a third repository in |
| [Development](development.md) | Run the tests and linters, and follow the code conventions |

## Two halves, one CLI

Both halves run through `python3 run_scout.py`.

- **The review path** (`brief`, `review`) builds a deterministic static brief of a change, lets a
  local model answer the brief's bounded questions, checks the answers in code, and writes a report
  and a PR comment. Its committed detector, D6, finds platforms the change reaches that PR CI never
  builds. See [scout-hld.md](scout-hld.md).
- **Break-risk scoring** (`mine-*`, `score-pr`, `dataset`, `ml`) learns from upstream history which
  changes break which CI jobs. Labels are the results of named gold jobs in Azure DevOps, never the
  overall pipeline result. See [calibration-and-labels.md](calibration-and-labels.md) and
  [scoring.md](scoring.md).

## Ground rules

- Scout reads upstream `sonic-net` repositories on `master` and is advisory only.
- Author and vendor identity are never model features. Reverts are a weak proxy for breakage and
  are never the target of a shipped scorer.
- Splits are chronological, and nothing is trained on test PRs or test commits.
- `score-pr` is a pure function of the changed paths and `scoring-plan.json`; it makes no network
  call.
