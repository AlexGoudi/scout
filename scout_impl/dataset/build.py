"""Mine the first-parent history of a snapshot into a dataset directory.

Records come from the same ``extract_records`` stream as single-commit mining, in chunks
spread over a process pool, and are cached per commit under a directory named for
everything else a record depends on: extractor version, taxonomy, redaction universe,
author salt, patch limits and repository URL.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ..mining import EXTRACTOR_VERSION
from ..mining.gitio import Git, GitError
from ..mining.message import DEFAULT_AUTHOR_SALT
from ..mining.record import Context, extract_records, make_context, record_to_json
from ..mining.taxonomy import Taxonomy
from .categories import Category, categorize
from .export import export_dataset, intrinsic_columns, llm_document
from .history import CommitFacts, facts_from_record, history_features
from .labels import LABELS_VERSION, BlameCache, commit_labels, link_reverts
from .labels import szz as run_szz
from .shards import ShardWriter, dumps, gzip_bytes, write_bytes_atomic, write_shards
from .split import assign_splits, read_exclusions

CHUNK_SIZE = 100
SHORT_SHA = 9
FINGERPRINT_FILE = ".scout-dataset-fingerprint"
Progress = Callable[[str], None]


def build_fingerprint(
    snapshot: str,
    context: Context,
    exclude: str | Path | None,
    szz: bool,
    repo_type: str,
) -> str:
    material = "\x00".join(
        (
            EXTRACTOR_VERSION,
            LABELS_VERSION,
            context.taxonomy.sha256,
            snapshot,
            str(exclude or ""),
            "szz" if szz else "no-szz",
            repo_type,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class RecordCache:
    """Compact record JSON keyed by commit sha; ``root=None`` disables caching."""

    def __init__(self, root: str | Path | None, context: Context) -> None:
        material = "\x00".join(
            (
                EXTRACTOR_VERSION,
                context.taxonomy.sha256,
                context.redactor.digest,
                hashlib.sha256(context.salt.encode("utf-8")).hexdigest(),
                repr(context.limits),
                context.repo or "",
            )
        )
        self.key = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        self.directory = Path(root) / "records" / self.key if root else None

    def _path(self, sha: str) -> Path:
        assert self.directory is not None
        return self.directory / sha[:2] / f"{sha}.json.gz"

    def has(self, sha: str) -> bool:
        return self.directory is not None and self._path(sha).is_file()

    def get(self, sha: str) -> str | None:
        if self.directory is None:
            return None
        try:
            return gzip.decompress(self._path(sha).read_bytes()).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            return None

    def put(self, sha: str, text: str) -> None:
        if self.directory is not None:
            write_bytes_atomic(self._path(sha), gzip_bytes(text.encode("utf-8")))


_WORKER: tuple[Git, Context] | None = None


def _init_worker(repository: str, names_revision: str, salt: str) -> None:
    global _WORKER
    git = Git(repository)
    _WORKER = (git, make_context(git, names_revision, salt=salt))


def _mine_chunk(shas: Sequence[str]) -> list[tuple[str, str]]:
    assert _WORKER is not None, "worker not initialized"
    git, context = _WORKER
    return [(record.commit.sha, record_to_json(record)) for record in extract_records(git, shas, context)]


def mine_records(
    git: Git,
    shas: Sequence[str],
    context: Context,
    *,
    names_revision: str,
    cache: RecordCache,
    workers: int = 1,
    progress: Progress | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> Iterator[str]:
    """Compact record JSON for ``shas`` in the order given, mining whatever the cache lacks."""
    say = progress or (lambda message: None)
    missing = [sha for sha in shas if not cache.has(sha)]
    chunks = [missing[start:start + chunk_size] for start in range(0, len(missing), chunk_size)]
    say(f"records: {len(shas)} commits, {len(shas) - len(missing)} cached, {len(missing)} to mine")
    missing_set = set(missing)
    executor = None
    if workers > 1 and len(chunks) > 1:
        executor = ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(str(git.repository), names_revision, context.salt),
        )
        results: Iterator[list[tuple[str, str]]] = executor.map(_mine_chunk, chunks)
    else:
        results = (
            [(record.commit.sha, record_to_json(record)) for record in extract_records(git, chunk, context)]
            for chunk in chunks
        )
    try:
        buffer: dict[str, str] = {}
        for index, sha in enumerate(shas, 1):
            if sha in missing_set:
                while sha not in buffer:
                    buffer.update(next(results))
                text = buffer.pop(sha)
                cache.put(sha, text)
            else:
                text = cache.get(sha)
                if text is None:
                    text = record_to_json(next(iter(extract_records(git, [sha], context))))
                    cache.put(sha, text)
            if index % 1000 == 0:
                say(f"records: {index}/{len(shas)}")
            yield text
    finally:
        if executor is not None:
            executor.shutdown(cancel_futures=True)


def build_dataset(
    *,
    repo: str | Path,
    rev: str,
    out: str | Path | None = None,
    workers: int | None = None,
    exclude: str | Path | None = None,
    szz: bool = True,
    cache: str | Path | None = None,
    salt: str = DEFAULT_AUTHOR_SALT,
    taxonomy: Taxonomy | None = None,
    repo_type: str = "sonic-buildimage",
    refresh: bool = False,
    progress: Progress | None = None,
) -> dict:
    """Build the dataset for the first-parent history of ``rev``; returns the dataset card."""
    say = progress or (lambda message: None)
    started = time.monotonic()
    git = Git(repo)
    if git.is_shallow():
        raise GitError("the clone is shallow; labels and history features need the full history")
    snapshot = git.resolve_commit(rev)
    directory = Path(out) if out else Path("corpus") / f"{repo_type}-{snapshot[:SHORT_SHA]}"
    context = make_context(git, snapshot, taxonomy=taxonomy, salt=salt)
    fingerprint = build_fingerprint(snapshot, context, exclude, szz, repo_type)
    fp_path = directory / FINGERPRINT_FILE
    card_path = directory / "dataset_card.json"
    if (
        not refresh
        and card_path.is_file()
        and fp_path.is_file()
        and fp_path.read_text(encoding="utf-8").strip() == fingerprint
    ):
        return json.loads(card_path.read_text(encoding="utf-8"))
    shas = git.first_parent_shas(snapshot)
    record_cache = RecordCache(cache, context)
    texts = mine_records(
        git,
        shas,
        context,
        names_revision=snapshot,
        cache=record_cache,
        workers=workers or os.cpu_count() or 1,
        progress=say,
    )
    exclusions = read_exclusions(exclude)
    facts: list[CommitFacts] = []
    categories: list[Category] = []
    intrinsic: list[dict] = []
    documents = ShardWriter(directory / "llm", compress=False)

    def observe(stream: Iterator[str]) -> Iterator[str]:
        landed = 0
        for index, text in enumerate(stream):
            record = json.loads(text)
            category = categorize(record)
            landed = max(landed, record["commit"]["committed_epoch"])
            facts.append(facts_from_record(index, record, category, landed, context.taxonomy.szz_file_classes))
            categories.append(category)
            intrinsic.append(intrinsic_columns(record))
            documents.write(dumps(llm_document(record)))
            yield text

    record_names = write_shards(directory / "records", observe(texts))
    document_names = documents.close()
    say(f"records: {len(shas)} written to {len(record_names)} shards in {time.monotonic() - started:.1f}s")

    reverts, revert_stats = link_reverts(facts)
    say(f"labels: {revert_stats['reverts']} reverts, {len(reverts)} reverted commits linked")
    bugs, szz_stats = None, None
    if szz:
        szz_started = time.monotonic()
        bugs, szz_stats = run_szz(
            git, facts, cache=BlameCache(cache), workers=workers or os.cpu_count() or 1, progress=say
        )
        say(f"szz: {len(bugs)} bug-introducing commits in {time.monotonic() - szz_started:.1f}s")
    history = history_features(facts, {target: link.revert for target, link in reverts.items()})
    labels = commit_labels(facts, reverts, bugs)
    split = assign_splits(facts, exclusions)
    card = export_dataset(
        directory,
        provenance={
            "snapshot": snapshot,
            "repo": context.repo,
            "taxonomy_sha": context.taxonomy.sha256,
            "redaction_sha": context.redactor.digest,
            "author_salt": "default" if salt == DEFAULT_AUTHOR_SALT else "custom",
            "szz_file_classes": sorted(context.taxonomy.szz_file_classes),
        },
        component_ids=[rule.id for rule in context.taxonomy.components],
        feature_ids=[area.id for area in context.taxonomy.features],
        facts=facts,
        categories=categories,
        intrinsic=intrinsic,
        history=history,
        labels=labels,
        split=split,
        revert_stats=revert_stats,
        szz_stats=szz_stats,
        shards={"records": record_names, "llm": document_names},
    )
    fp_path.write_text(fingerprint + "\n", encoding="utf-8")
    say(f"dataset: {directory} in {time.monotonic() - started:.1f}s")
    return card
