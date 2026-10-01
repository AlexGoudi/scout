"""The seed corpus: incidents and controls selected by rule rather than by hand.

No domain expert adjudicates relevance, so adjudication is replaced by the rules below,
and **every item is marked auto-selected, not human-adjudicated**, here and in the scorecard. Every step records what
it removed and why, so the counts can be audited from `candidates.jsonl` alone.

Incidents, in order:

1. every `^Revert` commit the miner finds on master;
2. auto-linked: the trailer names a commit, and that commit is on master's history. A
   partial clone fetches a named commit from whichever branch holds it, so "resolvable"
   is checked against master's commit list rather than against the object store;
3. not a nested revert, which is an un-revert rather than an incident;
4. relevant: the cause touched `device/` or `platform/`, or the revert's subject or body
   names a platform, HWSKU, vendor or ASIC family the cause's tree declares;
5. inside D6's domain: the cause's tree carries a pipeline the coverage model parses.
   Upstream grew the `Build` and `BuildVS` stages on 2022-03-23; before that there is no
   PR-CI coverage in the shape D6 computes, so an older incident measures the parser;
6. gradeable: the revert names at least one entity of that tree (`groundtruth.py`);
7. the most recent `incidents` by revert date, ties broken by sha.

Recency is blind to Scout's output, keeps the trees close to the pipeline Scout models, and
is the population Scout would actually meet. When the pool is smaller than asked for, all of
it is taken and the shortfall is recorded; the filter is never loosened to make up numbers.

Controls are master's own single-parent commits touching `device/` or `platform/` inside
the selected incidents' span, never named by any revert, at least `censor_days` old at the
history tip so that they had the incidents' chance to be reverted, and inside D6's domain.
They are taken in the order of `sha256("<salt>:<sha>")`, which is uniform, reproducible,
and fixed before any backtest runs.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..incidents import LINK_UNLINKED, LINK_UNRESOLVED, Incident
from ..repos import RepoAdapter, get_adapter
from ..static.coverage import query_coverage
from ..static.extract import build_coverage, build_index
from ..static.pipeline import PipelineParseError
from ..static.platforms import EntityIndexError
from ..static.treeindex import TreeIndex
from .groundtruth import (
    STATUS_AMBIGUOUS,
    STATUS_UNCOVERED,
    GroundTruth,
    Vocabulary,
    build_vocabulary,
    extract,
    landing,
    mentions,
)
from .history import History, changed_paths, ensure_blobs, first_parent_commits, master_commits, mine

logger = logging.getLogger(__name__)

CORPUS_VERSION = "1.0"
ADAPTER_NAME = "sonic-buildimage"
ADJUDICATION = "auto-selected, not human-adjudicated"

KIND_INCIDENT = "incident"
KIND_CONTROL = "control"

MANIFEST_FILE = "manifest.json"
INCIDENTS_FILE = "incidents.jsonl"
CONTROLS_FILE = "controls.jsonl"
CANDIDATES_FILE = "candidates.jsonl"
MARKDOWN_FILE = "corpus.md"

# What a tree survey reads. A prefetch hint, not a contract: anything missed is fetched lazily.
SURVEY_BLOBS = ("azure-pipelines.yml", ".azure-pipelines/*", "device/*/platform_asic")
PLATFORM_ROOTS = ("device/", "platform/")
SURVEY_BATCH = 40

STEP_UNLINKED = "unlinked"
STEP_UNRESOLVED = "unresolved"
STEP_NESTED = "nested"
STEP_IRRELEVANT = "irrelevant"
STEP_OUTSIDE_DOMAIN = "outside_d6_domain"
STEP_UNGRADEABLE = "ungradeable"
STEP_NOT_SELECTED = "pool_not_selected"
STEP_SELECTED = "selected"

_QUOTED_SUBJECT_RE = re.compile(r'^Revert\s+"(?P<subject>.+)"')
_REVERT_WORD_RE = re.compile(r"\brevert", re.IGNORECASE)
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z-]*-by:.*$", re.MULTILINE)
_EMAIL_RE = re.compile(r"\S+@\S+")
_URL_RE = re.compile(r"https?://\S+")
_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"


class CorpusError(ValueError):
    """The corpus could not be built, or a corpus directory could not be read."""


@dataclass(frozen=True)
class CorpusRules:
    """The selection rule's parameters; recorded in the manifest so the corpus can be rebuilt."""

    incidents: int = 24
    controls: int = 20
    control_salt: str = "scout-controls-v1"
    censor_days: int = 90

    def to_dict(self) -> Dict[str, Any]:
        return {
            "incidents": self.incidents,
            "controls": self.controls,
            "control_salt": self.control_salt,
            "censor_days": self.censor_days,
        }


@dataclass(frozen=True)
class TreeSurvey:
    """One tree as D6 sees it: its vocabulary, and whether its pipeline is one D6 can model."""

    rev: str
    vocabulary: Vocabulary
    analyzable: bool
    reason: str = ""
    job_groups: Tuple[str, ...] = ()

    @property
    def platforms_in_tree(self) -> int:
        return len(self.vocabulary.platforms)

    @property
    def never_built_in_tree(self) -> int:
        return len(self.vocabulary.never_built)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rev": self.rev,
            "analyzable": self.analyzable,
            "reason": self.reason or None,
            "platforms_in_tree": self.platforms_in_tree,
            "never_built_in_tree": self.never_built_in_tree if self.analyzable else None,
            "job_groups": list(self.job_groups),
        }


def survey_tree(source: Any, rev: str, adapter: RepoAdapter) -> TreeSurvey:
    """Index one tree and try its pipeline, separating "what can be named" from "can D6 run"."""
    tree = TreeIndex(source, rev)
    markers = tuple(getattr(adapter.entity_model, "hwsku_markers", ()) or ())
    try:
        index = build_index(tree, adapter.entity_model)
    except EntityIndexError as error:
        empty = Vocabulary(rev=rev, platforms={}, families=(), hwskus={}, shared={})
        return TreeSurvey(rev=rev, vocabulary=empty, analyzable=False, reason=f"{type(error).__name__}: {error}")

    try:
        model = build_coverage(tree, adapter.coverage_spec)
    except PipelineParseError as error:
        return TreeSurvey(
            rev=rev,
            vocabulary=build_vocabulary(index, tree.paths, markers),
            analyzable=False,
            reason=f"{type(error).__name__}: {str(error).splitlines()[0][:240]}",
        )

    coverage = query_coverage(index, model)
    return TreeSurvey(
        rev=rev,
        vocabulary=build_vocabulary(index, tree.paths, markers, model, coverage),
        analyzable=True,
        job_groups=tuple(model.names),
    )


def control_rank_key(salt: str, sha: str) -> str:
    return hashlib.sha256(f"{salt}:{sha}".encode("utf-8")).hexdigest()


def clean_body(body: str) -> str:
    """A commit body without the trailers, addresses and links that name vendors by accident."""
    text = _TRAILER_LINE_RE.sub("", body or "")
    return _URL_RE.sub("", _EMAIL_RE.sub("", text))


def base_rate_record(truth: GroundTruth, survey: TreeSurvey) -> Dict[str, Any]:
    """Where one revert landed relative to PR-CI coverage in its own cause's tree, under both readings."""
    landed = landing(truth, survey.vocabulary)
    status = survey.vocabulary.status
    uncovered = [item for item in landed if status.get(item) == STATUS_UNCOVERED]
    ambiguous = [item for item in landed if status.get(item) == STATUS_AMBIGUOUS]
    states = list(status.values())
    return {
        "landed": list(landed),
        "landed_uncovered": uncovered,
        "landed_ambiguous": ambiguous,
        "any_never_built": bool(uncovered or ambiguous),
        "any_uncovered": bool(uncovered),
        "share_never_built": round((len(uncovered) + len(ambiguous)) / len(landed), 6) if landed else None,
        "share_uncovered": round(len(uncovered) / len(landed), 6) if landed else None,
        "platforms_in_tree": survey.platforms_in_tree,
        "uncovered_in_tree": states.count(STATUS_UNCOVERED),
        "ambiguous_in_tree": states.count(STATUS_AMBIGUOUS),
    }


@dataclass
class Corpus:
    """A built or loaded corpus: its manifest, its items, and every candidate it considered."""

    manifest: Dict[str, Any]
    incidents: List[Dict[str, Any]] = field(default_factory=list)
    controls: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    directory: Optional[Path] = None

    @property
    def items(self) -> List[Dict[str, Any]]:
        return list(self.incidents) + list(self.controls)

    @property
    def pool(self) -> List[Dict[str, Any]]:
        """Every gradeable in-domain incident the selection drew from; the base-rate population."""
        return [item for item in self.candidates if item.get("step") in (STEP_SELECTED, STEP_NOT_SELECTED)]

    def fixture_path(self, item_id: str) -> Path:
        base = self.directory if self.directory is not None else Path(".")
        return base / "fixtures" / f"{item_id}.json"

    def write(self, directory: Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / MANIFEST_FILE).write_text(json.dumps(self.manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
        for name, rows in ((INCIDENTS_FILE, self.incidents), (CONTROLS_FILE, self.controls),
                           (CANDIDATES_FILE, self.candidates)):
            with (directory / name).open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        (directory / MARKDOWN_FILE).write_text(render_markdown(self), encoding="utf-8")
        self.directory = directory
        logger.info("Wrote %d incident(s) and %d control(s) to %s", len(self.incidents), len(self.controls),
                    directory)


def load_corpus(directory: Path) -> Corpus:
    directory = Path(directory)
    manifest_path = directory / MANIFEST_FILE
    if not manifest_path.is_file():
        raise CorpusError(f"{directory} holds no {MANIFEST_FILE}; build one with `run_scout.py corpus`")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("corpus_version")) != CORPUS_VERSION:
        raise CorpusError(f"{manifest_path} is corpus version {manifest.get('corpus_version')!r}, "
                          f"this Scout reads {CORPUS_VERSION!r}")

    def rows(name: str) -> List[Dict[str, Any]]:
        path = directory / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    return Corpus(
        manifest=manifest,
        incidents=rows(INCIDENTS_FILE),
        controls=rows(CONTROLS_FILE),
        candidates=rows(CANDIDATES_FILE),
        directory=directory,
    )


def build_corpus(history: History, rules: CorpusRules = CorpusRules(),
                 adapter: Optional[RepoAdapter] = None) -> Corpus:
    """Mine, filter, select and sample; everything recorded, nothing adjudicated by hand."""
    adapter = adapter or get_adapter(ADAPTER_NAME)
    mined = mine(history)
    on_master = master_commits(history)
    bodies = _bodies(history, [incident.revert_sha for incident in mined])

    candidates: List[Dict[str, Any]] = []
    eligible: List[Tuple[Incident, Dict[str, Any]]] = []
    for incident in mined:
        row = _candidate_row(incident)
        if incident.link_status == LINK_UNLINKED:
            row["step"] = STEP_UNLINKED
        elif incident.link_status == LINK_UNRESOLVED or incident.reverted_sha not in on_master:
            row["step"] = STEP_UNRESOLVED
            row["detail"] = "the trailer names a commit that is not on master's history"
        elif incident.is_nested_revert:
            row["step"] = STEP_NESTED
        else:
            eligible.append((incident, row))
        candidates.append(row)

    causes = sorted({incident.reverted_sha for incident, _ in eligible})
    fetched = ensure_blobs(history, causes, SURVEY_BLOBS)
    logger.info("Surveying %d cause tree(s); prefetched %d blob(s)", len(causes), fetched)
    source = history.tree_source()
    surveys = {sha: survey_tree(source, sha, adapter) for sha in causes}

    pool: List[Dict[str, Any]] = []
    for incident, row in eligible:
        survey = surveys[incident.reverted_sha]
        touched = sorted(path for path in incident.reverted_paths if path.startswith(PLATFORM_ROOTS))
        named = sorted(set(mentions(incident.revert_subject, survey.vocabulary))
                       | set(mentions(clean_body(bodies.get(incident.revert_sha, "")), survey.vocabulary)))
        row["relevance"] = {"cause_touched": touched[:20], "cause_touched_count": len(touched), "named": named}
        row["tree"] = survey.to_dict()
        if not touched and not named:
            row["step"] = STEP_IRRELEVANT
            continue
        if not survey.analyzable:
            row["step"] = STEP_OUTSIDE_DOMAIN
            row["detail"] = survey.reason
            continue
        truth = extract(incident.revert_subject, incident.revert_paths, survey.vocabulary)
        row["ground_truth"] = truth.to_dict()
        if truth.is_empty:
            row["step"] = STEP_UNGRADEABLE
            row["detail"] = "the revert names no platform, family or vendor this tree declares"
            continue
        row["base_rate"] = base_rate_record(truth, survey)
        row["step"] = STEP_NOT_SELECTED
        pool.append(row)

    pool.sort(key=lambda item: (-_timestamp(item["revert_committed_date"]), item["revert_sha"]))
    selected = pool[:rules.incidents]
    incidents = []
    for rank, row in enumerate(selected, start=1):
        row["step"] = STEP_SELECTED
        incidents.append(_incident_item(row, rank, len(pool), rules))

    tip_date = history.commit_date(history.revision)
    controls, control_stats = _sample_controls(history, adapter, rules, incidents, mined, tip_date)

    counts = _counts(mined, candidates, rules, len(incidents))
    manifest = {
        "corpus_version": CORPUS_VERSION,
        "adapter": adapter.name,
        "adjudication": ADJUDICATION,
        "history": history.to_dict(),
        "rules": rules.to_dict(),
        "selection": {
            "incidents": (
                f"The {rules.incidents} most recent reverts by committer date that are auto-linked to a cause "
                f"on master, not nested, relevant, inside D6's domain and gradeable; ties by revert sha."
            ),
            "controls": (
                f"First-parent single-parent commits touching device/ or platform/ inside the incidents' "
                f"cause-date span, at least {rules.censor_days} days older than the history tip, never named "
                f"by any revert, inside D6's domain, taken in ascending sha256('{rules.control_salt}:<sha>')."
            ),
            "control_window": control_stats["window"],
        },
        "counts": counts,
        "control_sampling": control_stats,
        "items": [item["item_id"] for item in incidents] + [item["item_id"] for item in controls],
    }
    return Corpus(manifest=manifest, incidents=incidents, controls=controls, candidates=candidates)


def _candidate_row(incident: Incident) -> Dict[str, Any]:
    return {
        "incident_id": incident.incident_id,
        "revert_sha": incident.revert_sha,
        "revert_subject": incident.revert_subject,
        "revert_committed_date": incident.revert_committed_date,
        "revert_paths": list(incident.revert_paths),
        "link_status": incident.link_status,
        "is_nested_revert": incident.is_nested_revert,
        "cause_sha": incident.reverted_sha,
        "cause_subject": incident.reverted_subject,
        "cause_committed_date": incident.reverted_committed_date,
        "cause_paths": list(incident.reverted_paths),
        "lead_time_days": incident.lead_time_days,
        "pr_number": incident.pr_number,
        "step": None,
    }


def _incident_item(row: Dict[str, Any], rank: int, pool_size: int, rules: CorpusRules) -> Dict[str, Any]:
    truth = GroundTruth.from_dict(row["ground_truth"])
    relevance = row["relevance"]
    why = []
    if relevance["cause_touched_count"]:
        why.append(f"the cause touched {relevance['cause_touched_count']} path(s) under device/ or platform/ "
                   f"(first: {relevance['cause_touched'][0]})")
    if relevance["named"]:
        why.append(f"the revert names {', '.join(relevance['named'][:6])}")
    graded = {"platform": truth.platforms, "family": truth.families, "vendor": truth.vendors}[truth.level]
    rationale = (
        f"Rank {rank} of {pool_size} in the eligible pool by revert date, under the rule 'the {rules.incidents} most "
        f"recent'. Relevant because {' and '.join(why)}. Graded at {truth.level} level on "
        f"{', '.join(graded[:6])}{' and more' if len(graded) > 6 else ''}. {ADJUDICATION.capitalize()}."
    )
    return {
        "item_id": row["incident_id"],
        "kind": KIND_INCIDENT,
        "adjudication": ADJUDICATION,
        "selection": {"rule": "most-recent", "rank": rank, "pool": pool_size},
        "rationale": rationale,
        "cause_sha": row["cause_sha"],
        "cause_subject": row["cause_subject"],
        "cause_committed_date": row["cause_committed_date"],
        "cause_paths": row["cause_paths"],
        "revert_sha": row["revert_sha"],
        "revert_subject": row["revert_subject"],
        "revert_committed_date": row["revert_committed_date"],
        "revert_paths": row["revert_paths"],
        "lead_time_days": row["lead_time_days"],
        "pr_number": row["pr_number"],
        "relevance": relevance,
        "ground_truth": row["ground_truth"],
        "base_rate": row["base_rate"],
        "tree": row["tree"],
    }


def _sample_controls(
    history: History,
    adapter: RepoAdapter,
    rules: CorpusRules,
    incidents: Sequence[Dict[str, Any]],
    mined: Sequence[Incident],
    tip_date: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not incidents or rules.controls <= 0:
        return [], {"window": None, "examined": 0}

    starts = sorted(_timestamp(item["cause_committed_date"]) for item in incidents)
    censor = datetime.fromisoformat(tip_date) - timedelta(days=rules.censor_days)
    start = datetime.fromtimestamp(starts[0], tz=censor.tzinfo)
    end = min(datetime.fromtimestamp(starts[-1], tz=censor.tzinfo), censor)
    window = {"since": start.isoformat(), "until": end.isoformat(), "censor_days": rules.censor_days,
              "history_tip_date": tip_date}

    reverted = {sha for incident in mined for sha in [incident.reverted_sha, *incident.additional_reverted_shas] if sha}
    # A linked revert names its cause by sha. An unlinked one only quotes its subject, and
    # GitHub truncates long ones with an ellipsis, so a prefix match is the best available:
    # it can only over-exclude, which keeps "never reverted" true.
    quoted = [match.group("subject") for incident in mined if incident.link_status == LINK_UNLINKED
              for match in [_QUOTED_SUBJECT_RE.match(incident.revert_subject)] if match]

    commits = first_parent_commits(history, since=start.isoformat(), until=end.isoformat())
    screened: Dict[str, int] = {"merge": 0, "revert_itself": 0, "named_by_revert_trailer": 0,
                                "quoted_by_unlinked_revert": 0}
    candidates = []
    for commit in commits:
        if len(commit["parents"]) != 1:
            screened["merge"] += 1
        elif _REVERT_WORD_RE.search(commit["subject"]):
            screened["revert_itself"] += 1
        elif commit["sha"] in reverted:
            screened["named_by_revert_trailer"] += 1
        elif _quoted_by(commit["subject"], quoted):
            screened["quoted_by_unlinked_revert"] += 1
        else:
            candidates.append(commit)

    paths = changed_paths(history, [commit["sha"] for commit in candidates])
    pool = [dict(commit, paths=paths.get(commit["sha"], [])) for commit in candidates
            if any(path.startswith(PLATFORM_ROOTS) for path in paths.get(commit["sha"], []))]
    for commit in pool:
        commit["rank_key"] = control_rank_key(rules.control_salt, commit["sha"])
    pool.sort(key=lambda commit: commit["rank_key"])

    source = history.tree_source()
    controls: List[Dict[str, Any]] = []
    rejected: List[Dict[str, str]] = []
    examined = 0
    for offset in range(0, len(pool), SURVEY_BATCH):
        if len(controls) >= rules.controls:
            break
        batch = pool[offset:offset + SURVEY_BATCH]
        ensure_blobs(history, [commit["sha"] for commit in batch], SURVEY_BLOBS)
        for commit in batch:
            if len(controls) >= rules.controls:
                break
            examined += 1
            survey = survey_tree(source, commit["sha"], adapter)
            if not survey.analyzable:
                rejected.append({"sha": commit["sha"], "reason": survey.reason})
                continue
            controls.append(_control_item(commit, survey, len(controls) + 1, examined, len(pool), rules))

    stats = {
        "window": window,
        "first_parent_commits_in_window": len(commits),
        "screened_out": screened,
        "not_touching_device_or_platform": len(candidates) - len(pool),
        "pool": len(pool),
        "examined_in_hash_order": examined,
        "rejected_outside_d6_domain": rejected,
        "selected": len(controls),
        "shortfall": max(0, rules.controls - len(controls)),
    }
    return controls, stats


def _quoted_by(subject: str, quoted: Sequence[str]) -> bool:
    for text in quoted:
        if text == subject or (text.endswith("…") and subject.startswith(text[:-1])):
            return True
    return False


def _control_item(commit: Dict[str, Any], survey: TreeSurvey, number: int, position: int, pool_size: int,
                  rules: CorpusRules) -> Dict[str, Any]:
    touched = sorted(path for path in commit["paths"] if path.startswith(PLATFORM_ROOTS))
    rationale = (
        f"Position {position} of {pool_size} in ascending sha256('{rules.control_salt}:<sha>') "
        f"(key {commit['rank_key'][:12]}) among never-reverted commits touching device/ or platform/ in the "
        f"window. Touches {len(touched)} such path(s), first {touched[0]}. {ADJUDICATION.capitalize()}."
    )
    return {
        "item_id": f"control-{number:02d}",
        "kind": KIND_CONTROL,
        "adjudication": ADJUDICATION,
        "selection": {"rule": "sha256-order", "salt": rules.control_salt, "key": commit["rank_key"],
                      "position": position, "pool": pool_size},
        "rationale": rationale,
        "cause_sha": commit["sha"],
        "cause_subject": commit["subject"],
        "cause_committed_date": commit["committed_date"],
        "cause_paths": list(commit["paths"]),
        "relevance": {"cause_touched": touched[:20], "cause_touched_count": len(touched), "named": []},
        "tree": survey.to_dict(),
    }


def _counts(mined: Sequence[Incident], candidates: Sequence[Dict[str, Any]], rules: CorpusRules,
            selected: int) -> Dict[str, Any]:
    steps: Dict[str, int] = {}
    for row in candidates:
        steps[row["step"]] = steps.get(row["step"], 0) + 1
    with_trailer = sum(1 for incident in mined if incident.link_status != LINK_UNLINKED)
    unresolved = steps.get(STEP_UNRESOLVED, 0)
    nested = steps.get(STEP_NESTED, 0)
    relevant = [row for row in candidates if row.get("relevance") and row["step"] != STEP_IRRELEVANT]
    pool = steps.get(STEP_SELECTED, 0) + steps.get(STEP_NOT_SELECTED, 0)
    return {
        "reverts_mined": len(mined),
        "with_trailer": with_trailer,
        "without_trailer": steps.get(STEP_UNLINKED, 0),
        "trailer_not_on_master": unresolved,
        "linked_on_master": with_trailer - unresolved,
        "nested_excluded": nested,
        "nested_total": sum(1 for incident in mined if incident.is_nested_revert),
        "eligible": with_trailer - unresolved - nested,
        "irrelevant": steps.get(STEP_IRRELEVANT, 0),
        "relevant": len(relevant),
        "relevant_by_path": sum(1 for row in relevant if row["relevance"]["cause_touched_count"]),
        "relevant_by_name_only": sum(1 for row in relevant if not row["relevance"]["cause_touched_count"]),
        "outside_d6_domain": steps.get(STEP_OUTSIDE_DOMAIN, 0),
        "ungradeable": steps.get(STEP_UNGRADEABLE, 0),
        "pool": pool,
        "requested": rules.incidents,
        "selected": selected,
        "shortfall": max(0, rules.incidents - pool),
    }


def _bodies(history: History, shas: Sequence[str]) -> Dict[str, str]:
    if not shas:
        return {}
    output = history.repo.run("log", "--no-walk", "--stdin", f"--format={_RECORD_SEPARATOR}%H{_FIELD_SEPARATOR}%B",
                              stdin="\n".join(shas) + "\n")
    bodies = {}
    for record in output.split(_RECORD_SEPARATOR):
        if record.strip():
            sha, _, body = record.partition(_FIELD_SEPARATOR)
            bodies[sha.strip()] = body
    return bodies


def _timestamp(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def render_markdown(corpus: Corpus) -> str:
    """The corpus as a reviewer reads it: the counts, the rule, and every item with its reason."""
    manifest = corpus.manifest
    counts = manifest["counts"]
    history = manifest["history"]
    lines = [
        "# Scout seed corpus",
        "",
        f"**Every item is {ADJUDICATION}.** Mined from `{history['location']}` at `{history['revision'][:12]}` "
        f"({history['revision_date']}), source `{history['source']}`.",
        "",
        "## Filtering, step by step",
        "",
        "| Step | Count |",
        "| --- | --- |",
        f"| `^Revert` commits mined on master | {counts['reverts_mined']} |",
        f"| Carrying a `This reverts commit` trailer (auto-linkable) | {counts['with_trailer']} |",
        f"| ... naming a commit not on master's history (excluded) | {counts['trailer_not_on_master']} |",
        f"| ... nested reverts, un-reverts rather than incidents (excluded) | {counts['nested_excluded']} |",
        f"| Eligible linked incidents | {counts['eligible']} |",
        f"| Relevant (cause touched device/ or platform/, or a platform, vendor or family is named) | "
        f"{counts['relevant']} ({counts['relevant_by_path']} by path, "
        f"{counts['relevant_by_name_only']} by name only) |",
        f"| ... outside D6's domain: the cause's pipeline predates the coverage model (excluded) | "
        f"{counts['outside_d6_domain']} |",
        f"| ... ungradeable: the revert names nothing the tree declares (excluded) | {counts['ungradeable']} |",
        f"| Pool | {counts['pool']} |",
        f"| Selected ({manifest['selection']['incidents']}) | {counts['selected']} of {counts['requested']} "
        f"requested; shortfall {counts['shortfall']} |",
        "",
        f"Controls: {manifest['selection']['controls']} Window {manifest['selection']['control_window']}.",
        "",
        "## Incidents",
        "",
        "| Item | Reverted | Lead (days) | Revert subject | Ground truth | Rationale |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in corpus.incidents:
        truth = GroundTruth.from_dict(item["ground_truth"])
        lines.append(
            f"| {item['item_id']} | {item['revert_committed_date'][:10]} | {item['lead_time_days']} | "
            f"{_cell(item['revert_subject'])} | {_cell(', '.join(truth.entity_ids[:5]))} | {_cell(item['rationale'])} |"
        )
    lines += ["", "## Controls", "", "| Item | Merged | Subject | Rationale |", "| --- | --- | --- | --- |"]
    for item in corpus.controls:
        lines.append(f"| {item['item_id']} | {item['cause_committed_date'][:10]} | {_cell(item['cause_subject'])} | "
                     f"{_cell(item['rationale'])} |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")
