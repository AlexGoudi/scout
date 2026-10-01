# Adding a repository adapter

Everything Scout knows about one repository lives in its adapter under `scout_impl/repos/`. The
core reads only the adapter's data, so a new repository is mostly new data. This page lists what
an adapter must provide and the few places outside `repos/` that still name the two existing
adapters. The design is in [scout-hld.md](scout-hld.md) section 5.1.

## 1. Clone upstream

Clone the upstream repository with full history next to the others, for example
`~/data/git/<repo>`. Scout reads clones and never pushes to them; `mine-labels` adds
`refs/tmp/pr-*` refs when it fetches PR heads.

## 2. Write the adapter module

Create `scout_impl/repos/<repo_module>.py` defining one `RepoAdapter` (`scout_impl/repos/base.py`)
and register it at the bottom of `scout_impl/repos/__init__.py` with
`register_adapter(<module>.ADAPTER)`. `sonic_mgmt.py` is the smaller example.

| Field | Purpose |
| --- | --- |
| `name` | The `--repo-type` value and the cache directory name. |
| `summary` | One line for humans. |
| `markers` | Tuples of paths; the tree is this repository if every path of any one tuple exists. Two adapters matching the same tree is an error, so markers must be distinctive. |
| `path_classes`, `path_rules`, `fallback` | Ranked path classes (lower rank is wider blast radius) and the `fnmatch` rules that assign them; the first matching rule wins and unmatched paths get `fallback`. |
| `entity_sources` | Named glob families (topologies, platforms, ...) the toolbox and brief can enumerate. |
| `invariants`, `ci_surfaces` | Optional checklists and CI configuration files to surface in the brief. |
| `entity_model` | `DirectoryEntitySpec` (an entity is a directory holding a declaration file, as `sonic-buildimage` platforms) or `FileEntitySpec` (an entity is a file, as `sonic-mgmt` topologies). |
| `coverage_spec` | `PipelineCoverageSpec` (coverage is the job groups of named pipeline stages) or `ConstantCoverageSpec` (coverage is a list literal in a Python file). |
| `github_api`, `azure_devops`, `calibration` | Built from the calibration table (step 3). |
| `rules` | `RuleSpec` entries the brief states and the agent's rule-consistency check enforces. |
| `detectors` | Detector ids that apply; leave empty until a detector is designed and measured for this repository. |

If the new repository declares entities or coverage in a shape neither spec covers, that is a core
change: add the spec to `base.py` and its reader to `scout_impl/static/`, as the second adapter did
for `FileEntitySpec` and `ConstantCoverageSpec`.

## 3. Write the calibration table

Create `scout_impl/repos/<repo_module>_calibration.py` defining `JOIN_TABLES`, a dictionary with
the keys the existing tables use. Do not copy another repository's job map: define gold jobs from
this repository's own pipeline timelines.

| Key | Contents |
| --- | --- |
| `name` | Adapter name. |
| `github` | `owner`, `repo`, `ref` (upstream default branch, for example `origin/master`), `user_agent`. |
| `azure` | `org_url`, `pipeline_name`, `definition_id`, `yaml_name_filter`, `name_search`, `pr_builds`, `pr_timelines`, `builds_per_pr`, `official_definitions`. |
| `jobs` | `gold` (job keys that are labels), `pretest` (a subset reached by almost every path, or empty), `excluded` (dumped for contrast, never labels), `groups` (Azure job name to job key, per group), `legacy_timeline_fields`. |
| `paths` | `buckets` (ordered rules with `exact`, `prefixes`, `contains` or `regex`), `default_bucket`, `high_risk`, `submodule_only`. |
| `blast` | `all_gold_buckets`, `bucket_jobs`, and optionally `tests_prefix`/`feature_segment`/`feature_to_jobs`/`unknown_feature_jobs`, `platform_prefix`/`platform_to_jobs`, `device_prefix`/`device_to_jobs`. See [scoring.md](scoring.md#blast-radius). |
| `priors` | Revert prior per bucket, used when a path has fewer than 50 touches. |
| `heuristic` | `cap`, `skip_mark_substr`, `skip_mark_cap_mult`, `shared_boost_buckets`, `shared_boost_jobs`, `shared_boost`. |
| `feature_path_flags`, `feature_prefix_counts`, `shared_bucket_for_frac` | Extra PR features. |
| `scan` | `pytest_markers` and `docker_from`: which repository scans `mine-labels` runs. |

Choosing gold jobs is the important decision. A gold job must be one whose failure the change
under review plausibly caused. Never use the parent pipeline result, and never a job family that
fails often for reasons unrelated to the change; on `sonic-mgmt` the VPP Elastictest jobs are
excluded for that reason, and on `sonic-buildimage` KVM Test is not a gold job. Inspect a sample
of `azure-pr-job-timelines.json` first: run `mine-azure` with a provisional table, look at the job
names and results, then fix `jobs.groups`.

## 4. Add a commit-dataset taxonomy (optional)

For `dataset build`, add `scout_impl/repos/<dir>/taxonomy.yaml` with the keys of the existing ones:
`schema_version`, `szz_file_classes` (optional; defaults to `code`, `config`, `build`, `yang`,
`patch`), `bots`, `name_stoplist`, `features`, `components`, `entities` and `file_classes`. Decide
which file classes SZZ should blame: on `sonic-mgmt` the tests are the product, so `test` is
included.

## 5. Update the places that name adapters

These still list the two adapters explicitly:

- `scout_impl/mining_cli.py`: `TAXONOMY_BY_REPO`, and the `--repo-type` choices of `ml train-pr-job`,
  `ml walk-forward`, `ml watch` and `ml grade`.
- `scout_impl/cli.py`: `check-fidelity` has a `sonic-mgmt`-only topology comparison.
- `scout_impl/agent/prompts.py`: the system prompt describes the reviewer as reviewing
  `sonic-buildimage` changes.
- `scout_impl/eval/corpus.py` and `scout_impl/eval/backtest_run.py`: the seed corpus and backtest
  are `sonic-buildimage` only.

## 6. Mine, check and measure

```bash
CLONE=~/data/git/<repo>
python3 run_scout.py --repo-root "$CLONE" --repo-type <repo> mine-incidents
python3 run_scout.py --repo-root "$CLONE" --repo-type <repo> mine-git-dumps --revision origin/master
python3 run_scout.py --repo-root "$CLONE" --repo-type <repo> mine-azure
python3 run_scout.py --repo-root "$CLONE" --repo-type <repo> mine-labels
python3 run_scout.py --repo-root "$CLONE" --repo-type <repo> check-fidelity
python3 run_scout.py --repo-root "$CLONE" brief --rev HEAD --stdout | head -40
```

`check-fidelity` must print `"ok": true`. `brief --rev HEAD` without `--repo-type` checks that the
markers identify the tree. Then confirm that `mine-labels` produced the full contract:
`model-schema.json`, `model-index.json`, `model-splits.json`, `model-dataset-pr.json`,
`model-dataset-pr-job.jsonl`, `model-path-priors.json`, `model-azure-path-job-fail.json` and
`scoring-plan.json` (see [calibration-and-labels.md](calibration-and-labels.md#the-model--contract)).

Before trusting any metric, count the failing PRs, not just the failing rows, per split.

## 7. Tests

- Add a conformance test beside `tests/conformance/test_second_repository.py`, which records what
  the second adapter forced into the core and asserts what it did not.
- Pin a tree fixture with `tests/fixtures/capture_tree.py --remote <owner/repo> --rev <sha> --out
  tests/fixtures/trees/<name>.json` and commit it, so brief and coverage tests run offline.
- Add unit tests for any new calibration rule. See [development.md](development.md).
