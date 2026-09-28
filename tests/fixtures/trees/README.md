# Pinned tree fixtures

Four trees, captured once and committed, so the conformance suite runs with no network and
no clone (NFR-10). Each holds a **filtered** tree listing with paths, modes and blob shas,
the handful of blobs the static stage actually reads, and the size of the tree it came
from. None of them is a clone: the largest is 461 KB against a 20,155-path tree.

| Fixture | Tree | Revision | Why it is pinned |
| --- | --- | --- | --- |
| `sonic-buildimage-master-62cfe50.json` | `sonic-net/sonic-buildimage` master | `62cfe5086`, 2026-09-21 | The tree [../../../docs/scout-hld.md](../../../docs/scout-hld.md) sections 4.3.1 and 6.2 were measured against: 20,155 paths, 287 declarations, 287 platforms, 9 job groups, 196 built, 91 never built under string matching and 80 under the architecture rule |
| `nokia-fork-202605.json` | The Nokia downstream fork, branch `202605` | `faef5faca`, 2026-09-08 | A second tree for rules C5 and C6 — 277 declarations, 278 platforms, 8 job groups — and the tree the `Build`-only scoping check is run against |
| `sonic-buildimage-3589b56.json` | `sonic-net/sonic-buildimage` at commit `3589b565d` | `3589b565d`, 2026-01-05 | The reverse-reach case. That commit edited `device/arista/x86_64-arista_common/pmon_daemon_control.json`, which through inbound symlinks is the pmon configuration of **38** Arista platforms; the stage reported zero before rule C6 |
| `sonic-mgmt-8355f75.json` | The Nokia downstream fork of `sonic-mgmt` | `8355f7581`, 2026-09-15 | The second adapter, present only to falsify the repo-agnostic claim. 183 topologies, 9 PR-checker topology types |

**The fork fixture is not a parser-defect exhibit, and it used to be described as one.**
The "5 job groups against 8" attributed to it was two hand measurements taken with
different stage patterns — a prefix match on `^- stage: Build` that also caught `BuildVS`,
against an exact match that did not — rather than two parses disagreeing. Scout's parser
reads 8 on this tree and its strict and loose parses agree. The `Build`-only scope is still
exercised, because a scope narrower than the pipeline is a real failure mode worth
catching; it is provoked by the test, not inherited from history.

`sonic-buildimage-3589b56.json` is reached through the Nokia clone, which mirrors upstream
commits under the same sha, so it is genuinely upstream at that revision.

The `sonic-mgmt` fixture is a **fork** checkout rather than upstream, because that is what
was reachable on the machine it was captured on. That is fine for what it is for — the
tree shape, the topology family and `PR_TOPOLOGY_TYPE` are the same — but it is why its
183 topologies should not be quoted as an upstream measurement.

**Assert against the pinned sha, never against live master.** Every figure above moves as
the tree moves. A suite that asserted against the network would fail whenever somebody
added a platform, which teaches the team to ignore it, and would pass for the wrong
reasons whenever the network was down.

## What is in one

```text
fixture_version  1.1
repo, adapter    which tree, and which adapter reads it
rev, rev_date    the pinned revision and when it landed
tree_paths       paths in the whole tree — the listing below is filtered, this is not
path_globs       which paths were captured, so a reader can see what it can answer
blob_globs       which blobs were captured
entries          one "mode kind sha path" line each, in git ls-tree form, sorted by path
blobs            blob sha -> content
```

Version 1.1 writes an entry as one line where 1.0 wrote a four-element array. Rule C6 needs
every symlink under `device/`, which took the upstream fixture from 1,255 entries to 3,065,
and at six lines apiece that was nineteen thousand lines of brackets.

Blobs are keyed by **sha, not path**, which is not only compression. The 287
`platform_asic` declarations upstream hold 31 distinct blobs between them, because a file
saying `broadcom` is byte-identical across all 156 platforms that say it, and the 1,853
symlinks hold 493 targets. That is the same fact that lets `TreeIndex` read them once each
rather than once per path, so storing them this way keeps the fixture honest about what a
run costs.

Symlinks are captured by **mode rather than by glob**: the question "which platforms does
this shared file belong to" is asked of paths no declaration glob has any reason to name,
so the capture keeps every mode-`120000` entry under the entity root and its target.

A blob the fixture does not carry **raises** rather than reading as empty. If the analyzer
starts reading a file the capture did not know about — a pipeline template chain that
moved, say — that shows up as a failing test rather than as a quietly different number.

## Recapturing

```bash
python3 tests/fixtures/capture_tree.py \
    --remote sonic-net/sonic-buildimage --rev <sha> \
    --out tests/fixtures/trees/sonic-buildimage-master-<short>.json \
    --note "why this revision"
```

`--checkout <path>` reads a working copy or partial clone that already holds the revision
instead of fetching. The capture derives what to keep from the adapter, and discovers the
pipeline templates by **running the parser**, so a moved template chain is followed rather
than guessed at. Recapturing changes the numbers, so update the assertions in
`tests/conformance/` in the same commit and say which revision they moved to.
