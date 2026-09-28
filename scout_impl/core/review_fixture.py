"""A pinned review: a change set and the trees on both sides of it, replayed with no git and no network.

`brief --fixture` replays one tree, which is all the static stage reads. A review needs
more: the agent stage reads both sides of the change, and the evidence of a change is its
diff. So a review fixture is a small manifest joining three files the project's own tools
already produce, the change set `run_scout.py ingest` writes and one tree fixture per side
from `tests/fixtures/capture_tree.py`, plus the directory of recorded model responses the
replay provider serves:

```text
review.json     manifest: repo, adapter, mode, the pinned run id and clock, and the file names below
changeset.json  ChangeSet.to_dict(), as `run_scout.py ingest --range BASE..HEAD` writes it
tree-head.json  TreeFixture at the head, capture_tree.py's selection plus what the live run read
tree-base.json  TreeFixture at the base, only when the live run read anything there
replay/         <digest>.json responses, written by RecordingProvider during the live run
```

The run id and the clock are pinned in the manifest so that two replays of one fixture
write byte-identical artifacts apart from the timings. A plain tree fixture is accepted too,
and reviews the whole tree with no change set, which is `brief --fixture`'s behaviour.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import ChangeSet
from ..source import RepoSource, TreeEntry
from ..static.engine import MODE_TREE
from ..static.fixtures import FixtureError, FixtureSource, TreeFixture

REVIEW_FIXTURE_VERSION = "1.0"
KIND_REVIEW = "review"
MANIFEST = "review.json"
CHANGE_SET_FILE = "changeset.json"
HEAD_TREE_FILE = "tree-head.json"
BASE_TREE_FILE = "tree-base.json"
REPLAY_DIR = "replay"


@dataclass(frozen=True)
class ReviewFixture:
    """One pinned review, loaded."""

    path: Path
    repo: str
    adapter: str
    mode: str
    head: TreeFixture
    base: Optional[TreeFixture] = None
    change_set: Optional[ChangeSet] = None
    replay_dir: Optional[Path] = None
    run_id: str = ""
    measured_at: str = ""
    note: str = ""

    @classmethod
    def load(cls, path: Path) -> "ReviewFixture":
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FixtureError(f"Cannot read review fixture {path}: {error}") from error
        if payload.get("kind") != KIND_REVIEW:
            tree = TreeFixture.load(path)
            return cls(path=path, repo=tree.repo, adapter=tree.adapter, mode=MODE_TREE, head=tree)

        version = str(payload.get("fixture_version") or "")
        if version != REVIEW_FIXTURE_VERSION:
            raise FixtureError(f"{path} is review fixture version {version!r}, this Scout reads "
                               f"{REVIEW_FIXTURE_VERSION!r}")
        folder = path.parent
        change_set = None
        if payload.get("change_set"):
            try:
                text = (folder / payload["change_set"]).read_text(encoding="utf-8")
                change_set = ChangeSet.from_dict(json.loads(text))
            except (OSError, ValueError, KeyError) as error:
                raise FixtureError(f"{path} names change set {payload['change_set']!r}, which cannot be read: "
                                   f"{error}") from error
        head = TreeFixture.load(folder / payload["head_tree"])
        base = TreeFixture.load(folder / payload["base_tree"]) if payload.get("base_tree") else None
        if change_set is not None and head.rev != change_set.head_sha:
            raise FixtureError(f"{path}: the head tree is {head.rev[:9]} but the change set ends at "
                               f"{change_set.head_sha[:9]}")
        if base is not None and change_set is not None and base.rev != change_set.base_sha:
            raise FixtureError(f"{path}: the base tree is {base.rev[:9]} but the change set starts at "
                               f"{change_set.base_sha[:9]}")
        replay = payload.get("replay")
        return cls(
            path=path,
            repo=str(payload.get("repo") or head.repo),
            adapter=str(payload.get("adapter") or head.adapter),
            mode=str(payload.get("mode") or "range"),
            head=head,
            base=base,
            change_set=change_set,
            replay_dir=(folder / replay) if replay else None,
            run_id=str(payload.get("run_id") or ""),
            measured_at=str(payload.get("measured_at") or ""),
            note=str(payload.get("note") or ""),
        )

    def source(self) -> "PairedFixtureSource":
        return PairedFixtureSource(self.head, self.base)


class PairedFixtureSource(RepoSource):
    """The head and base tree fixtures behind one source, each answering for its own revision."""

    def __init__(self, head: TreeFixture, base: Optional[TreeFixture] = None) -> None:
        super().__init__()
        self._sides: Dict[str, FixtureSource] = {head.rev: FixtureSource(head)}
        if base is not None:
            self._sides[base.rev] = FixtureSource(base)
        self.head = head

    @property
    def describe(self) -> str:
        return f"pinned review fixture {self.head.repo}@{self.head.rev[:9]}"

    @property
    def blob_reads(self) -> int:
        return sum(side.blob_reads for side in self._sides.values())

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        raise FixtureError(f"{self.describe} is offline by construction and was asked to run `git {' '.join(args)}`")

    def rev_parse(self, revision: str) -> str:
        for rev in self._sides:
            if revision == rev or (len(revision) >= 7 and rev.startswith(revision)):
                return rev
        if revision in ("HEAD", "FETCH_HEAD"):
            return self.head.rev
        raise FixtureError(f"{self.describe} holds {sorted(self._sides)} and not {revision!r}")

    def list_tree(self, commit: str, prefix: str = "") -> List[TreeEntry]:
        return self._side(commit).list_tree(commit, prefix)

    def path_count(self, commit: str, prefix: str = "") -> int:
        return self._side(commit).path_count(commit, prefix)

    def read_file(self, commit: str, path: str) -> str:
        return self._side(commit).read_file(commit, path)

    def _side(self, commit: str) -> FixtureSource:
        side = self._sides.get(commit)
        if side is None:
            raise FixtureError(f"{self.describe} was asked for {commit[:9]}, which it does not hold; capture "
                               f"that side with tests/fixtures/capture_tree.py and name it in the manifest")
        return side


def write_manifest(directory: Path, change_set: Optional[ChangeSet], repo: str, adapter: str, mode: str,
                   run_id: str, measured_at: str, note: str = "", base_tree: bool = False,
                   replay: bool = True) -> Path:
    """Write `review.json`, and `changeset.json` beside it, for trees already captured there."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if change_set is not None:
        (directory / CHANGE_SET_FILE).write_text(json.dumps(change_set.to_dict(), indent=1, sort_keys=True) + "\n",
                                                 encoding="utf-8")
    manifest: Dict[str, Any] = {
        "fixture_version": REVIEW_FIXTURE_VERSION,
        "kind": KIND_REVIEW,
        "repo": repo,
        "adapter": adapter,
        "mode": mode,
        "run_id": run_id,
        "measured_at": measured_at,
        "note": note,
        "change_set": CHANGE_SET_FILE if change_set is not None else "",
        "head_tree": HEAD_TREE_FILE,
        "base_tree": BASE_TREE_FILE if base_tree else "",
        "replay": REPLAY_DIR if replay else "",
    }
    path = directory / MANIFEST
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path
