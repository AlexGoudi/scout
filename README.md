# SONiC Scout

Scout reads an incoming change set and predicts which in-tree topologies, platforms and
tests it puts at risk, citing file and line, before an image is built or loaded onto a
box. It is **advisory only and never blocks a merge** — see
[docs/scout-hld.md](docs/scout-hld.md) section 1.1.

**Scout analyzes a separate repository, named on every invocation** with `--repo-root` for
a checkout on disk or `--remote owner/repo` to fetch one anonymously with no clone. It is a
standalone directory: nothing here reads its own history, and it does not need to sit
inside the tree it analyzes. `sonic-buildimage` is the primary target and `sonic-mgmt` is a
thin second adapter kept to prove the core is repo-agnostic.

This directory holds stage 0 (ingest) and **stage 1, the deterministic static analysis
stage**, which calls no model and emits `scout-brief.json` — the versioned contract the
agent stage consumes. The agent loop, detectors' adjudication half, verifier and report
renderer are not here yet; the schedule is [docs/scout-plan.md](docs/scout-plan.md)
section 4.

## Requirements

Python 3 (developed and tested on 3.10) and a `git` binary on `PATH`. `requirements.txt`
lists the runtime dependencies and why each is there: **PyYAML** for the pipeline
definitions and the commit-mining path taxonomy, and **numpy, pandas, pyarrow and
scikit-learn** for the commit dataset's feature table and its risk and similarity
baselines. Running the tests additionally needs `pytest`, and `jsonschema` for the
record-schema checks:

```bash
python3 -m pip install -r requirements.txt pytest jsonschema
```

## Current entrypoint

`run_scout.py`, run from this directory, with `--repo-root` pointing at the `sonic-mgmt`
working copy to analyze. That checkout is only ever read.

```bash
SONIC_MGMT=/path/to/sonic-mgmt

# Resolve a commit range into a structured, cacheable change set
python3 run_scout.py --repo-root "$SONIC_MGMT" ingest --range origin/master..HEAD --output changeset.json

# Resolve a fork sync delta and flag the local patches the incoming commits overlap
python3 run_scout.py --repo-root "$SONIC_MGMT" ingest --sync HEAD origin/master --output sync.json

# Mine the revert history into the labelled seed corpus
python3 run_scout.py --repo-root "$SONIC_MGMT" mine-incidents --output scout-corpus.jsonl
```

Ranges and revisions are resolved inside that checkout, so `origin/master..HEAD` means its
`origin/master` and its `HEAD`, not anything in this directory.

The static stage adds `brief`, which needs no checkout at all:

```bash
# A pull request on the primary target, fetched anonymously
python3 run_scout.py --remote sonic-net/sonic-buildimage brief --pr 29742

# A ref range, and the whole-tree figures at one revision
python3 run_scout.py --remote sonic-net/sonic-buildimage brief --range BASE..HEAD
python3 run_scout.py --remote sonic-net/sonic-buildimage brief --rev master

# The same analysis offline, from a pinned fixture: no network, no clone
python3 run_scout.py brief --fixture tests/fixtures/trees/sonic-buildimage-master-62cfe50.json
```

## Reusable modules

- `scout_impl/models.py`: the serializable change-set model and the prefilter's path classes
- `scout_impl/diffparse.py`: unified-diff parser producing per-file, per-line structure
- `scout_impl/ingest.py`: stage 0 ingest, `resolve(spec, repo_root) -> ChangeSet` (FR-1)
- `scout_impl/incidents.py`: incident miner over `^Revert` history, for the seed corpus
- `scout_impl/provider.py`: `Provider` interface, `ReplayProvider`, `RecordingProvider`
- `scout_impl/gitcmd.py`: read-only git wrapper with pinned config overrides
- `scout_impl/repos/`: the adapter boundary — path classes, entity model, coverage spec, rule pack
- `scout_impl/static/`: stage 1, the deterministic analyzer (below)
- `scout_impl/detectors/`: the detector catalog; one detector is committed, D6
- `scout_impl/core/static_run.py`: orchestration, `run_static(...) -> StaticRun`

## Change-set ingest

`resolve` normalizes two of the three FR-1 inputs into a `ChangeSet`:

| Mode | Spec | Commits | Extra |
| --- | --- | --- | --- |
| `range` | `ChangeSetSpec.from_range("BASE..HEAD")` | `BASE..HEAD`, oldest first | — |
| `sync` | `ChangeSetSpec(base_ref=LOCAL, head_ref=UPSTREAM, mode="sync")` | `merge-base..UPSTREAM` | Local patch commits, annotated with the paths they share with the delta — the input to detector D3 |

`BASE...HEAD` (three dots) resolves the base to the merge base, matching `git diff`.
Merge commits are excluded by default because their first-parent diff duplicates the
commits already in the list; pass `include_merges=True` to keep them.

Every `DiffLine` carries `old_lineno` and `new_lineno`, so a finding's citation resolves
to a file and line at a named revision (FR-7). The whole object round-trips through
`to_dict` / `from_dict`, so a change set can be cached and replayed without git — which is
how the backtest harness gets its third, replay ingest mode.

Changed paths are classified for the prefilter (HLD section 4.3), highest blast radius
first: `ansible_code`, `test_common`, `ansible_data`, `pipeline`, `feature_test`, `other`,
`documentation`. The rules are an ordered table in `models.py`; add to the table rather
than to the logic.

## Static analysis stage

Stage 1 is a program with a written specification, and it is tested like one: no model, no
credential, no network beyond the fetch stage 0 already performed (HLD section 4.3).

| Module | Responsibility | Key interface |
| --- | --- | --- |
| `static/treeindex.py` | One tree listing plus a blob cache keyed on **blob sha**, so the 287 declarations cost 31 reads | `TreeIndex(source, rev)`; `.read(path)`, `.blob_reads` |
| `static/platforms.py` | The entity index and counting rules C1 to C4 | `build_entity_index(tree, spec) -> EntityIndex` |
| `static/pipeline.py` | The PR-CI coverage model, through template indirection, cross-checked | `parse_coverage_model(tree, spec) -> CoverageModel` |
| `static/constants.py` | The same model read out of a Python constant, for the second adapter | `parse_constant_model(tree, spec) -> CoverageModel` |
| `static/extract.py` | Dispatch from an adapter's declared spec to the extractor that reads it | `build_index`, `build_coverage` |
| `static/coverage.py` | Rule C5 and the coverage-gap query: covered, uncovered, **ambiguous** | `query_coverage(index, model, affected) -> CoverageResult` |
| `static/hotspots.py` | Ranking, with a breakdown whose components sum to the score | `rank_hotspots(files, index, coverage, classes) -> (Hotspot, ...)` |
| `static/brief.py` | The risk brief, validated on write plus the contracts a schema cannot express | `BriefBuilder.build(...) -> Brief` |
| `static/schema.py` | A stdlib JSON-schema validator that **rejects** keywords it cannot check | `validate_brief(payload)` |
| `static/engine.py` | Runs an adapter over a tree and serializes the result | `analyze(...) -> StaticResult`; `build_brief(...) -> Brief` |
| `static/fixtures.py` | A `RepoSource` served entirely from a pinned fixture. No git, no network | `TreeFixture.load(path).source()` |
| `detectors/coverage_gap.py` | D6: the rules, questions and unresolved items the brief carries | `synthesize(index, model, coverage, rules) -> Synthesis` |

The schema is `schemas/scout-brief-1.0.json` and it is validated on write. Beyond shape,
the builder checks three things a schema cannot state: that each hotspot's components sum
to its score, that covered, uncovered and ambiguous partition `affected` exactly, and that
nothing outside `entities` is named anywhere — the closed world that lets stage 2 drop a
finding about a platform the static stage never enumerated.

### The counting rules are the specification

Getting from a directory listing to a platform count takes five rules, they are normative
in [docs/scout-hld.md](docs/scout-hld.md) section 4.3.1, and each is asserted separately
by the conformance suite. They live in the adapter as **data**, not as code, so a reader
can check them against the document without reading an extractor.

Two of them fail in opposite directions and both have bitten. C3 excludes the three shared
`*_common` directories, which are library directories rather than hardware; counting them
inflates every figure in the flattering direction. C4 **keeps** platforms owning no HWSKU
directory, because the scan that finds the shared directories also finds
`device/arista/x86_64-arista_7800_sup`, a chassis supervisor — the plausible-looking rule
would discard 30 supervisors and fabric cards to exclude 3 shared directories, and would
do it silently. Both directions have their own fixture case.

### The pipeline parser is hardened first

It is the detector's source of ground truth and its failure mode is silent: a sloppy parse
changes every finding while the citations still resolve, the brief still validates and the
report still renders. So it resolves the pipeline's template indirection rather than
scanning for `- name:`, scopes to named stages and **fails loudly** on one it cannot find,
publishes its job-group list and the templates it read into the brief, and cross-checks
itself against an independent line-oriented scan that never loads YAML. Disagreement
raises. The fork fixture reproduces the 5-versus-8 disagreement that caught this during
measurement, and the conformance suite asserts that it still raises.

### It refuses to guess

PR CI builds `marvell-prestera-arm64` and `marvell-prestera-armhf`; 12 platforms declare
plain `marvell-prestera`, and 5 more declare `aspeed` against `aspeed-arm64`. Whether
those 17 are covered depends on an architecture their `platform_asic` file does not state.
The brief reports **both** answers — 88 never built under string equality, 71 under
architecture-aware matching — names the platforms, and puts the question in `unresolved`
for the agent to adjudicate with evidence.

## Model provider

`Provider.complete(messages, tools) -> Completion` is the only method call sites use.
Adding the live provider later means subclassing `Provider` and registering a factory:

```python
class HttpProvider(Provider):
    def _complete(self, messages, tools):
        ...  # one HTTP call, returns a Completion with its TokenUsage

register_provider("openai", lambda spec, **options: HttpProvider(spec, **options))
```

Nothing that calls `complete` changes, and no provider SDK becomes a dependency of this
package. `complete` is a template method that owns token accounting, so an implementation
cannot forget to report what it spent; `provider.usage`, `provider.call_count` and
`provider.usd` are what the budget governor and the report's cost block read.

Determinism (NFR-3) lives in `ModelSpec`: temperature defaults to 0 and `model_id` must
name a pinned version rather than a floating alias. Requests are content-addressed on
`(prompt_sha, model_id, input_hash)` per HLD section 6.5, with the system turns hashed
separately from the rest so a prompt change can be told apart from an input change when a
replay misses.

`ReplayProvider` serves recorded responses from a fixture directory and makes no network
calls, which is what makes the unit tests and the backtest hermetic and free (NFR-10). A
miss raises `ReplayMiss` naming the key it looked for. `RecordingProvider` wraps any
provider and writes each response into that same layout, so a live run can be captured
once and replayed forever after.

One fixture per request, named for its digest:

```text
tests/fixtures/replay/<digest>.json
  key      - digest, prompt_sha, input_hash, model_id
  model    - the ModelSpec the response was recorded against
  request  - the messages and tools, so the fixture is reviewable in a diff
  response - text, tool_calls, usage, finish_reason
```

## Incident miner

Git history is already a labelled dataset (HLD section 6.3). The miner finds the `^Revert`
commits, links those carrying a `This reverts commit <sha>` trailer back to the commit
they revert, and writes one JSONL record per incident.

Measured on the `sonic-mgmt` checkout this was developed against:

| | |
| --- | --- |
| `^Revert` commits | 190 |
| Carrying the trailer | 131 (127 linked, 4 naming a commit absent from that clone) |
| No trailer | 59 |
| Nested (`Revert "Revert ..."`) | 9 |
| Detection lead time, linked incidents | min 0, median 6, mean 28, max 468 days; 9 at or above 88 |

`detector_category` is emitted as `null` on every record. R2 assigns it in the joint
triage session on D2, per [docs/scout-plan.md](docs/scout-plan.md) section 8; nothing
in this module guesses it.

Two fields exist to keep the corpus honest rather than to pad it. `link_status` separates
`linked` from `unresolved` so the 4 records whose cause is not in the clone are not
silently treated as linked. `is_nested_revert` marks the 9 reverts of reverts, which are
un-reverts rather than incidents and would otherwise pollute a recall measurement.

Lead time is computed from **committer** dates, which are when the commits landed, not
author dates, which are when the patches were written. Both are recorded per side.

## Commit mining, dataset and risk baseline

These `run_scout.py` commands turn a `sonic-buildimage` clone into a commit dataset for
machine learning and for a language model, and train a first risk baseline on it. The clone
is only ever read: no checkout, no index refresh, nothing from it executed. Every git call is
a read (`cat-file`, `log`, `blame`, `rev-parse`, `rev-list`, and `config` with `--get` or
`--get-regexp`).

```bash
BUILDIMAGE=/path/to/sonic-buildimage   # a full clone; shallow clones are rejected

# One commit as a scout-commit/1.1 JSON record (schemas/scout-commit-1.1.json)
python3 run_scout.py mine commit --repo "$BUILDIMAGE" --commit d89a360e2

# The first-parent history of a pinned snapshot, into corpus/buildimage-<short sha>/
python3 run_scout.py dataset build --repo "$BUILDIMAGE" --rev d89a360e2

# Risk models on the dataset; the default label is bug_introducing
python3 run_scout.py ml train-risk --dataset corpus/buildimage-d89a360e2 --out models
python3 run_scout.py ml train-risk --dataset corpus/buildimage-d89a360e2 --label reverted_within_90d

# Earlier commits most like one commit, and neighbour lift on the test split
python3 run_scout.py ml similar --dataset corpus/buildimage-d89a360e2 --commit 80a6a0fc4
python3 run_scout.py ml similar --dataset corpus/buildimage-d89a360e2 --evaluate

# The LLM-ready bundle for any commit in the clone, in the dataset or not
python3 run_scout.py ml score --dataset corpus/buildimage-d89a360e2 \
    --model models/risk-bug_introducing.joblib --repo "$BUILDIMAGE" --commit 9a331b65f
```

**A record** holds only facts intrinsic to one commit: metadata with the author as a salted
hash, the parsed message (subject tags, PR number, revert target, template sections), every
changed file with its class, hunks and a capped patch, submodule moves, and the components,
features and entities the paths map to through `scout_impl/repos/buildimage/taxonomy.yaml`.
Email addresses and every author or committer name reachable from the snapshot are replaced
before anything is capped or written.

**The dataset directory** holds:

- `records/`: one record per first-parent commit, in landing order, gzipped JSONL shards of 1,000
- `llm/`: one label-free document per commit for a language model, at most 12,000 characters
- `annotations/`: each commit's category, history features, split and labels as of the snapshot
- `features.parquet`: one row per commit, with intrinsic and history features, one-hot areas and `label_*` columns
- `dataset_card.json` and `DATASET.md`: provenance, counts, label definitions and the split

**Labels.** `reverted` links a later revert by its `This reverts commit` trailer, or by PR
number when there is none; `reverted_within_{7,30,90}d` adds the lead time. `bug_introducing`
is SZZ: `git blame -w -M` at a fix-like commit's parent attributes a non-trivial line the fix
removed or changed to this commit. Merges have no labels. Categories (fix, feature,
submodule-bump, platform-support and so on) are rule-based and carry their evidence.

**No lookahead.** History features for a commit read only commits that landed before it, so
building the dataset up to that commit gives the same values. The split follows landing order
(70% train, 15% validation, 15% test) with a 90-day gap before each boundary and before the
snapshot. `features.parquet` counts a revert or fix only if it landed before its split ends, and
`--exclude FILE` moves listed commits into a `holdout` split no model sees.

**Determinism.** Rebuilding gives byte-identical output whatever the worker count, and records
and blame results are cached under `.scout-cache/` (`--cache ""` disables the cache). Training
with the same seed gives an identical model card.

Measured at `d89a360e2` on 8 cores:

| | |
| --- | --- |
| Commits | 12,881 (24 merges, 1,871 bot-authored), 0 schema errors |
| Build time | 82 s cold including SZZ, 14 s with a warm cache |
| Reverts | 148; 133 reverted commits linked (115 by sha, 18 by PR), 15 unlinked, 4 nested |
| SZZ | 1,857 fix-like commits, 1,710 bug-introducing (17.2% of 9,948 known) |
| Split | train 8,266, validation 1,423, test 1,834, gap 1,358 |
| Unmapped paths | 156 of 72,414 (0.22%) |

Test-split results for `bug_introducing` (1,270 commits, 151 positive), with 95% bootstrap
intervals. Recall at 20% effort is the share of positives found by reviewing the riskiest
commits until 20% of changed lines are read:

| Model | PR-AUC | ROC-AUC | Recall at 20% effort |
| --- | --- | --- | --- |
| Prevalence | 0.119 | 0.500 | 0.258 |
| Size only (log churn) | 0.263 | 0.735 | 0.000 |
| Logistic regression | 0.311 | 0.736 | 0.033 |
| Gradient boosting, selected | 0.376 [0.298, 0.458] | 0.732 | 0.371 |

Of the ten nearest earlier neighbours of a bug-introducing test commit, 20.4% had already been
found bug-introducing when it landed, against 12.7% for the other test commits: a lift of 1.61.

`reverted_within_90d` is too rare to learn from yet: 8 positives in test and 10 in
validation, and every model is near chance. The label is in the dataset for when there is more
history, but it is not a usable risk signal today.

`ml score` returns one JSON bundle for a language model: the commit's label-free document; the
calibrated risk with its percentile among labelled commits and the logistic regression's five
largest contributions; and the five most similar earlier commits with their outcomes as known
when the scored commit landed. A commit outside the dataset is mined and scored as if it landed
right after the snapshot. `in_training_data` marks a commit whose own label the model saw.

Known limits:

- Only author and committer names are redacted. Other people named in messages and GitHub
  handles that are not author names stay.
- A `.gitattributes` in the clone's working tree can change which files git treats as binary.
- Recall at effort ranks by probability, so a model that learns "big commits are risky" scores
  low on it even when its PR-AUC is good; read the two together.

## Tests

```bash
python3 -m pytest -q
```

`pytest.ini` teaches discovery about the `unit_test_*.py` naming, so the obvious command
finds the whole suite — the units and the conformance suite together. Network-dependent
integration tests stay opt-in: `integration_test_*.py` is deliberately absent from
`python_files`, so those run only when named on the command line with
`SCOUT_NETWORK_TESTS=1` set.

Everything else runs offline with no API key and no network: the provider tests replay
committed fixtures, the ingest and miner tests build throwaway git repositories, and the
conformance suite reads pinned tree fixtures.

### Conformance suite

`tests/conformance/` is what replaces a verification stage for the committed detector
(HLD section 4.8). It asserts each counting rule individually against
`tests/fixtures/trees/`, rather than asserting one headline that two compensating bugs
could satisfy:

```bash
python3 -m pytest tests/conformance -q
```

Pinned to `62cfe5086` on upstream master, 21 Sep 2026: 20,155 paths, 287 declarations, 18
of them symlinks with 0 unresolved, 1 naming two families, 3 excluded as `_common`, **284
real platforms**, 30 owning no HWSKU and kept, 9 Build job groups, **196 built and 88
never built**, and the ambiguity resolving to 88 or 71. The fixtures and how to recapture
them are in [tests/fixtures/trees/README.md](tests/fixtures/trees/README.md).

Two of them instead exercise real history, so they need a `sonic-mgmt` checkout supplied.
They take it from the **`SCOUT_TARGET_REPO`** environment variable, falling back to
`/home/goudi/ws/sonic-mgmt` when that variable is unset:

```bash
SCOUT_TARGET_REPO=/path/to/sonic-mgmt python3 -m pytest tests/unit_test_*.py -q
```

If neither is a git working copy those two skip, and the skip reason names the variable so
the fix is obvious. Nothing else in the suite needs it.

## Lint

`.flake8` pins the 120-column limit this package had while it lived inside `sonic-mgmt`,
where it came from that repository's `.pre-commit-config.yaml`:

```bash
python3 -m flake8
```
