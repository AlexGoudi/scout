# Models and evaluation

Scout has two kinds of model, trained on two different datasets, and one online ledger that
grades them on changes they never saw:

| Model | Dataset | Unit | Label | Command |
| --- | --- | --- | --- | --- |
| Commit risk | commit dataset (`dataset build`) | landed commit | `bug_introducing` (SZZ) or `reverted_within_90d` | `ml train-risk` |
| PR-job failure | calibration join (`mine-labels`) | (PR, gold job) | gold Azure job failed on any attempt | `ml train-pr-job` |

The phase-0 rule in [scoring.md](scoring.md) is the baseline for the second; a model that does not
beat it is not used. Every flag is listed in [cli-reference.md](cli-reference.md).

Two rules apply to every model:

- **Author and vendor identity are never features.** The commit dataset keeps a salted author
  hash and `author_*` and `file_prior_authors` history columns for analysis. `ml/matrix.py` drops
  every column starting with `author_`, `committer_` or `file_prior_authors` before a model sees it
  (`IDENTITY_PREFIXES`).
- **Reverts are a weak proxy.** `reverted_within_90d` exists to measure how far reverts track real
  breakage; it must not become the target of a shipped scorer.

## The commit dataset

```bash
python3 run_scout.py dataset build --repo ~/data/git/sonic-mgmt --rev origin/master \
  --repo-type sonic-mgmt
```

`dataset build` walks the first-parent history of `--rev` in a full (not shallow) clone and writes
`corpus/<repo-type>-<sha9>/` unless `--out` is given. Pass `--repo-type`: without it the
`sonic-buildimage` taxonomy and directory name are used whatever the clone is.

| Output | Contents |
| --- | --- |
| `records/` | One commit record per commit (schema `schemas/scout-commit-1.1.json`), with hunks and a capped patch |
| `llm/` | Label-free text documents per commit |
| `annotations/` | Labels as of the snapshot |
| `features.parquet` | One row per commit: intrinsic features, history features, split, and labels as known at each split's end |
| `dataset_card.json`, `DATASET.md` | Counts, label definitions and rates, split boundaries, filters, provenance |
| `.scout-dataset-fingerprint` | Lets a repeat build skip work; `--refresh` ignores it |

Commit records are cached under `--cache` (default `.scout-cache/`). `--workers 0` uses every CPU,
`--no-szz` skips blame (and leaves `bug_introducing` null), and `--exclude FILE` sends listed SHAs
to a `holdout` split that no model ever sees.

### Labels

Labels depend on commits that land later, so they are never features
(`scout_impl/dataset/labels.py`, `LABELS_VERSION` `"2"`):

- `reverted`: a later first-parent commit reverts this one, linked by its `This reverts commit`
  trailer or, failing that, by the PR number. `reverted_within_7d`, `_30d` and `_90d` add a window.
- `bug_introducing`: SZZ. For each fix-like commit, `git blame -w -M` at the fix's first parent
  attributes the lines the fix removed or changed to earlier commits. Only SZZ-eligible file
  classes count: `code`, `config`, `build`, `yang` and `patch`, plus `test` on `sonic-mgmt` (its tests
  are the product; set by `szz_file_classes` in `scout_impl/repos/mgmt/taxonomy.yaml`). A fix
  deleting more than 1,000 lines is skipped as a likely rewrite. A commit touching no eligible file
  gets `null`, not `false`.
- Merges get `null` labels.

### Splits

Commits are split by landing order, 70% `train`, 15% `validation`, 15% `test`
(`scout_impl/dataset/split.py`). A commit landing within 90 days before a boundary, or before the
snapshot, goes to `gap`, because its label window would straddle the boundary. `features.parquet`
holds each split's labels as known when that split ends, so a model trained at that moment could
have known them. Merge, `gap` and `holdout` rows never reach a model.

## `ml train-risk`

```bash
python3 run_scout.py ml train-risk --dataset corpus/sonic-mgmt-<sha9> --label bug_introducing \
  --out models/sonic-mgmt
```

Four models are fitted on `train`: the train prevalence, a size-only logistic regression on churn,
a standardized logistic regression over every allowed feature, and a histogram gradient-boosting
classifier. The logistic and boosting hyperparameters come from small fixed grids chosen on
validation PR-AUC. Probabilities are Platt-calibrated on validation. Of the two learned models, the
one with the higher validation PR-AUC is selected.

Each model is reported on validation and test with PR-AUC, ROC-AUC, recall at 20% of changed lines
inspected, and Brier score, each with a 1,000-sample bootstrap 95% interval. The same `--seed`
gives the same metrics. Permutation importance on validation is reported for the two learned
models.

Outputs go directly into `--out`:

- `risk-<label>.joblib`: the fitted models, calibrators, column spec and reference scores;
- `risk-<label>.model_card.json`: rows, features, grid, selection, calibration, metrics;
- `risk-<label>.report.md`: the same, readable.

A rerun with the same dataset card, label and seed reuses the cached model unless `--refresh` is
given. `--final` refits the selected model on train, validation and test together and writes
`risk-<label>-final.*`. The selected model's calibrator is refitted too, on the same rows: each row's
score comes from a copy of the model trained without that row's fold (five contiguous folds in
landing order), so the calibration is not fitted to scores the model has already seen. The card
records this under `final_calibration` and still reports the held-out metrics of the non-final fit.
`ml walk-forward` refuses a final model, because nothing it could score is out of sample.

### `ml similar` and `ml score`

`ml similar --commit SHA` lists earlier commits similar to one in the dataset, scored as
0.6 times TF-IDF cosine on text plus 0.4 times Jaccard over files, areas and entities. Only commits
that landed earlier are returned, and a neighbour's outcome is shown only if it was known when the
query commit landed. `ml similar --evaluate` reports whether positive commits have more positive
neighbours than negative ones do.

`ml score --dataset D --model M --repo CLONE --commit SHA` prints one JSON bundle for a commit: its
label-free document, the calibrated probability with its percentile and the base rate, the top
logistic-regression reasons, and `-k` similar earlier commits. A commit outside the dataset is mined
from the clone and scored as if it landed right after the snapshot.

## `ml train-pr-job`

```bash
python3 run_scout.py ml train-pr-job --repo-type sonic-buildimage \
  --repo-root ~/data/git/sonic-buildimage --out models/sonic-buildimage
```

It reads `model-dataset-pr-job.jsonl` from the calibration directory (`--calibration`, else the
default cache) and refuses to run unless:

- the fidelity check of [calibration-and-labels.md](calibration-and-labels.md#fidelity-check)
  passes on the cached timelines;
- `scoring-plan.json` exists;
- train, valid and test rows exist, and train and valid each have at least one failure.

Rows with `path_bag_usable: false` (two-dot diff fallbacks, whose path features are empty) are left
out of every split; the card counts them under `rows_excluded_two_dot`.

It fits one class-balanced, standardized logistic regression on the train split's numeric PR
features, the row's `heuristic_p` and a one-hot job indicator. It writes `pr-job-<repo-type>.json`
with validation and test PR-AUC for both the model and the heuristic. `selected` is
`logistic_pr_job` when the model beats the heuristic on **validation**, otherwise `heuristic_p_job`;
test PR-AUC is reported for information and never decides. The `pr-job-<repo-type>.joblib` is
written only when the model is kept.

`--final` makes the same decision, then refits a kept model on train, valid and test together and
writes `pr-job-<repo-type>-final.{json,joblib}` with `final_refit: true`. Its reported metrics
describe the train-only fit. `ml walk-forward` takes commit risk models only, so it refuses both PR-job
models and any `--final` model.

Read these metrics with the split sizes in mind. On `sonic-buildimage`, a breaking PR fails eight or
nine image jobs at once, so a test split with a few dozen failing rows may rest on three or four PRs.
In one recorded run, the model reported PR-AUC 0.49 against the heuristic's 0.12 on a test split
whose 29 failing rows came from six PRs, three of which held 26 of them. That is not
evidence of a real improvement.

## Online evaluation

All three commands share one ledger per adapter, `<cache>/eval/online/<adapter>/ledger.jsonl`,
where `<cache>` is `$SCOUT_CACHE_DIR` or its default; the ledger ignores `--cache-dir`. A row is keyed
by `(subject, model)` and appended once; only `ml grade` rewrites rows, to fill outcomes.

| Command | What it appends | Subject | Model id |
| --- | --- | --- | --- |
| `ml walk-forward --dataset D --model M --repo-type R` | Every validation and test commit, with its probability and the label it carries in the snapshot | `commit:<sha>` | `risk-<label>:<card hash>` |
| `ml watch --remote sonic-net/R --repo-type R` | Each open PR head not yet scored, with `score_by_job` from the phase-0 rule over its GitHub file list and `p` as the maximum job score; at most `--max-prs` (default 100) per run | `pr:<number>:<head sha>` | `phase0:<scoring-plan hash>` |
| `ml grade --repo-type R` | Nothing new; fills PR outcomes and writes `online-scorecard.json` next to the ledger | | |

`ml watch` calls the GitHub REST API; set `GITHUB_TOKEN` or `GH_TOKEN` to avoid the unauthenticated
rate limit. Run it before Azure has finished on a PR, so the score is a true prediction.

`ml grade` looks up each ungraded PR row in `model-dataset-pr.json` of the calibration join. If that
PR's example finished at or after the row's `scored_at`, the row gets `outcome.y` (1 if any gold job
failed), the per-job `labels` and the Azure build id. The scorecard reports, per model, rows, pending
rows, and ROC-AUC and PR-AUC over graded rows, plus a job-grain score for PR rows. Rerun
`mine-azure` and `mine-labels` before grading so that recent PRs have outcomes.

## The D6 backtest

The review path's committed detector has its own offline evaluation, described in
[scout-hld.md](scout-hld.md) section 6.3:

```bash
python3 run_scout.py backtest                # replay the pinned corpus from fixtures
python3 run_scout.py backtest --capture      # mine a corpus and capture missing fixtures
```

`--capture` fetches upstream history into `<cache>/eval/history/`, selects the corpus when none is
pinned yet, and captures a tree fixture for each item that lacks one (network); `--revision` pins
the history tip used for a new corpus. Without `--capture`, the backtest replays the fixtures
offline; an item without a fixture is counted under `missing_fixture` rather than graded. It runs on `sonic-buildimage` only. Results go
to `scorecard.json` in `<cache>/eval/backtest/sonic-buildimage/` (or `--corpus-dir`), with recall,
precision on the corpus and the control flag rate, each with a Wilson 95% interval.
