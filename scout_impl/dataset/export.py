"""Dataset exports: the feature table, annotations, label-free LLM documents and the card."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..mining import EXTRACTOR_VERSION
from ..mining.diff import PatchLimits
from ..mining.record import PATCH_MAX_BYTES_PER_COMMIT, SCHEMA_ID, SCHEMA_VERSION, utc
from .categories import PRECEDENCE, SZZ_CLASSES, Category
from .history import HISTORY_FEATURES, CommitFacts
from .labels import BLAME_VERSION, REVERT_WINDOWS_DAYS, SZZ_MAX_DELETED_LINES
from .shards import dumps, write_bytes_atomic, write_shards, write_text_atomic
from .split import FRACTIONS, GAP_DAYS, LABELS, SPLITS, Split, split_labels

LLM_DOC_VERSION = "2"
LLM_BUDGET = 12_000
SECTION_BUDGET = 1_500
MESSAGE_BUDGET = 3_000
FILE_LIST_MAX = 60
SCOPES_IN_DOCUMENT = 3
ENTITY_LIST_MAX = 20
TRUNCATION_MARK = "\n[truncated]"
SECTION_TITLES = (("why", "Why"), ("how", "How"), ("verify", "How it was verified"))

LABEL_DEFINITIONS = {
    "reverted": "A later first-parent commit reverts this one, linked by its 'This reverts commit' trailer, "
    "or failing that by the PR number in its subject or body.",
    **{
        f"reverted_within_{days}d": f"Reverted, and the revert landed within {days} days."
        for days in REVERT_WINDOWS_DAYS
    },
    "bug_introducing": "SZZ: git blame -w -M, at the fix's first parent, of the lines a fix-like commit removed or "
    "changed in code, config, build, YANG or patch files attributes at least one non-trivial line to this commit.",
    "null": "Labels are null for merges, and bug_introducing is also null for commits that only move gitlinks "
    "and when SZZ is skipped.",
    "split_aware": "features.parquet holds labels as known when each split ends: train at the validation "
    "boundary, validation at the test boundary, test at the snapshot. annotations/ holds them as of the snapshot.",
}


def intrinsic_columns(record: Mapping[str, Any]) -> dict[str, Any]:
    """Columns of the feature table that come from the record alone."""
    commit = record["commit"]
    columns: dict[str, Any] = {
        "committed_at": commit["committed_at"],
        "is_merge": commit["is_merge"],
        "author_is_bot": commit["author_is_bot"],
    }
    columns.update({name: record["features"][name] for name in sorted(record["features"])})
    columns.update({f"component__{item['id']}": 1 for item in record["areas"]["components"]})
    columns.update({f"area__{item['id']}": 1 for item in record["areas"]["features"]})
    return columns


def llm_document(record: Mapping[str, Any], budget: int = LLM_BUDGET) -> dict[str, Any]:
    """A plain-text view of one commit for a language model; it carries no outcome labels."""
    commit = record["commit"]
    message = record["message"]
    areas = record["areas"]
    files = record["files"]
    lines = [f"Commit {commit['sha']} landed {commit['committed_at']}", f"Subject: {message['subject']}"]
    if message["subject_tags"]:
        lines.append("Tags: " + ", ".join(message["subject_tags"]))
    if message["pr_number"] is not None:
        lines.append(f"PR: #{message['pr_number']}")
    revert = message["revert"]
    if revert["is_revert"]:
        targets = [f"commit {revert['reverts_sha'][:12]}" if revert["reverts_sha"] else "",
                   f"PR #{revert['reverts_pr']}" if revert["reverts_pr"] is not None else ""]
        lines.append("Reverts: " + (", ".join(item for item in targets if item) or "unspecified"))
    for module in record["submodules"]:
        old = (module["old_sha"] or "added")[:12]
        new = (module["new_sha"] or "removed")[:12]
        lines.append(f"Submodule {module['name']}: {old} -> {new}")
    lines.append("Components: " + (", ".join(item["id"] for item in areas["components"]) or "none"))
    lines.append("Feature areas: " + (", ".join(item["id"] for item in areas["features"]) or "none"))
    entities = [item["id"] for item in areas["entities"]]
    more = f" and {len(entities) - ENTITY_LIST_MAX} more" if len(entities) > ENTITY_LIST_MAX else ""
    lines.append("Entities: " + (", ".join(entities[:ENTITY_LIST_MAX]) + more if entities else "none"))

    sections = [(title, message["sections"][name]) for name, title in SECTION_TITLES if message["sections"][name]]
    if sections:
        for title, text in sections:
            lines += ["", f"{title}:", _clip(text, SECTION_BUDGET)]
    elif message["body"]:
        lines += ["", "Message:", _clip(message["body"], MESSAGE_BUDGET)]

    additions = sum(item["additions"] or 0 for item in files)
    deletions = sum(item["deletions"] or 0 for item in files)
    lines += ["", f"Files ({len(files)}, +{additions} -{deletions}):"]
    for item in files[:FILE_LIST_MAX]:
        path = item["new_path"] or item["old_path"]
        moved = f" (from {item['old_path']})" if item["status"] in ("R", "C") else ""
        size = "binary" if item["binary"] else f"+{item['additions'] or 0} -{item['deletions'] or 0}"
        scopes = item["scopes"][:SCOPES_IN_DOCUMENT]
        if len(item["scopes"]) > SCOPES_IN_DOCUMENT:
            scopes.append(f"+{len(item['scopes']) - SCOPES_IN_DOCUMENT} more")
        where = " in: " + " | ".join(scopes) if scopes else ""
        lines.append(f"{item['status']} {item['file_class']:<9} {path}{moved} {size}{where}")
    if len(files) > FILE_LIST_MAX:
        lines.append(f"... and {len(files) - FILE_LIST_MAX} more files")

    text = "\n".join(lines)
    complete = not any(item["patch_truncated"] for item in files)
    patches = [(item["new_path"] or item["old_path"], item["patch"]) for item in files if item["patch"]]
    if patches:
        diff = "\n\nDiff (zero context):"
        if len(text) + len(diff) < budget - len(TRUNCATION_MARK):
            text += diff
            for path, patch in patches:
                block = f"\n--- {path}\n{patch.rstrip()}"
                if len(text) + len(block) > budget - len(TRUNCATION_MARK):
                    complete = False
                    room = budget - len(TRUNCATION_MARK) - len(text)
                    if room > 200:
                        text += block[:room].rsplit("\n", 1)[0]
                    break
                text += block
        else:
            complete = False
    if len(text) > budget:
        text = text[: budget - len(TRUNCATION_MARK)]
        complete = False
    if not complete:
        text += TRUNCATION_MARK
    return {
        "sha": commit["sha"],
        "committed_at": commit["committed_at"],
        "doc_version": LLM_DOC_VERSION,
        "chars": len(text),
        "diff_complete": complete,
        "text": text,
    }


def feature_row(
    fact: CommitFacts,
    category: Category,
    columns: Mapping[str, Any],
    history: Mapping[str, Any],
    one_hot: Sequence[str],
    split: str,
) -> dict[str, Any]:
    """The label-free part of one ``features.parquet`` row."""
    row: dict[str, Any] = {"sha": fact.sha, "index": fact.index, "landed": fact.landed, "split": split}
    row["change_type"] = category.change_type
    row.update({f"type__{kind}": kind in category.change_types for kind in PRECEDENCE})
    row.update({key: value for key, value in columns.items() if "__" not in key})
    row.update({key: history[key] for key in HISTORY_FEATURES})
    row.update({key: int(key in columns) for key in one_hot})
    return row


def export_dataset(
    directory: Path,
    *,
    provenance: Mapping[str, Any],
    component_ids: Sequence[str],
    feature_ids: Sequence[str],
    facts: Sequence[CommitFacts],
    categories: Sequence[Category],
    intrinsic: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    split: Split,
    revert_stats: Mapping[str, int],
    szz_stats: Mapping[str, int] | None,
    shards: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Write annotations, ``features.parquet``, ``dataset_card.json`` and ``DATASET.md``."""
    annotation_names = write_shards(
        directory / "annotations",
        (
            dumps(
                {
                    "sha": fact.sha,
                    "index": fact.index,
                    "landed": fact.landed,
                    "split": split.assignment[fact.index],
                    "category": {
                        "change_type": category.change_type,
                        "change_types": list(category.change_types),
                        "evidence": list(category.evidence),
                    },
                    "history": row,
                    "labels": label,
                }
            )
            for fact, category, row, label in zip(facts, categories, history, labels)
        ),
    )

    one_hot = [f"component__{name}" for name in component_ids] + [f"area__{name}" for name in feature_ids]
    per_split: dict[str, dict[str, list[int]]] = {name: {label: [0, 0] for label in LABELS} for name in SPLITS}
    rows = []
    for fact, category, columns, features, label in zip(facts, categories, intrinsic, history, labels):
        name = split.assignment[fact.index]
        aware = split_labels(label, name, split.ends)
        if name in per_split:
            for key, value in aware.items():
                if value is not None:
                    per_split[name][key][0] += bool(value)
                    per_split[name][key][1] += 1
        row = feature_row(fact, category, columns, features, one_hot, name)
        row.update({f"label_{key}": value for key, value in aware.items()})
        row["label_revert_lead_days"] = label["revert_lead_days"] if aware["reverted"] else None
        row["label_fix_lead_days"] = label["fix_lead_days"] if aware["bug_introducing"] else None
        rows.append(row)
    _write_parquet(directory / "features.parquet", rows)

    card = _card(
        provenance=provenance,
        facts=facts,
        categories=categories,
        intrinsic=intrinsic,
        labels=labels,
        split=split,
        per_split=per_split,
        revert_stats=revert_stats,
        szz_stats=szz_stats,
        files={**{key: list(value) for key, value in shards.items()}, "annotations": annotation_names,
               "features": ["features.parquet"]},
        columns=list(rows[0]) if rows else [],
    )
    write_text_atomic(directory / "dataset_card.json", json.dumps(card, indent=2, sort_keys=True) + "\n")
    write_text_atomic(directory / "DATASET.md", render_dataset_md(card))
    return card


def _card(
    *,
    provenance: Mapping[str, Any],
    facts: Sequence[CommitFacts],
    categories: Sequence[Category],
    intrinsic: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    split: Split,
    per_split: Mapping[str, Mapping[str, list[int]]],
    revert_stats: Mapping[str, int],
    szz_stats: Mapping[str, int] | None,
    files: Mapping[str, list[str]],
    columns: list[str],
) -> dict[str, Any]:
    as_of_snapshot = {}
    for key in LABELS:
        known = [row[key] for row in labels if row[key] is not None]
        as_of_snapshot[key] = _rate(sum(bool(value) for value in known), len(known))
    paths = sum(row["file_count"] for row in intrinsic)
    unmapped = sum(row["unmapped_path_count"] for row in intrinsic)
    limits = PatchLimits()
    return {
        "card_version": "1",
        "snapshot": provenance["snapshot"],
        "repo": provenance["repo"],
        "record_schema": f"{SCHEMA_ID}/{SCHEMA_VERSION}",
        "extractor_version": EXTRACTOR_VERSION,
        "taxonomy_sha": provenance["taxonomy_sha"],
        "redaction_sha": provenance["redaction_sha"],
        "author_salt": provenance["author_salt"],
        "commits": {
            "total": len(facts),
            "merges": sum(fact.is_merge for fact in facts),
            "bot_authored": sum(bool(row["author_is_bot"]) for row in intrinsic),
            "first_landed": utc(facts[0].landed) if facts else None,
            "last_landed": utc(facts[-1].landed) if facts else None,
        },
        "splits": {
            "fractions": list(FRACTIONS),
            "gap_days": GAP_DAYS,
            "boundaries": {key: utc(value) for key, value in split.boundaries.items()},
            "counts": dict(sorted(Counter(split.assignment).items())),
            "exclude": {"matched": split.exclude_matched, "unmatched": list(split.exclude_unmatched)},
        },
        "labels": {
            "definitions": LABEL_DEFINITIONS,
            "reverts": dict(revert_stats),
            "szz": dict(szz_stats) if szz_stats is not None else None,
            "as_of_snapshot": as_of_snapshot,
            "per_split": {
                name: {key: _rate(*counts) for key, counts in values.items()} for name, values in per_split.items()
            },
        },
        "categories": dict(sorted(Counter(category.change_type for category in categories).items())),
        "coverage": {"file_paths": paths, "unmapped_paths": unmapped, "unmapped_share": _ratio(unmapped, paths)},
        "filters": {
            "history": "first-parent chain of the snapshot, diffed against each commit's first parent",
            "patch_max_lines_per_file": limits.max_lines_per_file,
            "patch_max_line_bytes": limits.max_line_bytes,
            "patch_max_bytes_per_commit": PATCH_MAX_BYTES_PER_COMMIT,
            "szz_file_classes": sorted(SZZ_CLASSES),
            "szz_max_deleted_lines": SZZ_MAX_DELETED_LINES,
            "szz_blame": f"git blame -w -M (cache v{BLAME_VERSION}); blank, bracket-only and comment lines ignored",
            "llm_char_budget": LLM_BUDGET,
        },
        "files": dict(files),
        "columns": columns,
    }


def render_dataset_md(card: Mapping[str, Any]) -> str:
    commits = card["commits"]
    splits = card["splits"]
    labels = card["labels"]
    lines = [
        f"# Commit dataset for {card['snapshot'][:12]}",
        "",
        f"Generated from `{card['repo'] or 'a local clone'}` at snapshot `{card['snapshot']}` with extractor "
        f"{card['extractor_version']} (`{card['record_schema']}`). Records carry no author names or email "
        "addresses; authors appear only as salted hashes.",
        "",
        f"- Commits: {commits['total']} ({commits['merges']} merges, {commits['bot_authored']} bot-authored), "
        f"landed {commits['first_landed']} to {commits['last_landed']}.",
        f"- Unmapped file paths: {card['coverage']['unmapped_paths']} of {card['coverage']['file_paths']} "
        f"({card['coverage']['unmapped_share']:.2%}).",
        f"- Reverts: {labels['reverts']['reverts']}, of which {labels['reverts']['linked_by_sha']} linked by sha and "
        f"{labels['reverts']['linked_by_pr']} by PR number; {labels['reverts']['unlinked']} unlinked.",
    ]
    if labels["szz"]:
        lines.append(
            f"- SZZ: {labels['szz']['fix_commits']} fix-like commits, {labels['szz']['blame_calls']} blame calls, "
            f"{labels['as_of_snapshot']['bug_introducing']['positive']} bug-introducing commits."
        )
    lines += [
        "",
        "## Split",
        "",
        f"Landing order, {' / '.join(f'{int(value * 100)}%' for value in splits['fractions'])}, with a "
        f"{splits['gap_days']}-day gap before each boundary and before the snapshot. Validation starts "
        f"{splits['boundaries']['validation_start']}, test starts {splits['boundaries']['test_start']}.",
        "",
        "| Split | Commits |",
        "| --- | --- |",
        *(f"| {name} | {count} |" for name, count in splits["counts"].items()),
        "",
        "## Label positive rates",
        "",
        "Counted over commits whose label is not null, as known when each split ends.",
        "",
        "| Label | " + " | ".join(labels["per_split"]) + " | as of snapshot |",
        "| --- |" + " --- |" * (len(labels["per_split"]) + 1),
    ]
    for key in LABELS:
        cells = [_cell(labels["per_split"][name][key]) for name in labels["per_split"]]
        lines.append(f"| {key} | " + " | ".join(cells) + f" | {_cell(labels['as_of_snapshot'][key])} |")
    lines += ["", "## Label definitions", ""]
    lines += [f"- `{key}`: {value}" for key, value in labels["definitions"].items()]
    lines += ["", "## Change types (rule-based, primary)", "", "| Type | Commits |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in card["categories"].items()]
    lines += ["", "## Files", ""]
    lines += [f"- `{key}/`: {len(value)} file(s)" for key, value in card["files"].items() if key != "features"]
    lines += ["- `features.parquet`: one row per commit; label columns start with `label_`.", ""]
    return "\n".join(lines)


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows)
    temporary = path.with_name(path.name + ".tmp")
    pq.write_table(table, temporary, compression="zstd")
    write_bytes_atomic(path, temporary.read_bytes())
    temporary.unlink()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _rate(positive: int, known: int) -> dict[str, Any]:
    return {"positive": positive, "known": known, "rate": _ratio(positive, known)}


def _cell(value: Mapping[str, Any]) -> str:
    return f"{value['rate']:.2%} ({value['positive']}/{value['known']})"
