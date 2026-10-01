# Phase-0 scoring: `score-pr`

`score-pr` estimates, for each gold job of a repository, the probability that a change breaks it.
It is a rule, not a trained model, and it is the baseline every model in
[models-and-evaluation.md](models-and-evaluation.md) must beat. Its inputs come from the
calibration cache described in [calibration-and-labels.md](calibration-and-labels.md).

## Contract

`score-pr` is a pure function of two inputs:

1. the changed paths, from `git diff --name-only <base>...HEAD` in the `--repo-root` checkout;
2. `scoring-plan.json` from the calibration directory, plus the adapter's own tables.

It never queries Azure DevOps or GitHub. The same paths and the same plan always give the same
output, so a score can be replayed offline and the same record can feed a log, a comment or the
online ledger. It is advisory and never blocks a merge.

```bash
python3 run_scout.py --repo-root ~/data/git/sonic-mgmt --repo-type sonic-mgmt \
  score-pr --base origin/master
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--base` | `origin/master` | Base ref of the three-dot diff |
| `--brief PATH` | none | Score the change a `scout-brief.json` describes instead of `BASE...HEAD` |
| `--calibration DIR` | `<cache>/eval/calibration/<adapter>` | Directory holding `scoring-plan.json` |

The JSON goes to stdout even under `-q`.

## Output

```json
{
  "base_ref": "origin/master",
  "score_by_job": {"vs": 0.149561, "broadcom": 0.128195, "...": 0.0},
  "job_base_source": "scoring-plan",
  "files": ["rules/sonic-utilities.mk", "device/dell/x/platform.json"],
  "reasons": [
    {"path": "rules/sonic-utilities.mk", "bucket": "build_system",
     "path_class": "sonic-buildimage:build_rule", "prior": 0.025, "jobs": ["vs", "vpp", "..."]},
    {"path": "device/dell/x/platform.json", "bucket": "device_sku",
     "path_class": "sonic-buildimage:platform_data", "prior": 0.008, "jobs": ["broadcom"]}
  ],
  "source": "scoring-plan"
}
```

The numbers above are illustrative; they depend on the plan in the cache.

| Field | Meaning |
| --- | --- |
| `base_ref` | The base the diff was taken against |
| `score_by_job` | One probability per gold job, at most 0.35 |
| `job_base_source` | `scoring-plan` when the plan carries per-job base rates, else `revert_base_rate` |
| `files` | The changed paths scored |
| `reasons` | Per path: its bucket, the adapter's path class, the revert prior used, and the gold jobs its blast radius reaches |
| `source` | `scoring-plan` when the plan was found, else `adapter_defaults` |

With `--brief`, `source` is `scout-brief-view`, and the output adds `brief_path` and
`files_complete`. The brief's `base_sha...head_sha` is diffed in the checkout; if that diff fails
(for example, the SHAs are not in the clone), the brief's hotspot paths are scored instead and
`files_complete` is `false`. Hotspots are capped, so such a score can miss files.

## The rule

The scorer is `heuristic_p` in `scout_impl/eval/calibration_rules.py`. `mine-labels` uses the same
function to fill `heuristic_p_job` in `model-dataset-pr.json` and `heuristic_p` in
`model-dataset-pr-job.jsonl`, so the baseline and the shipped score cannot drift apart.

For one gold job `j`:

1. Start from `job_base[j]`, the job's failure rate on train-split PRs, from `scoring-plan.json`.
   Without a plan it falls back to the overall revert rate, which itself defaults to 0.012.
2. For each changed path whose blast radius reaches `j`, take its revert prior: the path's own
   P(revert) from `file_priors` when it was touched at least 50 times, else its bucket's prior from
   the adapter table, else the overall revert rate `base_rate_revert`.
3. The path's lift is `prior / base_rate_revert`. Paths in a shared-boost bucket that reach a
   shared-boost job get their lift multiplied by 1.15. On `sonic-mgmt`, a path containing
   `tests_mark_conditions` has its prior capped at 1.3 times the revert rate first, because those
   skip-condition YAML files are the highest-churn files in the tree.
4. The score is `min(job_base[j] * max_lift, 0.35)`, where `max_lift` is at least 1. Paths never lower
   the score, and a change with no files scores `min(job_base[j], 0.35)`.

Job failure rates and revert priors are on different scales, which is why a path contributes a
ratio rather than its raw prior. An earlier rule took `max(job_base, prior)`; once any-attempt labels
raised every job's base rate above every revert prior, it returned the base rate for every PR.

### Blast radius

`jobs_for_path` decides which gold jobs a path can affect, in this order:

1. A bucket in `all_gold_buckets` reaches every gold job.
2. Under `platform_prefix` or `device_prefix`, a vendor directory found in `platform_to_jobs` or
   `device_to_jobs` (or named like a gold job) reaches that vendor's jobs only. Vendor paths are
   SKU-local, so this outranks the bucket table.
3. A bucket listed in `bucket_jobs` reaches the listed jobs (`pretest` and `gold` expand to their
   groups). An empty list reaches nothing.
4. Under `tests_prefix` (`sonic-mgmt`), the feature directory is looked up in `feature_to_jobs` and
   reaches Pre_test plus the mapped jobs; an unknown feature reaches Pre_test plus every gold job.
5. Anything else reaches the Pre_test jobs, or nothing on a repository without them.

| Repository | Reaches every gold job | Notes |
| --- | --- | --- |
| `sonic-buildimage` | buckets `build_system`, `docker`, `slave`, `image_rootfs`, `ci` | `platform/<vendor>/` and `device/<vendor>/` reach only that vendor's image jobs through `platform_to_jobs` and `device_to_jobs` (`platform/vs/` reaches `vs`, `vpp`, `alpinevs`; `device/arista/` reaches `broadcom`). A vendor in neither map, `submodule`, `github` and `other` reach none. |
| `sonic-mgmt` | buckets `shared_pytest`, `ci`, `testbed_ansible`, `ansible_lib`, `ansible_other` | `tests/<feature>/` goes through `feature_to_jobs`. `spytest` and `sdn_tests` reach Pre_test only. |

The tables are in `scout_impl/repos/sonic_buildimage_calibration.py` and
`scout_impl/repos/sonic_mgmt_calibration.py`. Changing them changes the fingerprint of the
`join-labels` step, so the next `mine-labels` recomputes the baseline. Changing the rule's code bumps
`JOIN_VERSION` instead (version 4 moved the vendor maps ahead of the bucket table).

## Measured baseline

These figures are copied from the project's recorded runs; regenerate them before relying on them.

- `sonic-buildimage`, rejoin of 30 Sep 2026 at `JOIN_VERSION` 3: 269 PRs, 2,307 (PR, job) rows,
  157 failing rows (6.8%) from 26 failing PRs. The rule scored PR-AUC 0.116 and ROC-AUC 0.67 on test
  rows, and 0.051 and 0.47 on valid rows.
- `sonic-mgmt`, lift rule at `JOIN_VERSION` 3 on the join of 29 Sep 2026 (tip `01296ee`, one build
  per PR): PR-AUC 0.065 and ROC-AUC 0.79 on test rows (11 failures), 0.038 and 0.69 on valid rows
  (8 failures).

On `sonic-buildimage`, a PR that breaks the build usually fails eight or nine image jobs at once, so
row-level metrics rest on very few PRs. Count failing PRs before trusting any figure.

## Using it on a pull request

The scorer only needs a checkout with the PR head and the base:

```bash
git -C ~/data/git/sonic-buildimage fetch origin master
git -C ~/data/git/sonic-buildimage fetch origin pull/12345/head
git -C ~/data/git/sonic-buildimage checkout --detach FETCH_HEAD
python3 run_scout.py --repo-root ~/data/git/sonic-buildimage --repo-type sonic-buildimage \
  score-pr --base origin/master > score.json
```

`ml watch` applies the same rule to every open PR of a remote, taking each PR's file list from the
GitHub API instead of a local diff, and appends the scores to the online ledger; see
[models-and-evaluation.md](models-and-evaluation.md#online-evaluation).
