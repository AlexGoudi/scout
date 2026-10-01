# Calibration data and labels

This document covers how Scout mines upstream history into the calibration cache, how it labels
pull requests from Azure DevOps job results, and the `model-*` files a trainer or scorer reads. The
design rationale is in [scout-hld.md](scout-hld.md) section 7; flags are in
[cli-reference.md](cli-reference.md).

## The mining steps

Run them in this order, each with `--repo-root` pointing at a clone of the upstream repository
(the clone must have `origin` pointing at `github.com/sonic-net/<repo>`):

| Step | Command | Needs | Writes |
| --- | --- | --- | --- |
| 1 | `mine-incidents` | clone | `incidents.jsonl` in the calibration directory, plus a corpus JSONL at `--output` (default `scout-corpus.jsonl` in the working directory) |
| 2 | `mine-git-dumps` | clone | `git-overview.json`, `git-commit-file-changes.json`, `git-file-revert-rates.json` (also refreshes `incidents.jsonl`) |
| 3 | `mine-azure` | network, `--repo-type` | `azure-definitions.json`, `azure-pr-pipeline-builds.json`, `azure-pr-job-timelines.json` |
| 4 | `mine-labels` | clone, steps 2 and 3 | `azure-pr-file-changes.json`, every `model-*` file, `scoring-plan.json`, and `repo-pytest-markers.json` (`sonic-mgmt`) or `repo-docker-from.json` (`sonic-buildimage`) |
| 5 | `check-fidelity` | step 3 | nothing; prints a JSON comparison and exits 1 on a mismatch |

Example for `sonic-buildimage`:

```bash
CLONE=~/data/git/sonic-buildimage
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-buildimage mine-incidents
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-buildimage mine-git-dumps --revision origin/master
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-buildimage mine-azure
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-buildimage mine-labels
python3 run_scout.py --repo-root "$CLONE" --repo-type sonic-buildimage check-fidelity
```

`mine-labels` writes to the clone: for PRs that are not merged squash commits on the ref, it runs
`git fetch origin pull/<N>/head` (and `pull/<N>/merge` when needed) into `refs/tmp/pr-<N>` refs.
It does not touch the working tree or any branch.

## Cache layout

Everything lives under `<cache>/eval/calibration/<adapter>/`, where `<cache>` is `--cache-dir` or
its default (`$SCOUT_CACHE_DIR`, else `$XDG_CACHE_HOME/sonic-scout/repos`, else
`~/.cache/sonic-scout/repos`). The directory name carries no SHA; tips are tracked in the manifest.
`--output` on `mine-git-dumps`, `mine-azure`, `mine-labels` and `check-fidelity` overrides the
directory. `mine-labels` expects the `git-*` and `azure-*` files in the same directory it writes to.

### Manifest, resume and `--refresh`

`manifest.json` (`scout_impl/eval/_cache.py`, `schema_version` 1) records one entry per step:
`{"fingerprint", "cursor", "files"}`. A step whose fingerprint matches is skipped.

| Step | Name in manifest | Fingerprint covers | Resume behaviour |
| --- | --- | --- | --- |
| `mine-incidents` | `incidents` | `CODE_VERSION`, the `--grep` pattern | Appends reverts since the cached tip; re-walks if the old tip is not an ancestor. `--limit` always re-mines. |
| `mine-git-dumps` | `git-dumps` | `CODE_VERSION`, adapter, ref, the bucket tables | Walks only `old_tip..new_tip`; re-walks on a non-ancestor tip. |
| `mine-azure` | none | not fingerprinted | Reuses the cached definition id and builds, fetches builds newer than the newest cached one (with one day of overlap) and older ones up to the cap, then fetches only timelines not yet cached. |
| `mine-labels` | `join-labels` | `CODE_VERSION`, `JOIN_VERSION`, adapter, git tip, the set of Azure build ids, the full calibration table | Skips when fresh and `model-index.json` and `model-dataset-pr.json` exist. |

`--refresh` on any of them ignores the cache and redoes the step from scratch.

## Raw dumps

| File | Contents | Train on it? |
| --- | --- | --- |
| `git-overview.json` | Ref, tip SHA, commit count, date range, revert count and rate | no |
| `git-commit-file-changes.json` | Non-merge commits whose subject ends in `(#N)`: SHA, time, subject, PR number, `is_revert`, and `files` as `{path, additions, deletions, binary}` | no, it is unlabelled |
| `git-file-revert-rates.json` | `base_rate` (reverts over commits) and, for paths touched at least 50 times, `p_revert`, `lift_vs_base` and bucket | no, it feeds priors |
| `incidents.jsonl` | Mined `^Revert` commits and their links | no |
| `azure-definitions.json` | Pipeline definition lookup | no |
| `azure-pr-pipeline-builds.json` | Slim records of completed PR builds, newest first | no |
| `azure-pr-job-timelines.json` | Per sampled build: `pr`, `result`, `pipelineResult`, `groupJobs` (gold and excluded jobs by group), and every job's name and result | no, it includes excluded jobs and the parent result |
| `azure-pr-file-changes.json` | Per PR: labels, diff source and numstat files, two-dot files included | no, `model-*` already did the join |

File-change records are paths plus numstat only, never hunks. `binary` is true when git reports
`-` for both counts.

## Azure DevOps

| | `sonic-buildimage` | `sonic-mgmt` |
| --- | --- | --- |
| Pipeline | `Azure.sonic-buildimage` | `Azure.sonic-mgmt` |
| Definition id | 1 | 39 |
| Organization URL | `https://dev.azure.com/mssonic/build/_apis` | same |

Requests use `api-version=7.1` and need no credential. The sampling caps are on `AzureDevOpsSpec`
(`scout_impl/repos/base.py`), filled from each adapter's `*_calibration.py` table:

- `pr_builds = 3000`: the newest completed builds with reason `pullRequest`, newest first.
- `pr_timelines = 800`: at most this many distinct PRs get timelines.
- `builds_per_pr = 3`: at most this many attempts per PR, reruns included.

Timeline fetches run in parallel: `--workers`, else `$SCOUT_AZURE_WORKERS`, else 6, clamped to 1 to
32. A timeline that cannot be fetched is logged as a warning and left out of the sample. Azure keeps
PR runs for a limited time (about ten days, as observed by the maintainers), so run `mine-azure` at
least weekly to keep growing the sample.

## Gold labels

A gold job is a job whose result is the label. Each adapter names its own, in the `jobs` block of
its calibration table (`scout_impl/repos/*_calibration.py`). Azure job names are mapped to job keys
by the `groups` table, and only mapped jobs are kept in `groupJobs`.

**`sonic-buildimage`, image assembly.** Gold jobs: `vs`, `vpp`, `alpinevs`, `broadcom`, `mellanox`,
`marvell_prestera_arm64`, `marvell_prestera_armhf`, `nvidia_bluefield`, `aspeed_arm64` (group
`image`). KVM Test is not a gold job.

**`sonic-mgmt`, required tests.**

- Pre_test (group `pretest`): `static_analysis`, `validate_test_cases`, `dependency_check`,
  `markers_check`, `meta_check`, mapped from `Static Analysis`, `Validate Test Cases`,
  `Dependency Check`, `Markers Check` and `Meta check`.
- Classic KVM Elastictest (group `elastictest`): `t0`, `t0_2vlans`, `t1_lag`, `dualtor`, `t0_sonic`,
  `dpu`, `t1_multi_asic`, `t2`, mapped from Azure job names of the form
  `impacted-area-kvmtest-<topology> by Elastictest` (for example `t1_multi_asic` is
  `impacted-area-kvmtest-multi-asic-t1 by Elastictest`).
- Excluded, dumped for contrast only: `t0_vpp` and `t1_lag_vpp`. They appear in `vpp_labels` and
  `excluded_labels` on PR records and in `model-azure-path-job-fail.json`, and never in `y`.

**Never a label:** the parent pipeline result (`pipelineResult`), on either repository. It is red far
more often than the gold jobs, because of VPP Elastictest on `sonic-mgmt` and KVM Test on
`sonic-buildimage`. Also never labels: reverts, author or vendor identity, GitHub Actions Semgrep,
DCO or CodeQL results, and the automerge label.

### The any-attempt rule

A PR's attempts are ordered oldest first and combined per job by `aggregate_attempt_labels`
(`scout_impl/eval/calibration_rules.py`):

- `failed` if any attempt failed;
- otherwise `succeeded` if any attempt succeeded;
- otherwise the latest non-null result (for example `canceled`).

The final attempt alone hides failures fixed before merge, so it is kept separately:

| Field | Where | Meaning |
| --- | --- | --- |
| `labels` | PR records | Any-attempt result per gold job, `null` when the job did not run |
| `labels_latest` | PR records | Result of the latest attempt only |
| `n_attempts` | PR records and job rows | Attempts fetched for the PR |
| `y` | job rows | 1 if the job failed on any fetched attempt, else 0 |
| `y_latest` | job rows | 1 if it failed on the latest attempt |
| `fail_attempts` | job rows | Number of attempts on which the job failed |

A job row exists only when the any-attempt result is `succeeded` or `failed`. Missing slots (for
example `sonic-mgmt` impacted-area skips) and canceled jobs are counted in
`class_balance.canceled_or_missing_job_slots` and dropped.

## Changed paths per PR

For each labelled PR, the join needs the files the PR changed:

1. If the PR landed on the ref as a squash commit whose subject ends in `(#N)`, its numstat from
   `git-commit-file-changes.json` is used (`diff_source` `origin/master_squash_numstat`).
2. Otherwise the clone fetches `refs/pull/N/head` and diffs it against its merge base with the ref
   (`fetch_pull_head_merge_base`).
3. Failing that, the first parent of `refs/pull/N/merge` against the merge ref
   (`fetch_pull_head_merge_first_parent`).
4. Failing that, it deepens the head fetch by 200 commits and retries the merge base
   (`fetch_pull_head_merge_base_deepened`).
5. Failing that, a three-dot diff (`fetch_pull_head_three_dot`), then a two-dot diff
   (`fetch_pull_head_two_dot`).

A PR whose diff comes from a two-dot fallback, or whose fetch or diff failed, keeps its labels but
has `path_bag_usable: false`: its `paths` and `files` in `model-*` are empty and its features are
computed from no files. Two-dot file lists stay only in `azure-pr-file-changes.json`. Never copy
them back into a training set.

## The `model-*` contract

Read `model-schema.json` and `model-index.json` first; they describe the rest and name the files not
to train on.

| File | Role |
| --- | --- |
| `model-schema.json` | Task, target definitions (`y`, `y_latest`, `heuristic_job_base`), what to drop, the `do_not_use` list, feature notes, class balance, leakage rules and `how_to_feed` recipes |
| `model-index.json` | What to train on, lookups, `do_not_train_on_directly`, class balance and `still_missing` |
| `model-dataset-pr.json` | One example per PR: `pr`, `sha`, `azureBuildId`, `finishTime`, `diff_source`, `path_bag_usable`, `features`, `heuristic_p_job`, `labels`, `labels_latest`, `n_attempts`, `excluded_labels`, `split`, `paths`, `files` |
| `model-dataset-pr-job.jsonl` | One row per (PR, gold job) with a gold result: `pr`, `sha`, `split`, `job`, `y`, `y_latest`, `n_attempts`, `fail_attempts`, `heuristic_p`, `features`, `path_bag_usable` |
| `model-splits.json` | `train_prs`, `valid_prs`, `test_prs` and counts |
| `model-path-priors.json` | `base_rate_revert`, `by_path_n_ge_50` (P(revert given path) for paths touched at least 50 times), `bucket_priors` |
| `model-azure-path-job-fail.json` | Small-sample P(job fails given path) over train-split PRs, minimum 5 PRs per pair, excluded jobs included for contrast |
| `scoring-plan.json` | The phase-0 scorer's inputs: `path_weights.file_priors`, `base_rate_revert`, `bucket_priors`, `job_base` per gold job, the prefix maps and the `score-pr` contract |

`features` holds `n_files`, `additions`, `deletions`, `churn`, `binary_files`, `log1p_churn`,
`log1p_files`, `n_shared_paths`, `frac_shared`, `max_revert_prior`, `mean_revert_prior`, bucket and
extension counts, the adapter's `feature_prefix_counts` and `feature_path_flags`, `submodule_only`,
`device_only`, `platforms_touched` and, for `sonic-mgmt`, `spytest_only` and `features_touched`. Records also carry
`pipelineResult` for reference; it is never a feature or a label.

### Splits and base rates

PRs are sorted by the `finishTime` of their latest attempt and split 70/15/15 into `train`, `valid`
and `test`. All jobs of a PR share its split. `job_base` (each gold job's failure rate) and
`model-azure-path-job-fail.json` use train-split PRs only. Never train on `test_prs`, and never use
later nightly builds as PR features.

## Fidelity check

`check-fidelity` loads `azure-pr-job-timelines.json`, collects the job keys present in `groupJobs`,
and compares them with the adapter's gold job list, ignoring excluded jobs. For `sonic-mgmt` it
also compares the KVM subset (gold minus Pre_test). It prints
`{"ok", "only_in_coverage_model", "only_in_azure_timelines", ...}` and exits 0 when `ok` is true, 1
otherwise. `ml train-pr-job` refuses to train when this check fails.

## Versions and what to rerun

| Change | Bump | Invalidates | Rerun |
| --- | --- | --- | --- |
| Label or join logic in `scout_impl/eval/join_labels.py` or `calibration_rules.py` | `JOIN_VERSION` in `join_labels.py` (currently `"3"`) | the `join-labels` step | `mine-labels`, then `ml train-pr-job`, `ml grade` |
| Calibration tables in `repos/*_calibration.py` | none needed | `join-labels` (table is fingerprinted); `git-dumps` if buckets change | `mine-git-dumps` and `mine-labels`, then models |
| Commit labels in `scout_impl/dataset/labels.py` | `LABELS_VERSION` (currently `"2"`) | `dataset build` | `dataset build`, then `ml train-risk`, `ml walk-forward` |
| Record extraction in `scout_impl/mining/` | `EXTRACTOR_VERSION` in `mining/__init__.py` | record cache and `dataset build` | `dataset build` and models |
| Cache mechanics in `scout_impl/eval/_cache.py` | `CODE_VERSION` | every calibration step | all mining steps |
| Taxonomy YAML | none needed | `dataset build` (taxonomy SHA is fingerprinted) | `dataset build` and models |

Timelines are not fingerprinted. To pick up rerun attempts that an older `mine-azure` did not
collect, rerun `mine-azure` (with `--refresh` if the cached sample predates the per-PR attempt cap),
then `mine-labels`.
