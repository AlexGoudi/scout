# CLI reference

Every command runs through one entry point:

```bash
python3 run_scout.py [global options] <command> [command options]
```

This page lists every command and flag as defined in `scout_impl/cli.py` and
`scout_impl/mining_cli.py`. `python3 run_scout.py <command> --help` prints the same. What the
commands compute is explained in [scout-hld.md](scout-hld.md),
[calibration-and-labels.md](calibration-and-labels.md), [scoring.md](scoring.md) and
[models-and-evaluation.md](models-and-evaluation.md).

## Global options

These belong to the top-level parser, so they go **before** the command name.

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo-root PATH` | current directory | Working copy to read. Mutually exclusive with `--remote`. |
| `--remote REMOTE` | none | Read a remote instead, as `owner/repo` or a URL, over anonymous HTTPS with a blob-filtered partial fetch and no full clone. |
| `--repo-type {sonic-buildimage,sonic-mgmt}` | identified from the tree | Which adapter to use. Required with `--remote` for `mine-azure` and `check-fidelity`. |
| `--cache-dir DIR` | `$SCOUT_CACHE_DIR`, else `$XDG_CACHE_HOME/sonic-scout/repos`, else `~/.cache/sonic-scout/repos` | Where fetched remotes are cached; mined evaluation data lives under `eval/` in it. |
| `--depth N` | 2 | Initial fetch depth for `--remote`; deepens automatically when the change set needs more. |
| `--max-depth N` | 512 | Cap on deepening. |
| `--log-level LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Logs go to stderr. |
| `-q`, `--quiet` | off | Do not print JSON summaries to stdout; log progress only. Accepted anywhere on the command line. |

The `mine`, `dataset` and `ml` groups have their own parser and accept only `-q` (anywhere) and
`--cache-dir` (in front of the group) from this list. Put no other global option in front of them:
`python3 run_scout.py --log-level DEBUG ml ...` is parsed by the wrong parser and fails with
"Unknown command".

### Exit codes

| Code | When |
| --- | --- |
| 0 | Success. `review` also returns 0 when the run degraded, because it is advisory. |
| 1 | A handled error (git, schema, contract, value, network in the `mine`/`dataset`/`ml` groups), or `check-fidelity` found a mismatch. |
| 2 | A usage conflict caught before work starts: `--repo-root` with `--remote`, a mining or scoring command that needs a working copy given a remote, `--pr` without `--remote` on `ingest` or `tree`, the wrong number of `--range`/`--sync`/`--pr` on `ingest`, or a `backtest` error such as a missing corpus. Argparse errors also exit 2. The same mistakes on `brief` and `review` are reported as handled errors and exit 1. |

## Review path

### `ingest`

Resolve a change set and print it as JSON.

| Option | Default | Meaning |
| --- | --- | --- |
| `--range BASE..HEAD` | | Commit range, two-dot or three-dot. |
| `--pr N` | | Pull request number; needs `--remote`. |
| `--sync LOCAL_REF UPSTREAM_REF` | | Fork-sync delta: commits arriving from `UPSTREAM_REF`, plus the local patches they overlap. |
| `--context-lines N` | 3 | Diff context lines kept per hunk. |
| `--include-merges` | off | Keep merge commits, which are excluded by default because their diffs duplicate their parents. |
| `--output PATH` | stdout | Write the JSON here. |

Exactly one of `--range`, `--pr` and `--sync` is required.

### `tree`

List paths at a revision from tree metadata only; against `--remote` it transfers no file content.

| Option | Default | Meaning |
| --- | --- | --- |
| `--rev REV` | `HEAD` | Revision to list. |
| `--pr N` | | List the pull request's head instead; needs `--remote`. |
| `--pattern GLOB` | | Keep only matching paths; `*` spans directories. |
| `--count` | off | Print the number of matching paths instead of the paths. |

### `brief`

Run the deterministic static stage and write `scout-brief.json`. No model, no credential.

| Option | Default | Meaning |
| --- | --- | --- |
| `--pr N` | | Pull request on `--remote`. |
| `--range BASE..HEAD` | | Commit range. |
| `--rev REV` | | Analyse a whole tree with no change set. |
| `--fixture PATH` | | Read a pinned tree fixture instead of a repository; offline, ignores `--remote` and `--repo-root`. |
| `--output PATH` | `scout-brief.json` | Where to write the brief. |
| `--stdout` | off | Print the brief instead of writing it. |
| `--hotspots N` | 10 | Ranked hotspots to keep. |
| `--context-lines N` | 3 | Diff context lines kept per hunk. |

Exactly one of `--pr`, `--range` and `--rev` is required, except with `--fixture`, which supplies
its own revision.

### `review`

Run all stages and write `scout-brief.json`, `scout-report.json`, `scout-comment.md` and
`scout-run.jsonl`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--pr N` | | Pull request on `--remote`. |
| `--range BASE..HEAD` | | Commit range. |
| `--fixture PATH` | | A pinned review fixture (`review.json`) or tree fixture; offline. Takes neither `--pr` nor `--range`. |
| `--provider {ollama,replay,none}` | `ollama` | Who answers the brief's questions. `none` runs degraded: brief and deterministic findings only. |
| `--model NAME` | `qwen2.5-coder:7b` | Model the ollama provider asks. |
| `--ollama-url URL` | `$SCOUT_OLLAMA_URL`, then `$OLLAMA_HOST`, then ollama's default | The ollama server. |
| `--replay-dir DIR` | the fixture's replay directory | Recorded responses for `--provider replay`. |
| `--record DIR` | | Also record every live response here, for later replay. |
| `--output-dir DIR` | `.` | Where the four artifacts are written. |
| `--repo-name NAME` | `--remote` or the checkout's directory name | Repository name recorded in the brief and report. |
| `--timeout S` | 3000 | Seconds per model call, lowered to whatever is left of `--deadline`. |
| `--deadline S` | 1200 | Wall-clock seconds the agent stage may spend before the run degrades. A model call still running at the deadline is cut off. |
| `--hotspots N` | 10 | Ranked hotspots the brief keeps. |
| `--context-lines N` | 3 | Diff context lines kept per hunk. |

## Calibration and scoring

These need `--repo-root` pointing at a clone, except `mine-azure` and `check-fidelity`, which also
accept `--remote` with `--repo-type`. The default output directory is
`<cache>/eval/calibration/<adapter>/`.

### `mine-incidents`

Mine `^Revert` commits and their links into `incidents.jsonl` in the calibration directory, and write
the same corpus to `--output`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--revision REV` | `HEAD` | Revision to mine. |
| `--grep PATTERN` | `^Revert` | Commit-message pattern selecting reverts. |
| `--limit N` | | Only the most recent N matching commits; always re-mines. |
| `--output PATH` | `scout-corpus.jsonl` | JSONL corpus file. Unlike the other mining commands, this is a file, not the calibration directory. |
| `--refresh` | off | Re-mine from scratch instead of appending since the cached tip. |

### `mine-git-dumps`

Write `git-overview.json`, `git-commit-file-changes.json` and `git-file-revert-rates.json`, and
refresh `incidents.jsonl`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--revision REV` | `origin/master` | Ref to walk. |
| `--output DIR` | calibration directory | Directory override. |
| `--refresh` | off | Re-mine everything even when the cache for this ref and tip is complete. |

### `mine-azure`

Fetch Azure DevOps PR builds and job timelines: `azure-definitions.json`,
`azure-pr-pipeline-builds.json` and `azure-pr-job-timelines.json`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--output DIR` | calibration directory | Directory override. |
| `--workers N` | `$SCOUT_AZURE_WORKERS`, else 6 (1 to 32) | Concurrent timeline fetches; 1 is serial. |
| `--refresh` | off | Re-fetch from scratch instead of resuming. |

### `mine-labels`

Join timelines with git paths: writes `azure-pr-file-changes.json`, every `model-*` file,
`scoring-plan.json`, and `repo-pytest-markers.json` or `repo-docker-from.json`. Fetches PR refs into
the clone when a PR is not a squash commit on the ref.

| Option | Default | Meaning |
| --- | --- | --- |
| `--output DIR` | calibration directory | Directory holding the `git-*` and `azure-*` inputs, and receiving the outputs. |
| `--refresh` | off | Rerun the join even when it is fresh. |

### `score-pr`

Print the phase-0 per-job score of `BASE...HEAD` as JSON on stdout, even under `-q`. See
[scoring.md](scoring.md).

| Option | Default | Meaning |
| --- | --- | --- |
| `--base REF` | `origin/master` | Diff base. |
| `--brief PATH` | | Score a brief's `base_sha...head_sha` instead, or its hotspots when the checkout lacks those commits (`files_complete: false`). |
| `--calibration DIR` | calibration directory | Directory with `scoring-plan.json`. |

### `check-fidelity`

Compare the adapter's gold jobs with the job keys in `azure-pr-job-timelines.json`; exit 1 on a
mismatch.

| Option | Default | Meaning |
| --- | --- | --- |
| `--output DIR` | calibration directory | Directory holding the timelines. |

### `backtest`

Grade D6 on the seed corpus. It does not use `--repo-root` or `--remote`; it fetches upstream
history itself into `<cache>/eval/history/` when capturing.

| Option | Default | Meaning |
| --- | --- | --- |
| `--adapter {sonic-buildimage,sonic-mgmt}` | `sonic-buildimage` | Only `sonic-buildimage` has a corpus; any other value exits 2. |
| `--capture` | off | Allow network: mine the corpus if none is pinned and capture missing fixtures. Without it, replay only, and exit 2 if no corpus is pinned. |
| `--corpus-dir DIR` | `<cache>/eval/backtest/<adapter>` | Pinned corpus directory. |
| `--revision REV` | | Pin the history tip when mining a new corpus. |

## `mine` group

### `mine commit`

Print the JSON commit record of one commit (schema `schemas/scout-commit-1.1.json`).

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo PATH` | required | Local clone. |
| `--commit REV` | required | Commit to mine. |
| `--names-from REV` | `HEAD` | Revision whose tree supplies the platform and entity names; falls back to `--commit` when it does not resolve. |
| `--output PATH` | stdout | Write the record here. |

## `dataset` group

### `dataset build`

Build the commit dataset from the first-parent history of `--rev`. The clone must not be shallow.

| Option | Default | Meaning |
| --- | --- | --- |
| `--repo PATH` | required | Local clone. |
| `--rev REV` | required | Snapshot revision. |
| `--repo-type {sonic-buildimage,sonic-mgmt}` | `sonic-buildimage` taxonomy | Selects the taxonomy and the output directory name. Always pass it. |
| `--out DIR` | `corpus/<repo-type>-<sha9>` | Output directory. |
| `--workers N` | 0 (every CPU) | Parallel workers. |
| `--exclude FILE` | | SHAs or unique prefixes to put in the `holdout` split. |
| `--no-szz` | off | Skip SZZ blame; `bug_introducing` stays null. |
| `--cache DIR` | `.scout-cache` in the repository root | Commit-record cache. |
| `--refresh` | off | Rebuild even when the fingerprint matches. |

## `ml` group

| Command | Options | Notes |
| --- | --- | --- |
| `ml train-risk` | `--dataset DIR` (required), `--label {bug_introducing,reverted_within_90d}` (default `bug_introducing`), `--out DIR` (default `models`), `--seed N` (default 0), `--refresh`, `--final` | Writes `risk-<label>[-final].{joblib,model_card.json,report.md}` into `--out`. |
| `ml similar` | `--dataset DIR` (required), exactly one of `--commit SHA` or `--evaluate`, `-k N` (default 10) | Prints JSON. |
| `ml score` | `--dataset DIR`, `--model PATH`, `--repo PATH`, `--commit SHA` (all required), `-k N` (default 5) | Prints the risk bundle as JSON. |
| `ml train-pr-job` | `--repo-type` (required), `--repo-root PATH` (required), `--calibration DIR`, `--out DIR` (default `models`), `--final` | Gated on fidelity; kept only if it beats `heuristic_p` on validation. `--final` refits a kept model on every split into `pr-job-<repo-type>-final.*`. `--repo-root` is recorded in the card only. |
| `ml walk-forward` | `--dataset DIR`, `--model PATH`, `--repo-type` (all required) | Refuses a `--final` model. |
| `ml watch` | `--remote OWNER/REPO` (required), `--repo-type` (required), `--calibration DIR`, `--max-prs N` (default 100) | Calls the GitHub API. |
| `ml grade` | `--repo-type` (required), `--calibration DIR` | Writes `online-scorecard.json` next to the ledger. |

`--calibration` defaults to `<cache>/eval/calibration/<adapter>`, and the online ledger lives at
`<cache>/eval/online/<adapter>/ledger.jsonl`, where `<cache>` is `--cache-dir` given in front of
`ml`, else `$SCOUT_CACHE_DIR` or its default. Errors print `error: <message>` to stderr and exit 1.

## Environment variables

| Variable | Used by |
| --- | --- |
| `SCOUT_CACHE_DIR`, `XDG_CACHE_HOME` | Default cache root |
| `SCOUT_AZURE_WORKERS` | `mine-azure` worker count |
| `SCOUT_OLLAMA_URL`, `OLLAMA_HOST` | `review` with `--provider ollama` |
| `GITHUB_TOKEN` or `GH_TOKEN` | GitHub REST calls made by `ml watch`; optional, but anonymous access allows about 60 requests an hour. `--remote --pr` uses git refs only and needs no token. |
| `SCOUT_TARGET_REPO`, `SCOUT_NETWORK_TESTS`, `SCOUT_NETWORK_REMOTE`, `SCOUT_OLLAMA_TESTS`, `SCOUT_OLLAMA_MODEL`, `SCOUT_BUILDIMAGE_REPO`, `SCOUT_RECORD_DEMO` | Tests only; see [development.md](development.md#tests) |

The demo scripts read more; see [demos.md](demos.md).
