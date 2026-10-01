"""One tree listing plus a content-addressed blob cache, shared by the whole stage.

Every analyzer in this package reads through a `TreeIndex` rather than through a
`RepoSource` directly, for two reasons. The listing is taken once and answered from
memory afterwards, so "which paths exist" stays the free operation `scout_impl/source.py`
promises it is. And reads are memoized on the blob sha rather than on the path, which
matters more here than it looks: a
`platform_asic` file holding `broadcom` is byte-identical across all 156 platforms that
declare it, so git stores one blob and Scout reads it once. Memoizing on the path instead
would pay 156 round trips for the same bytes.

`blob_reads` therefore counts round trips actually spent, which is the figure NFR-12 asks
for, and `cache_hits` counts the ones the sha memo avoided.
"""

from typing import Dict, Iterable, List, Optional

from ..source import RepoSource, TreeEntry

SYMLINK_MODE = "120000"


class TreeIndex:
    """The tree at one revision: paths, modes and blob shas, plus cached reads."""

    def __init__(self, source: RepoSource, rev: str, prefixes: Iterable[str] = ()) -> None:
        self.source = source
        self.rev = rev
        self._entries: Dict[str, TreeEntry] = {}
        for prefix in tuple(prefixes) or ("",):
            for entry in source.list_tree(rev, prefix):
                self._entries[entry.path] = entry
        self._blobs: Dict[str, str] = {}
        self._blob_reads = 0
        self._cache_hits = 0
        self._prefetches = 0
        self._total_paths = source.path_count(rev)

    @property
    def entries(self) -> Dict[str, TreeEntry]:
        return self._entries

    @property
    def paths(self) -> List[str]:
        return sorted(self._entries)

    @property
    def total_paths(self) -> int:
        """Paths in the whole tree, which a filtered listing does not enumerate."""
        return self._total_paths

    @property
    def blob_reads(self) -> int:
        return self._blob_reads

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    def entry(self, path: str) -> Optional[TreeEntry]:
        return self._entries.get(path)

    def exists(self, path: str) -> bool:
        return path in self._entries

    def is_symlink(self, path: str) -> bool:
        entry = self._entries.get(path)
        return entry is not None and entry.mode == SYMLINK_MODE

    def prefetch(self, paths: Iterable[str]) -> int:
        """Ask the source to bring these paths' blobs within reach in one round trip.

        Deduped on the blob sha first, and shas already in the read memo are dropped, so
        what reaches the source is the set of distinct bytes still missing rather than the
        list of paths that want them. Advisory: a source that cannot prefetch returns 0 and
        `read` then does what it always did, one trip at a time.
        """
        wanted = []
        for path in paths:
            entry = self._entries.get(path)
            if entry is not None and entry.sha not in self._blobs and entry.sha not in wanted:
                wanted.append(entry.sha)
        if not wanted:
            return 0

        self._prefetches += 1
        return self.source.prefetch_blobs(wanted)

    @property
    def prefetches(self) -> int:
        """Batched fetches issued. Reported so a regression to one-at-a-time is visible."""
        return self._prefetches

    def read(self, path: str) -> str:
        """Content of one path, charged once per distinct blob sha."""
        entry = self._entries.get(path)
        if entry is None:
            raise KeyError(f"{path} is not in the tree at {self.rev[:9]}")

        cached = self._blobs.get(entry.sha)
        if cached is not None:
            self._cache_hits += 1
            return cached

        content = self.source.read_file(self.rev, path)
        self._blobs[entry.sha] = content
        self._blob_reads += 1
        return content
