# SONiC Scout: High-Level Design

This document describes what SONiC Scout does and how it is built, as implemented in this
repository. Command syntax is in [cli-reference.md](cli-reference.md); the evaluation data in
[calibration-and-labels.md](calibration-and-labels.md), [scoring.md](scoring.md) and
[models-and-evaluation.md](models-and-evaluation.md).

Keep section numbers stable so links into this page keep working. Paths such as `device/` are in the
analyzed SONiC tree unless they start with `scout_impl/`, `schemas/` or `tests/`.

---

## 1. Purpose

Scout reads an incoming change set (a pull request, a commit range, or a fork sync delta) and
predicts what the change puts at risk before an image is built, citing file and line.

It has two halves that share one CLI, `python3 run_scout.py`:

1. **The review path** (sections 4 to 6). It runs a deterministic static stage, an agent stage
   bounded by what the static stage found, and a report. The one committed detector, D6, finds the
   CI coverage gap: platforms a change reaches that the pull-request pipeline never builds.
2. **Break-risk evaluation and scoring** (section 7). This half mines git and Azure DevOps history
   for upstream SONiC, joins it into labelled per-job datasets, and scores a change with a
   rule-based phase-0 scorer that trained models must beat.

### 1.1 Advisory only

A Scout result never blocks a merge; Azure Pipelines remains the merge gate. A run that cannot reach
a model, or runs out of budget, reports `degraded`, still ships the brief and the deterministic
findings, and exits 0. No command fails a build because of a finding. When unsure, Scout says less:
low-confidence adjudications stay in the JSON and are left out of the comment.

## 2. Requirements

The identifiers below are cited from code and tests. Only implemented behaviour is listed.

| ID | Requirement |
| --- | --- |
| FR-1 | Accept a change set as a PR number, a commit range or a fork sync delta, and resolve it without a local checkout of the analyzed repository. |
| FR-4 | Build the entity index (platforms, ASIC families, CI job groups for `sonic-buildimage`; topologies for `sonic-mgmt`) mainly from tree metadata. |
| FR-5 | Answer the coverage-gap query: which affected platforms the PR pipeline builds, which it never builds, and which cannot be decided. |
| FR-7 | Every claim carries file-and-line evidence. A claim whose citation does not re-read is dropped, not caveated. |
| FR-10 | Band every adjudication, and suppress the low band. The thresholds are provisional until the backtest can tune them (section 4.9). |
| FR-12 | A local CLI produces the same artifacts for any PR or range. |
| FR-17 | The risk brief is a versioned, machine-readable contract between the stages, validated on both sides. |
| NFR-2 | The agent stage has a wall-clock deadline (default 20 minutes) after which the run degrades. |
| NFR-3 | The brief is byte-identical for the same pinned tree, change set and run id. Model calls use temperature 0 and pinned prompts. |
| NFR-4 | Reports and comments are ranked and capped at 10 findings. |
| NFR-6 | Advisory only (section 1.1). |
| NFR-7 | An unreachable provider or an exhausted budget produces a `degraded` report that still carries the brief. |
| NFR-9 | Every excerpt sent to a model is recorded in the run log by path, revision and line range. |
| NFR-10 | Unit and conformance tests, the backtest replay and fixture reviews run offline, with pinned fixtures and a replay provider. |
| NFR-11 | Detectors are data plus a synthesis function; adding one does not touch the agent loop. Adding a repository does not touch the core. |
| NFR-12 | The static stage answers from tree metadata where it can. Blob reads are counted and published in the brief as `blobs_read`. |

## 3. Scope

- **Repositories.** Upstream `sonic-net/sonic-buildimage` (primary) and `sonic-net/sonic-mgmt`
  (second adapter), on `master`. Scout is not a SONiC tree. It reads sibling clones, or fetches a
  remote anonymously into its own cache. It never pushes or touches a clone's working tree; the only
  write to a clone is `mine-labels` fetching PR heads into `refs/tmp/pr-*`.
- **`sonic-buildimage`** has the full review path, D6, the seed-corpus backtest, the commit dataset
  and the Azure image-job labels.
- **`sonic-mgmt`** is at smoke level on the review path: a schema-valid brief over topologies and
  PR-checker coverage, but no detector, so no questions. Its evaluation half is complete.
- **Out of scope:** merge gating, vendor-fork analysis, image builds, lab or hardware access, and
  fine-tuning a model.

## 4. The review path

### 4.1 Pipeline and artifacts

Stage 0 ingests the change, stage 1 writes the brief, stage 2 answers the brief's questions with a
model, stage 3 would verify the answers (D6 needs none), and stage 4 writes the report and renders
the comment. `review` (orchestrated by `scout_impl/core/review.py`) writes four artifacts into
`--output-dir`:

| Artifact | Written by | When |
| --- | --- | --- |
| `scout-brief.json` | stage 1 | as soon as it validates, before any model call |
| `scout-run.jsonl` | every stage | record by record, so a killed run leaves its trace |
| `scout-report.json` | stage 4 | after stage 2, validated on write |
| `scout-comment.md` | stage 4 | rendered from the report and nothing else |

`brief` runs stage 1 alone. Stage 2 and the report builder never raise for anything a model or a
budget can do, so the only failures that stop a review are stage 1's own.

### 4.2 Stage 0: ingest

`scout_impl/ingest.py` resolves a change set into commits and per-file diffs (`ChangeSet`,
`scout_impl/models.py`). Sources implement one interface (`scout_impl/source.py`):

- `LocalCheckout` reads a working copy given by `--repo-root`.
- `RemoteRepo` (`scout_impl/remote.py`) does a shallow `git fetch --filter=blob:none` into a cache
  keyed by remote, starting at `--depth` (default 2) and deepening up to `--max-depth` (default
  512) when a merge base is not yet reachable. No credential is needed.

Modes: `--pr N` (needs `--remote`), `--range BASE..HEAD` or `BASE...HEAD`, and `--sync LOCAL UPSTREAM`
for a fork sync delta, which records the fork-local commits that touch the same files. Merge commits
are excluded unless `--include-merges` is given. Pinned fixtures replace the source entirely for
offline runs (`--fixture`).

With a blob-filtered fetch, listing a tree is free and each blob read is a round trip, so the static
stage answers from paths, modes and blob SHAs and reads contents only where it must (NFR-12).

### 4.3 Stage 1: deterministic static analysis

No model, no credential, no network beyond stage 0's fetch. `scout_impl/static/engine.py` runs the
substages in order and times them:

1. **Path classification.** Each changed path gets the adapter's `PathClass` (`RepoAdapter.classify`).
2. **Entity resolution.** `static/platforms.py` builds the entity index from the adapter's
   `entity_model` (section 4.3.1).
3. **Coverage model.** `static/pipeline.py` parses the adapter's `coverage_spec` (section 6.2.2).
4. **Coverage-gap query.** `static/coverage.py` splits the affected entities into `covered`,
   `uncovered` and `ambiguous`; the sets are disjoint and exhaustive over `affected`.
5. **Hotspot ranking.** `static/hotspots.py` scores each changed path from four components (path
   class 0.30, entity fan-out 0.20, coverage gap 0.30, ambiguity 0.20). The components sum exactly
   to `score`, and the list is capped by `--hotspots` (default 10).
6. **Rule and question synthesis.** The adapter's detectors turn the result into rules, questions
   and unresolved items (section 4.7).
7. **Paths to assess.** `static/related.py` maps changed paths to the feature groups named in
   `scout_impl/static/datapath.json` and lists what else those groups hold.

#### 4.3.1 The counting rules

Which `device/` directories count as platforms is a classification, and every coverage number depends
on it. The rules are data on `DirectoryEntitySpec` in `scout_impl/repos/sonic_buildimage.py`, and the
conformance suite (`tests/conformance/test_counting_rules.py`) asserts each one against pinned trees.

| Rule | Statement |
| --- | --- |
| C1 | Follow `platform_asic` symlinks (git mode `120000`) by path arithmetic against the listed tree, up to `max_link_hops`. A target that escapes the root is an error; one that does not resolve is recorded, not dropped. |
| C2 | A `platform_asic` file may declare several ASIC families, one per line. It parses to a set. |
| C3 | Directories whose name ends in `_common` are shared, not hardware, and are excluded from the platform count. |
| C4 | A platform owning no HWSKU directory is kept. The count of such platforms is published, never used to exclude. |
| C5 | A platform is covered if any declared family is built in PR CI for its architecture. This lives in the coverage query. |
| C6 | `device/` shares data by symlink. Entity directories that are themselves symlinks are platforms (outbound), and a change to a file other platforms link to reaches every one of them (inbound). |

The brief publishes each term, so the arithmetic is auditable. On the pinned tree `62cfe5086`
(`tests/fixtures/trees/sonic-buildimage-master-62cfe50.json`, 20,155 paths), 287 declarations less 3
shared directories plus 3 symlinked platforms give 287 platforms; 9 job groups build 196 of them, 74
are never built and 17 are ambiguous, so 91 are never built under string equality and 80 under the
architecture rule.

#### 4.3.2 Family-name matching and the ambiguous set

PR CI job groups are architecture-qualified (for example `marvell-prestera-arm64` and
`marvell-prestera-armhf`), while platforms declare the unqualified family, and `platform_asic` does
not state an architecture. Whether such a platform is covered depends on its CPU architecture, which
Scout reads from the ONIE prefix of the directory name (`x86_64`, `arm64`, `armhf`). That is a
naming convention, not a declaration.

The static stage does not silently pick an answer. Such platforms go into `coverage.ambiguous`, the
brief carries both readings (string equality and architecture-aware), and the architecture rule's
answer for each is the `rule_candidate` on unresolved item `u-001`: facts go in as facts,
under-determined facts as questions.

#### 4.3.3 Symlink resolution

The listing shows mode `120000` for free. Resolving a link reads its blob, joins the target to the
containing directory, normalizes it, and looks it up in the listing, iteratively up to a hop limit.
The conformance suite asserts there are no unresolved links on the pinned trees.

### 4.4 The risk brief

The brief (`schemas/scout-brief-1.0.json`, built by `scout_impl/static/brief.py`) is stage 1's
deliverable and stage 2's entire input. Stage 1 validates it on write and stage 2 on read, against
the JSON schema and against contracts a schema cannot express.

| Block | Contract |
| --- | --- |
| `brief` | Run header: repo, adapter, mode, `base_sha`, `head_sha`, `tree_paths`, `static_duration_s`, `blobs_read`, status. |
| `hotspots` | Ranked paths with `score_breakdown` components summing to `score`. |
| `entities` | The closed world. A finding may name no entity outside it. |
| `coverage` | Counting-rule figures, job-group names and the parse record (`scope`, `strict`, `loose_scan_agrees`), and the four coverage sets. |
| `rules` | Invariants with citations into the tree (for `sonic-buildimage`, BI-R1 to BI-R4). |
| `questions` | Each names its rule, the entities it may range over, the evidence roles a valid answer needs, and its own budget (12 tool calls and 6 blob reads for D6). |
| `unresolved` | Under-determined facts with candidate answers and the rule under which each holds. |
| `entity_closure`, `budget` | The closure flag and the run budget (60 tool calls, 30 blob reads, 120,000 input tokens by default). |
| `paths_to_assess` | Optional feature-group context from `datapath.json`. |

The brief is a pure function of tree, change set and adapter, so byte-identity is a unit test (NFR-3).

### 4.5 Stage 2: the agent stage

`scout_impl/agent/` asks the brief's questions and nothing else. `assemble.py` pre-assembles the
evidence each question needs, `prompts.py` holds the pinned prompts, and `runner.py` drives the
conversation. The model replies in a JSON protocol (`protocol.py`) with either an `answer` or one of
three read-only tool calls served by `toolbox.py`: `read_blob`, `list_tree` and `grep`, each bounded
in lines, entries and files. The model cannot write, execute or reach the network.

Five constraints are enforced in code on what the model returned, in this order, by
`scout_impl/agent/checks.py` and the runner:

| Check | On failure |
| --- | --- |
| Question binding | An answer to no group of the question is dropped and counted. |
| Entity closure | An answer naming a platform or job group outside the brief or the question's evidence is dropped whole. |
| Cited job group | A "covered" claim must name a job group the parse found, building a declared family for the platform's architecture; otherwise it is dropped. |
| Citation | Every cited evidence id must exist and re-read, at its revision, to exactly its quote (section 4.6). |
| Rule consistency | An answer that disagrees with the brief's `rule_candidate` is kept but marked `contested`, and never overrides the rule. |

`budget.py` charges tool calls, blob reads and input tokens per question and per run, against a
wall-clock deadline (`--deadline`, default 1,200 s). An exhausted question is `truncated`; an
exhausted run degrades. A missing or unreachable provider never raises.

#### 4.5.1 Why the brief carries rules, not only facts

Given only the facts of an ambiguous case, a small local model tended to answer "covered" and invent
a reason. Given the same facts plus the architecture rules BI-R3 and BI-R4 stated as rules, it
answered correctly. The design follows from that: the static stage derives rules and the agent
applies them; the rules are emitted with citations into the brief rather than written into a
prompt; and disagreement with the rule's answer is flagged by the rule-consistency check instead of
being trusted.

### 4.6 Evidence and citations

Evidence (`scout_impl/agent/evidence.py`) is `path`, `line_start`, `line_end`, `rev` (`head` or
`base`), `role` and `quote`. Roles are `cause` (the change), `affected` (an artifact it reaches),
`precedent` (a historical commit) and `contract` (what establishes the invariant); D6 requires
`cause`, `affected` and `contract`. A quote that does not re-read at its revision drops its answer.

### 4.7 Detector catalog

Detectors are registered in `scout_impl/detectors/__init__.py`. Each is a spec (trigger, question,
required evidence roles, verification method, confidence prior; `detectors/base.py`) plus a
synthesis function. One detector is implemented:

| Property | D6, CI coverage gap (`detectors/coverage_gap.py`) |
| --- | --- |
| Trigger | A changed path resolves to at least one platform. |
| Deterministic part | Path to platforms, platforms to ASIC families, families to PR-CI job groups; the covered, uncovered and ambiguous sets. |
| Agent part | Confirm or contest, with cited evidence, the architecture rule's answer for each ambiguous platform; judge whether the change materially reaches the uncovered platforms or only passes through. |
| Required evidence | `cause` in the diff, `affected` naming the platform's `platform_asic`, `contract` in the pipeline definition. |
| Verification | None (section 4.8). |

### 4.8 Stage 3: verification

There is no runtime verification stage in the code. D6's claim is a
deterministic fact about files Scout has already read, so re-running the lookup would prove nothing,
and each finding carries `verification: {"method": "none", "result": "not-applicable"}`. Confidence
in D6 comes from tests instead: the conformance suite over pinned trees, the byte-identical brief
test, and stage 2's citation re-read.

### 4.9 Confidence

`scout_impl/report/scoring.py` keeps the arithmetic and the judgement apart. The deterministic half
of a D6 finding is `proven` by construction: that is a claim about the counting, not about whether
the platforms matter. Only the adjudication is banded, from evidence completeness (required roles
against roles actually cited and re-read), breadth (platforms covered by complete answers), the
question's prior, and whether it was contested.

| Band | When | In the comment |
| --- | --- | --- |
| `proven` | Deterministic half | Always |
| `high` | Complete, broad, not contested, prior not weak | Shown |
| `medium` | Complete but narrow, or contested | Shown while fewer than five findings rank higher |
| `low` | No complete answer, undecided, or weak prior | Kept in the JSON only |

The constants (`BROAD_AT`, `WEAK_PRIOR`, `MEDIUM_POSTING_LIMIT`) are provisional. They are meant to be
tuned against the backtest, which is too small to do so yet (section 6.3).

### 4.10 Report

`scout-report.json` (`schemas/scout-report-2.0.json`, `scout_impl/report/builder.py`) is the single
source of truth; `scout-comment.md` renders it (`report/render.py`). It references the brief by path
and SHA. Top-level fields are `schema_version`, `run`, `findings`, `suppressed` and
`degraded_reason`. Each finding answers one brief question, with a `deterministic` half, a banded
`adjudication` half with per-group outcomes, the evidence, the affected platforms (always the
brief's) and `verification`. The builder refuses a report that names an entity outside the brief,
answers no brief question, bands a contested adjudication above `medium`, or disagrees with the
brief's coverage numbers. Findings beyond 10 go to `suppressed`.

## 5. Architecture

`run_scout.py` calls `scout_impl.cli`, which dispatches review-path commands to `scout_impl/core/`
and evaluation commands to `scout_impl/eval/`, `mining/`, `dataset/` and `ml/`. `core/` drives
ingest, `static/` (which calls `detectors/`), `agent/` (which calls a provider) and `report/`. Both
halves read repository specifics only from `repos/`. The package map is in
[development.md](development.md#layout).

### 5.1 Repo adapters

The core knows nothing about SONiC. Everything repository-specific is a `RepoAdapter` value
(`scout_impl/repos/base.py`) registered in `scout_impl/repos/__init__.py`:

| Field | `sonic-buildimage` | `sonic-mgmt` |
| --- | --- | --- |
| `markers` | `slave.mk` and `Makefile.work`, or `rules/config` and `platform` | `ansible/testbed-cli.sh`, or `ansible/vars` and `tests/common` |
| `path_classes`, `path_rules` | Build rules, platform data, dockers, sources, pipeline, docs | Ansible code, shared test infrastructure, data-file families, pipeline, leaf tests, docs |
| `entity_model` | `DirectoryEntitySpec` over `device/`, rules C1 to C6 | `FileEntitySpec` over `ansible/vars/topo_*.yml` |
| `coverage_spec` | `PipelineCoverageSpec`: `azure-pipelines.yml`, stages `Build` and `BuildVS` | `ConstantCoverageSpec`: `PR_TOPOLOGY_TYPE` in `constant.py` |
| `rules`, `detectors` | BI-R1 to BI-R4, D6 | none |
| `github_api`, `azure_devops`, `calibration` | From `sonic_buildimage_calibration.py` | From `sonic_mgmt_calibration.py` |

Without `--repo-type`, `resolve_adapter` identifies the repository by its markers; matching two
adapters is an error. `tests/conformance/test_second_repository.py` records what the second adapter
forced into the core ([adding-a-repo-adapter.md](adding-a-repo-adapter.md)).

## 6. Component design

### 6.1 Orchestration

`scout_impl/core/static_run.py` runs stage 1 for `brief`; `core/review.py` runs stages 1 to 4 over a
source or a pinned review fixture (`core/review_fixture.py`), always producing a brief and a report
unless stage 1 fails, in the order of section 4.1.

### 6.2 The `sonic-buildimage` entity graph

A changed path reaches platform directories under `device/<vendor>/`, directly or through inbound
symlinks (C6). Each platform declares ASIC families in `platform_asic` and has an architecture from
its directory prefix; each PR-CI job group builds one family for one architecture.

#### 6.2.1 The coverage-gap query

A platform is covered if some job group builds a declared family for its architecture, uncovered if
none does under either matching rule, and ambiguous if the answer depends on the rule (4.3.2).

#### 6.2.2 The pipeline parser

The coverage model is the ground truth for every D6 finding, and a wrong parse would change every
finding while citations still resolved and the brief still validated. `scout_impl/static/pipeline.py`
therefore:

- scopes to the named stages (`Build` and `BuildVS`) and raises `PipelineStageNotFound` if one is
  missing, rather than falling back to a whole-file scan;
- resolves template indirection to the job groups actually scheduled, reading the family and
  architecture from the `PLATFORM_NAME` and `PLATFORM_ARCH` variables, with `amd64` as the default;
- cross-checks the strict parse against a loose whole-file scan and raises
  `PipelineParseDisagreement` if they disagree;
- publishes the job-group list and the parse record in the brief.

It uses PyYAML, since the pipeline has anchors, templates and expression-valued keys (limits: section 10).

### 6.3 Seed corpus and D6 backtest

Git history is a labelled dataset. `scout_impl/incidents.py` mines `^Revert` commits and links each
to the commit it reverts. `scout_impl/eval/corpus.py` selects items by rule rather than by hand, and
marks every item auto-selected, not human-adjudicated:

- **Incidents** (up to 24): auto-linked, non-nested reverts whose cause touched `device/` or
  `platform/` or whose revert names a platform, HWSKU, vendor or ASIC family; whose cause tree has a
  pipeline the coverage model parses; and whose revert names at least one entity of that tree. The
  most recent are taken. A shortfall is recorded, never made up by loosening the filter.
- **Controls** (up to 20): single-parent `master` commits touching `device/` or `platform/` within the
  incidents' span, never named by a revert, at least 90 days older than the history tip, taken in a
  salted SHA-256 order fixed before any backtest runs.

`backtest` runs the static stage on each item at its cause commit (`parent..cause`) from a pinned
per-item fixture. An incident is recalled when the never-built set names a platform, family or
vendor the revert names; a control is a false positive when it gets any never-built platform. Both
readings are graded, string equality (uncovered plus ambiguous) and architecture-aware (uncovered
only), each with a Wilson 95% interval. See [models-and-evaluation.md](models-and-evaluation.md#the-d6-backtest).

### 6.4 Model provider and cost control

`Provider` (`scout_impl/provider.py`) has one live implementation, `OllamaProvider`
(`scout_impl/ollama.py`, default model `qwen2.5:7b-instruct`), which calls a local ollama server, so
no code leaves the machine; loopback calls bypass HTTP proxies. `ReplayProvider` serves recorded
responses and `RecordingProvider` records a live one. Cost is bounded by the brief's finite question
list, the per-question budgets, the run budget and the deadline.

### 6.5 Caching

| Cache | Key | Location |
| --- | --- | --- |
| Fetched remotes | Remote URL | `--cache-dir`, default `$SCOUT_CACHE_DIR`, else `$XDG_CACHE_HOME/sonic-scout/repos`, else `~/.cache/sonic-scout/repos` |
| Tree listing and blob contents | Git object SHA | `static/treeindex.py`, shared by the whole static stage; content-addressed, so never stale |
| Recorded model responses | `(prompt_sha, model_id, input_hash)` | A replay directory (`--record`, `--replay-dir`) |
| Commit records and SZZ blame | Extractor version, taxonomy, redaction set, salt, limits | `dataset build --cache`, default `.scout-cache/` in the repo (gitignored) |
| Calibration steps | Step fingerprint in `manifest.json` | `<cache>/eval/calibration/<adapter>/` |

### 6.6 Secrets and trust boundary

Ingest, `brief` and `review` need no credential, and Azure DevOps REST is public. GitHub REST calls
(`ml watch`) send `GITHUB_TOKEN` or `GH_TOKEN` when set and are rate-limited hard without one. No
credential is stored in the repository; `.env` is gitignored and read only by the demo scripts.
Egress is what the provider receives, and every excerpt is named in the run log (NFR-9). Entity
closure means text in the tree cannot make the agent report on an entity the static stage never named.

### 6.7 Observability

`scout-run.jsonl` (`scout_impl/core/runlog.py`) holds one record per stage boundary, model call, tool
call and blob read, timed from run start, so two logs of one run differ only in durations. The
report's `run` block summarizes durations, questions, model calls, tokens and checks fired.

## 7. Break-risk evaluation and scoring

Each adapter answers its own question from upstream Azure DevOps results (`mssonic/build`):

| Adapter | Question | Gold label |
| --- | --- | --- |
| `sonic-buildimage` | Will this PR break image assembly? | The image jobs of `Azure.sonic-buildimage` (definition 1) |
| `sonic-mgmt` | Will this PR break required tests? | Pre_test jobs plus classic KVM Elastictest jobs of `Azure.sonic-mgmt` (definition 39) |

The git miners turn the clone's history into revert rates and per-path priors; `mine-azure` fetches
PR builds and job timelines, up to three attempts per PR; `mine-labels` joins them with each PR's
changed paths into the `model-*` files and `scoring-plan.json`, split 70/15/15 by PR in time order
([calibration-and-labels.md](calibration-and-labels.md)). `score-pr` applies the phase-0 rule to a
diff offline ([scoring.md](scoring.md)). `ml train-pr-job` may replace that rule only if it beats it
on validation and test, and `ml watch` and `ml grade` score open PRs before Azure answers and grade
them afterwards. Separately, `dataset build` and `ml train-risk` build a commit dataset with revert
and SZZ labels and train commit-risk baselines on it
([models-and-evaluation.md](models-and-evaluation.md)).

## 8. Data locations

Mined data never lives in the repository. Under `<cache>/eval/` (section 6.5) are
`calibration/<adapter>/` (the `git-*`, `azure-*` and `model-*` files, `incidents.jsonl`,
`scoring-plan.json`, `manifest.json`), `backtest/<adapter>/` (the pinned corpus, fixtures and
`scorecard.json`), `online/<adapter>/` (the ledger and its scorecard), and `history/` and `items/`
(fetched upstream history and per-item captures). In the repository, `corpus/`, `models/` and
`.scout-cache/` are generated and gitignored; `schemas/` and `tests/fixtures/` are committed.

## 9. Design constraints

- **Upstream only.** Calibration reads `sonic-net` remotes on `master`; merged commits stay in every
  set, whoever wrote them.
- **Gold labels per adapter.** The parent pipeline result is never a label: it is red far more often
  than the gold jobs, because of VPP Elastictest on `sonic-mgmt` and KVM Test on `sonic-buildimage`.
- **No identity features.** `scout_impl/ml/matrix.py` drops every `author_*`, `committer_*` and
  `file_prior_authors*` column (`IDENTITY_PREFIXES`) before a model sees the table.
- **Reverts are a weak proxy.** Revert priors feed the phase-0 lift, and `reverted_within_90d` can be
  trained for measurement, but neither is a scorer's target.
- **No Azure query inside a scorer.** `score-pr` depends only on the changed paths and
  `scoring-plan.json`.
- **Chronological splits.** The Azure join splits by PR; the commit dataset by landing order with
  90-day gaps. Base rates and priors come from the train split only.
- **Raw dumps stay separate.** Only `model-*` files join mined families.

## 10. Known limitations

- **One detector.** Only D6 exists, and only for `sonic-buildimage`. A change that reaches no platform
  is invisible to it by design.
- **Small backtest.** The seed corpus is auto-selected, and the pinned one holds 20 incidents, not
  the 24 requested. In the scorecard generated on 1 Oct 2026 at history tip `9495839`, the
  architecture-aware reading recalled 2 of 20 incidents (Wilson 95% interval 0.03 to 0.30) and flagged
  1 of 20 controls (0.01 to 0.24). The banding constants of section 4.9 are therefore not calibrated.
- **Few, clustered failures.** A `sonic-buildimage` PR that breaks the build usually fails eight or
  nine image jobs at once, so job-row metrics rest on very few PRs, and the phase-0 baseline is weak
  ([scoring.md](scoring.md#measured-baseline)).
- **Azure retention.** Azure keeps PR runs only for a short time (about ten days, as observed), so
  `mine-azure` must run at least weekly or the sample stops growing.
- **Pipeline parser.** Matrix strategies and conditional job inclusion are not modelled.
- **Coverage fidelity is name-level.** `check-fidelity` compares the adapter's gold job list with
  the job keys seen in Azure timelines. It does not compare the static stage's parsed coverage model
  with Azure.
- **`sonic-mgmt` review path** has no detector, so its briefs carry no questions and its reports none.
