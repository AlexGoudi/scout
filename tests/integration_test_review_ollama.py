"""Live reviews against the local model, and the recording of the demo cases they become.

**Not part of the offline suite.** The filename is outside the `unit_test_*.py` glob, and
every test skips unless `$SCOUT_OLLAMA_TESTS=1`, so the default run stays hermetic:

    SCOUT_OLLAMA_TESTS=1 python3 -m pytest tests/integration_test_review_ollama.py -q -s
    SCOUT_OLLAMA_TESTS=1 SCOUT_RECORD_DEMO=1 python3 -m pytest tests/integration_test_review_ollama.py -q -s

`$SCOUT_OLLAMA_URL` names the server (default `http://127.0.0.1:11435`), and
`$SCOUT_BUILDIMAGE_REPO` the `sonic-buildimage` checkout holding the demo commits (default
the Nokia fork clone beside this directory). The checkout is only ever read.

Each case runs the whole review live and prints what it cost. With `SCOUT_RECORD_DEMO=1`
it is also pinned into `tests/fixtures/demo/<case>/`: the model's responses as the
recording provider wrote them, the change set, a tree fixture per side, the live run's
artifacts and its measurements. The fixture is then replayed with no network and no
model, and the replay must reproduce the live report exactly, apart from its timings, or
the recording is refused.

What gets captured is measured rather than guessed. The checkout is wrapped in a source
that records every blob the stages read, and those paths are captured alongside
`tests/fixtures/capture_tree.py`'s own selection, every symlink under `device/`, and every
path the agent consulted. If the static stage learns to read something new, the next
recording follows it.
"""

import difflib
import importlib.util
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

import pytest

from scout_impl.agent.prompts import AMBIGUITY_TASK
from scout_impl.cli import REVIEW_MAX_OUTPUT_TOKENS
from scout_impl.core.review import BRIEF_FILE, COMMENT_FILE, REPORT_FILE, RUN_LOG_FILE, review_fixture, run_review
from scout_impl.core.review_fixture import BASE_TREE_FILE, HEAD_TREE_FILE, MANIFEST, REPLAY_DIR, write_manifest
from scout_impl.ingest import resolve
from scout_impl.models import ChangeSetSpec, MODE_RANGE
from scout_impl.ollama import DEFAULT_MODEL, OLLAMA_URL_ENV, OllamaProvider, ollama_spec
from scout_impl.provider import Completion, Message, Provider, RecordingProvider, ReplayProvider, ToolSpec
from scout_impl.repos import get_adapter
from scout_impl.source import LocalCheckout, RepoSource
from scout_impl.static.engine import analyze, build_brief
from scout_impl.static.fixtures import TreeFixture

OLLAMA_TESTS_ENV = "SCOUT_OLLAMA_TESTS"
RECORD_ENV = "SCOUT_RECORD_DEMO"
REPO_ENV = "SCOUT_BUILDIMAGE_REPO"
MODEL_ENV = "SCOUT_OLLAMA_MODEL"
DEFAULT_URL = "http://127.0.0.1:11435"
DEFAULT_REPO = Path(__file__).resolve().parents[2] / "buildimage" / "sonic-buildimage"
DEMO_DIR = Path(__file__).resolve().parent / "fixtures" / "demo"
CAPTURE = Path(__file__).resolve().parent / "fixtures" / "capture_tree.py"
REPO_NAME = "sonic-net/sonic-buildimage"
ADAPTER = "sonic-buildimage"
MEASURED_AT = "2026-09-25T00:00:00Z"
LIVE_DIR = "live"
MEASUREMENTS = "live-measurements.json"


@dataclass(frozen=True)
class DemoCase:
    name: str
    commit_range: str
    run_id: str
    note: str


CASES = (
    DemoCase(
        name="pmon-24811",
        commit_range="3589b565dff1229247826b9094529cf676a89c41^..3589b565dff1229247826b9094529cf676a89c41",
        run_id="00000000-0000-4000-8000-000000024811",
        note=("sonic-net/sonic-buildimage#24811, 3589b565df: a shared Arista pmon_daemon_control.json that platforms "
              "reach through inbound symlinks. Same SHA upstream; captured from the Nokia fork clone's history."),
    ),
    DemoCase(
        name="prestera-20860",
        commit_range="d93686ede1f4a495c5e1b3002d00af8dd1c44531^..d93686ede1f4a495c5e1b3002d00af8dd1c44531",
        run_id="00000000-0000-4000-8000-000000020860",
        note=("sonic-net/sonic-buildimage#20860, d93686ede1: renames the family in the platform_asic of ten "
              "marvell-prestera platforms across amd64, arm64 and armhf, the ambiguous-architecture case."),
    ),
)

# Other real commits touching marvell-prestera platforms in all three architectures, put to
# the model only to measure how often it agrees with the rule candidate. Never recorded.
AGREEMENT_SWEEP = (
    ("888c9b34de^..888c9b34de", "[marvell-prestera] add Nokia support on trixie"),
    ("58c1c419c6^..58c1c419c6", "[Marvell] Update HWSKU (#21488)"),
    ("556d1f1361^..556d1f1361", "HWSKU for x86_64-marvell_db98cx8514_10cc-r0"),
)


class ReadRecordingSource(RepoSource):
    """A checkout that remembers every blob read through it, so a capture carries exactly those."""

    def __init__(self, inner: LocalCheckout) -> None:
        super().__init__()
        self.inner = inner
        self.reads: Dict[str, Set[str]] = {}

    @property
    def describe(self) -> str:
        return self.inner.describe

    def git(self, *args: str, check: bool = True, stdin: Optional[str] = None) -> str:
        return self.inner.git(*args, check=check, stdin=stdin)

    def read_file(self, commit: str, path: str) -> str:
        self.reads.setdefault(commit, set()).add(path)
        self._blob_reads += 1
        return self.inner.read_file(commit, path)


@pytest.fixture
def provider() -> OllamaProvider:
    if os.environ.get(OLLAMA_TESTS_ENV, "").strip().lower() not in ("1", "true", "yes"):
        pytest.skip(f"live reviews are opt-in: set ${OLLAMA_TESTS_ENV}=1 to call a real model "
                    f"(${OLLAMA_URL_ENV} names the server, default {DEFAULT_URL})")
    model = os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL
    live = OllamaProvider(ollama_spec(model, max_output_tokens=REVIEW_MAX_OUTPUT_TOKENS),
                          base_url=os.environ.get(OLLAMA_URL_ENV, "").strip() or DEFAULT_URL)
    live.preflight()
    return live


@pytest.fixture
def repo() -> Path:
    root = Path(os.environ.get(REPO_ENV, "").strip() or DEFAULT_REPO).expanduser()
    if not (root / ".git").exists():
        pytest.skip(f"no sonic-buildimage checkout at {root}; set ${REPO_ENV} to one holding the demo commits")
    return root


def _recording() -> bool:
    return os.environ.get(RECORD_ENV, "").strip().lower() in ("1", "true", "yes")


def _require_reverse_reach(repo: Path) -> None:
    """The gate the demo waits on: the static stage must see what 3589b565df reaches through its symlinks.

    Before reverse reach landed, that change reached no platform at all, and a demo recorded
    then would pin a brief with nothing in it. So nothing here runs until it reaches some.
    """
    source = LocalCheckout(repo)
    adapter = get_adapter(ADAPTER)
    change_set = resolve(ChangeSetSpec.from_range(CASES[0].commit_range, mode=MODE_RANGE), source, adapter)
    brief = build_brief(analyze(source, change_set.head_sha, adapter, change_set=change_set), repo=REPO_NAME,
                        base_sha=change_set.base_sha, head_sha=change_set.head_sha, mode=MODE_RANGE)
    affected = len(brief.coverage["affected"])
    if not affected:
        pytest.skip("the static stage reaches no platform from 3589b565df yet: reverse reach has not landed")
    print(f"\ngate: the static stage reaches {affected} platform(s) from 3589b565df, so the fix has landed")


def _review(repo: Path, commit_range: str, provider: Any, out: Path, run_id: str):
    source = ReadRecordingSource(LocalCheckout(repo))
    adapter = get_adapter(ADAPTER)
    change_set = resolve(ChangeSetSpec.from_range(commit_range, mode=MODE_RANGE), source, adapter)
    started = time.monotonic()
    result = run_review(source, repo=REPO_NAME, adapter=adapter, rev=change_set.head_sha, change_set=change_set,
                        provider=provider, output_dir=out, mode=MODE_RANGE, run_id=run_id, measured_at=MEASURED_AT)
    return result, time.monotonic() - started, source


def _measurements(result: Any, wall_s: float) -> Dict[str, Any]:
    run = result.report.payload["run"]
    questions = []
    for question in result.agent.questions:
        questions.append({
            "id": question.id, "kind": question.kind, "status": question.status, "reason": question.reason,
            "groups": len(question.assembly.groups) if question.assembly else 0,
            "evidence_items": len(question.assembly.book.items) if question.assembly else 0,
            "model_calls": len(question.calls), "tool_steps": question.tool_steps,
            "latency_s": round(question.latency_s, 2),
            "calls": [call.to_dict() for call in question.calls],
            "input_tokens": question.usage.input_tokens, "output_tokens": question.usage.output_tokens,
            "items": [{"group": item.group, "members": len(item.members), "outcome": item.outcome,
                       "check": item.check, "agrees_with_rule": item.agrees, "answer": item.answer,
                       "detail": item.detail} for item in question.items],
        })
    judged = [item for question in result.agent.questions for item in question.items if item.agrees is not None]
    return {
        "wall_clock_s": round(wall_s, 2),
        "static_duration_s": run["static_duration_s"],
        "agent_duration_s": run["agent_duration_s"],
        "status": run["status"],
        "model": run["model"]["id"],
        "questions_in_brief": run["questions_in_brief"],
        "questions_asked": run["questions_asked"],
        "questions_answered": run["questions_answered"],
        "model_calls": run["model_calls"],
        "tokens": run["cost"],
        "blobs_read": run["blobs_read"],
        "checks": run["checks"],
        "rule_agreement": {"groups_judged": len(judged), "agreed": sum(1 for item in judged if item.agrees),
                           "platforms_judged": sum(len(item.members) for item in judged),
                           "platforms_agreed": sum(len(item.members) for item in judged if item.agrees)},
        "questions": questions,
    }


def _print(case: str, result: Any, measured: Dict[str, Any]) -> None:
    coverage = result.brief.coverage
    print(f"\n===== {case}: {measured['status']} in {measured['wall_clock_s']}s wall "
          f"(static {measured['static_duration_s']}s, agent {measured['agent_duration_s']}s)")
    print(f"affected {len(coverage['affected'])}, covered {len(coverage['covered'])}, uncovered "
          f"{len(coverage['uncovered'])}, ambiguous {len(coverage['ambiguous'])}; questions "
          f"{measured['questions_asked']} of {measured['questions_in_brief']} put, {measured['questions_answered']} "
          f"answered; {measured['model_calls']} call(s), tokens {measured['tokens']}")
    for question in measured["questions"]:
        print(f"  {question['id']} {question['kind']}: {question['status']} in {question['latency_s']}s over "
              f"{question['model_calls']} call(s), {question['input_tokens']} in / {question['output_tokens']} out, "
              f"{question['groups']} group(s), {question['evidence_items']} evidence item(s)")
        for item in question["items"]:
            print(f"    {item['group'] or '-'} x{item['members']}: {item['outcome']} {item['check']} "
                  f"agrees={item['agrees_with_rule']} {json.dumps(item['answer'])} {item['detail']}")
    print(f"checks fired: {measured['checks']}; rule agreement: {measured['rule_agreement']}")
    print("----- scout-comment.md")
    print(result.comment)


def _symlinks(source: RepoSource, rev: str) -> List[str]:
    return [entry.path for entry in source.list_tree(rev, "device") if entry.mode == "120000"]


def _capture_tree() -> Any:
    spec = importlib.util.spec_from_file_location("capture_tree", CAPTURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(repo: Path, rev: str, out: Path, paths: Sequence[str], note: str) -> TreeFixture:
    """capture_tree.py's own selection, plus exact paths matched as a set rather than as globs.

    capture_tree.py matches every entry against every `--path-glob`, and `fnmatch` keeps only
    256 compiled patterns, so two thousand exact paths passed as globs recompile on nearly
    every match and one capture ran past fifteen minutes. Its selection is reused unchanged,
    imported rather than edited; only the extra paths are matched by membership.
    """
    capture = _capture_tree()
    source = LocalCheckout(repo)
    adapter = get_adapter(ADAPTER)
    entries = source.list_tree(rev)
    coverage_paths = capture._coverage_paths(source, rev, adapter)
    path_globs = sorted(set(capture._entity_globs(adapter) + list(capture.MARKER_GLOBS) + coverage_paths))
    blob_globs = sorted(set(capture._entity_blob_globs(adapter) + coverage_paths))
    extra = set(paths)
    kept = {entry.path: entry for entry in entries
            if entry.path in extra or capture._matching([entry.path], path_globs)}
    blobs: Dict[str, str] = {}
    for path in sorted(kept):
        entry = kept[path]
        if entry.is_file and entry.sha not in blobs and (path in extra or capture._matching([path], blob_globs)):
            blobs[entry.sha] = source.read_file(rev, path)
    fixture = TreeFixture(
        repo=REPO_NAME, adapter=adapter.name, rev=rev,
        rev_date=source.git("log", "-1", "--format=%cI", rev).strip(),
        captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        note=(f"{note} capture_tree.py's selection plus {len(extra)} exact path(s) the live run read, consulted "
              f"or found linked under device/."),
        tree_paths=len(entries), entries=tuple(sorted(kept.values(), key=lambda item: item.path)), blobs=blobs,
        path_globs=tuple(path_globs), blob_globs=tuple(blob_globs),
    )
    fixture.write(out)
    print(f"{out}: {len(fixture.entries)} of {fixture.tree_paths} entries, {len(blobs)} blobs, "
          f"{out.stat().st_size / 1024:.0f} KB")
    return fixture


def _pin(case: DemoCase, repo: Path, result: Any, source: ReadRecordingSource, replay: Path, live: Path,
         measured: Dict[str, Any]) -> Path:
    folder = DEMO_DIR / case.name
    folder.mkdir(parents=True, exist_ok=True)
    change_set = result.change_set
    head, base = change_set.head_sha, change_set.base_sha
    consulted = result.agent.consulted
    head_paths = set(source.reads.get(head, ())) | set(consulted.get("head", ())) | set(_symlinks(source, head))
    base_paths = set(source.reads.get(base, ())) | set(consulted.get("base", ()))
    _capture(repo, head, folder / HEAD_TREE_FILE, sorted(head_paths), case.note)
    if base_paths:
        _capture(repo, base, folder / BASE_TREE_FILE, sorted(base_paths), case.note)
    manifest = write_manifest(folder, change_set, repo=REPO_NAME, adapter=ADAPTER, mode=MODE_RANGE,
                              run_id=case.run_id, measured_at=MEASURED_AT, note=case.note, base_tree=bool(base_paths))
    if (folder / REPLAY_DIR).exists():
        shutil.rmtree(folder / REPLAY_DIR)
    shutil.copytree(replay, folder / REPLAY_DIR)
    if (folder / LIVE_DIR).exists():
        shutil.rmtree(folder / LIVE_DIR)
    shutil.copytree(live, folder / LIVE_DIR)
    (folder / MEASUREMENTS).write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _same(label: str, replayed: str, live: str) -> None:
    if replayed != live:
        diff = "\n".join(list(difflib.unified_diff(live.splitlines(), replayed.splitlines(), "live", "replayed",
                                                   lineterm=""))[:60])
        pytest.fail(f"the replay does not rebuild the {label}:\n{diff}")


def _assert_replay_reproduces(manifest: Path, live: Any, out: Path) -> None:
    replayed = review_fixture(manifest, provider=ReplayProvider.from_fixtures(manifest.parent / REPLAY_DIR),
                              output_dir=out)
    _same("brief", replayed.brief.canonical_json(), live.brief.canonical_json())
    _same("report", replayed.report.canonical_json(), live.report.canonical_json())
    _same("comment", replayed.comment, live.comment)
    assert replayed.agent.model_calls == live.agent.model_calls
    assert all(call.cached for question in replayed.agent.questions for call in question.calls)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_a_demo_case_reviewed_live(case: DemoCase, provider: OllamaProvider, repo: Path, tmp_path: Path) -> None:
    _require_reverse_reach(repo)
    replay = tmp_path / REPLAY_DIR
    live_out = tmp_path / LIVE_DIR
    result, wall, source = _review(repo, case.commit_range, RecordingProvider(provider, replay), live_out,
                                   case.run_id)
    measured = _measurements(result, wall)
    _print(case.name, result, measured)

    assert result.brief.payload["questions"], "a demo case must put at least one question to the model"
    assert result.agent.model_calls >= 1
    for name in (BRIEF_FILE, REPORT_FILE, COMMENT_FILE, RUN_LOG_FILE):
        assert (live_out / name).is_file()

    if _recording():
        manifest = _pin(case, repo, result, source, replay, live_out, measured)
        _assert_replay_reproduces(manifest, result, tmp_path / "replayed")
        print(f"pinned {manifest.parent} ({MANIFEST}), and its replay reproduces the live report")


@pytest.mark.parametrize("commit_range,subject", AGREEMENT_SWEEP, ids=[item[0][:10] for item in AGREEMENT_SWEEP])
def test_rule_agreement_sweep(commit_range: str, subject: str, provider: OllamaProvider, repo: Path,
                              tmp_path: Path) -> None:
    _require_reverse_reach(repo)
    result, wall, _ = _review(repo, commit_range, provider, tmp_path / "out", "sweep")
    measured = _measurements(result, wall)
    print(f"\n===== sweep {commit_range} ({subject})")
    _print(commit_range, result, measured)
    assert any(question.kind == "ambiguity" for question in result.agent.questions)


BLIND_TASK = ("For each group, decide from the rules and the evidence whether its platforms are built by a PR-CI "
              "job group for their own CPU architecture and, if they are, name the one job group that builds a "
              "family they declare for that architecture.")


class BlindingProvider(Provider):
    """Withholds the rule's answer from the ambiguity prompt, so the model decides and the checks judge it.

    An experiment, never a product path: the prompt keeps the rules and every job group but
    not the rule's answer, and the real loop's checks rule on what comes back.
    """

    def __init__(self, delegate: Provider) -> None:
        super().__init__(delegate.spec)
        self.delegate = delegate
        self.sent: List[str] = []

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        blinded = [Message(role=message.role, content=_blind(message.content)) if message.role == "user" else message
                   for message in messages]
        self.sent.extend(message.content for message in blinded if message.role == "user")
        return self.delegate.complete(blinded, tools, timeout_s)


def _blind(text: str) -> str:
    text = text.replace(AMBIGUITY_TASK, BLIND_TASK)
    text = re.sub(r"Rule answer: .*?\. (Its evidence)", r"\1", text)
    return re.sub(r"^(QUESTION q-\d+ \(rules [^)]*\)): .*$",
                  r"\1: Is each group below built by the PR pipeline for its CPU architecture?", text, flags=re.M)


@pytest.mark.parametrize("commit_range", [CASES[1].commit_range, *(item[0] for item in AGREEMENT_SWEEP)],
                         ids=["d93686ede1", *(item[0][:10] for item in AGREEMENT_SWEEP)])
def test_blind_probe(commit_range: str, provider: OllamaProvider, repo: Path, tmp_path: Path) -> None:
    _require_reverse_reach(repo)
    blinding = BlindingProvider(provider)
    result, wall, _ = _review(repo, commit_range, blinding, tmp_path / "out", "blind")
    measured = _measurements(result, wall)
    print(f"\n===== blind probe {commit_range}: the rule's answer withheld")
    _print(commit_range, result, measured)
    assert blinding.sent and not any("Rule answer" in text for text in blinding.sent)


def test_blinding_removes_the_rule_answer_and_nothing_else() -> None:
    text = ("QUESTION q-001 (rules BI-R3, BI-R4): The architecture rule has already decided...\n\n"
            f"{AMBIGUITY_TASK}\n\nGROUPS\n"
            "A: 5 amd64 platform(s) declaring marvell-prestera: x. Rule answer: not covered: no job group builds "
            "marvell-prestera for amd64. Its evidence: affected E5; cause E6, E7.")
    blinded = _blind(text)
    assert "Rule answer" not in blinded and AMBIGUITY_TASK not in blinded and BLIND_TASK in blinded
    assert blinded.endswith("x. Its evidence: affected E5; cause E6, E7.")
    assert "already decided" not in blinded
