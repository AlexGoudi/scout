import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from .agent.budget import DEFAULT_DEADLINE_S
from .core.review import ReviewResult, review_fixture, run_review
from .core.review_fixture import ReviewFixture
from .core.static_run import run_static, write_brief
from .eval.azure_dumps import join_cfg, run_azure
from .eval.calibration_rules import excluded_jobs
from .eval.backtest_run import BacktestError, run_backtest
from .eval.git_dumps import run_git_dumps
from .eval.incidents_cache import run_incidents_cache
from .eval.join_labels import run_labels
from .eval.paths import eval_calibration_dir
from .eval.score_pr import score_from_brief, score_pr_repo
from .gitcmd import GitError, GitRepo
from .incidents import DEFAULT_GREP, summarize, write_corpus
from .ingest import resolve
from .mining.taxonomy import TaxonomyError
from .mining_cli import MINING_GROUPS, main as mining_main
from .models import ChangeSetSpec, MODE_RANGE, MODE_SYNC
from .ollama import DEFAULT_MODEL, DEFAULT_TIMEOUT_S, OllamaProvider, ollama_spec
from .provider import Provider, ProviderError, RecordingProvider, ReplayProvider
from .remote import DEFAULT_DEPTH, MAX_DEPTH, RemoteRepo, default_cache_root
from .report.builder import ReportContractError
from .repos import RepoAdapter, RepoAdapterError, available_adapters, get_adapter, resolve_adapter
from .source import LocalCheckout, RepoSource
from .static.brief import BriefContractError
from .static.engine import MODE_TREE
from .static.fixtures import FixtureError, load_fixture
from .static.pipeline import PipelineParseError
from .static.platforms import EntityIndexError
from .static.schema import SchemaError, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_CORPUS_PATH = "scout-corpus.jsonl"
DEFAULT_BRIEF_PATH = "scout-brief.json"
DEFAULT_SCOUT_CACHE = Path(__file__).resolve().parents[1] / ".scout-cache"
# Part of every replay key, through `ModelSpec.max_output_tokens`, so recordings made at one
# value replay only at that value.
REVIEW_MAX_OUTPUT_TOKENS = 384


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SONiC Scout: advisory break-risk review of SONiC changes")
    parser.add_argument(
        "--repo-root",
        help="Working copy to read; mutually exclusive with --remote. Defaults to the current directory",
    )
    parser.add_argument(
        "--remote",
        help="Read a remote instead of a working copy: owner/repo, or a URL. Anonymous HTTPS, no clone",
    )
    parser.add_argument(
        "--repo-type",
        choices=available_adapters(),
        help="Which repository this is; identified from the tree when omitted",
    )
    parser.add_argument(
        "--cache-dir",
        help=f"Where fetched remotes are cached, keyed by remote, with mined eval data under eval/. "
        f"Defaults to {default_cache_root()}",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help="Initial fetch depth; deepens automatically when the change set needs more",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=MAX_DEPTH,
        help="Cap on deepening, so a fetch never degenerates into whole history",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Log progress only; do not print JSON summaries to stdout",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Resolve a change set and emit it as JSON")
    ingest_parser.add_argument(
        "--range",
        dest="commit_range",
        help="Commit range, BASE..HEAD or BASE...HEAD; mutually exclusive with --sync and --pr",
    )
    ingest_parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number on --remote; mutually exclusive with --range and --sync",
    )
    ingest_parser.add_argument(
        "--sync",
        nargs=2,
        metavar=("LOCAL_REF", "UPSTREAM_REF"),
        help="Fork sync delta: commits arriving from UPSTREAM_REF, plus the local patches they overlap",
    )
    ingest_parser.add_argument(
        "--context-lines",
        type=int,
        default=3,
        help="Diff context lines to retain per hunk",
    )
    ingest_parser.add_argument(
        "--include-merges",
        action="store_true",
        help="Keep merge commits; excluded by default because their diffs duplicate their parents",
    )
    ingest_parser.add_argument("--output", help="Write JSON here instead of stdout")

    tree_parser = subparsers.add_parser(
        "tree",
        help="List paths at a revision. Tree metadata only, so it transfers no file content",
    )
    tree_parser.add_argument("--rev", default="HEAD", help="Revision to list")
    tree_parser.add_argument("--pr", type=int, help="List the tree at this pull request's head instead of --rev")
    tree_parser.add_argument("--pattern", help="Keep only paths matching this glob; `*` spans directories")
    tree_parser.add_argument("--count", action="store_true", help="Print how many paths matched, not the paths")

    brief_parser = subparsers.add_parser(
        "brief",
        help="Run the deterministic static stage and emit scout-brief.json. No model, no credential",
    )
    brief_parser.add_argument("--pr", type=int, help="Pull request number on --remote")
    brief_parser.add_argument(
        "--range",
        dest="commit_range",
        help="Commit range, BASE..HEAD or BASE...HEAD; mutually exclusive with --pr",
    )
    brief_parser.add_argument(
        "--rev",
        help="Analyze this revision's whole tree with no change set, giving the tree-wide figures",
    )
    brief_parser.add_argument(
        "--fixture",
        help="Read a pinned tree fixture instead of a repository. Offline; ignores --remote and --repo-root",
    )
    brief_parser.add_argument("--output", default=DEFAULT_BRIEF_PATH, help="Where to write the brief")
    brief_parser.add_argument("--stdout", action="store_true", help="Print the brief instead of writing it")
    brief_parser.add_argument(
        "--hotspots",
        type=int,
        default=10,
        help="How many ranked hotspots to keep",
    )
    brief_parser.add_argument("--context-lines", type=int, default=3, help="Diff context lines to retain per hunk")

    review_parser = subparsers.add_parser(
        "review",
        help="Brief, agent and report end to end, writing all four artifacts. Advisory: exits 0 even when degraded",
    )
    review_parser.add_argument("--pr", type=int, help="Pull request number on --remote")
    review_parser.add_argument(
        "--range",
        dest="commit_range",
        help="Commit range, BASE..HEAD or BASE...HEAD; mutually exclusive with --pr and --fixture",
    )
    review_parser.add_argument(
        "--fixture",
        help="A pinned review fixture (review.json) or tree fixture. Offline; ignores --remote and --repo-root",
    )
    review_parser.add_argument(
        "--provider",
        choices=("ollama", "replay", "none"),
        default="ollama",
        help="Who answers the brief's questions. `none` runs degraded: the brief and the deterministic findings only",
    )
    review_parser.add_argument("--model", default=DEFAULT_MODEL, help="Model the ollama provider asks")
    review_parser.add_argument(
        "--ollama-url",
        help="The ollama server; defaults to $SCOUT_OLLAMA_URL, then $OLLAMA_HOST, then ollama's own default",
    )
    review_parser.add_argument(
        "--replay-dir",
        help="Recorded responses for --provider replay; defaults to the fixture's own replay directory",
    )
    review_parser.add_argument("--record", help="Also record every live response into this directory, for replay")
    review_parser.add_argument("--output-dir", default=".", help="Where the four artifacts are written")
    review_parser.add_argument(
        "--repo-name",
        help="Name the brief and report record for the repository; defaults to --remote or the checkout's name",
    )
    review_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds per model call (default: {DEFAULT_TIMEOUT_S:g}). Checked against --deadline only between calls",
    )
    review_parser.add_argument(
        "--deadline",
        type=float,
        default=DEFAULT_DEADLINE_S,
        help="Wall-clock seconds the agent stage may spend before the rest of the run degrades (NFR-2)",
    )
    review_parser.add_argument("--hotspots", type=int, default=10, help="How many ranked hotspots the brief keeps")
    review_parser.add_argument("--context-lines", type=int, default=3, help="Diff context lines to retain per hunk")

    miner_parser = subparsers.add_parser(
        "mine-incidents",
        help="Mine the revert history into the labelled seed corpus",
    )
    miner_parser.add_argument("--revision", default="HEAD", help="Revision to mine")
    miner_parser.add_argument("--grep", default=DEFAULT_GREP, help="Commit message pattern selecting reverts")
    miner_parser.add_argument("--limit", type=int, help="Only mine the most recent N matching commits")
    miner_parser.add_argument("--output", default=DEFAULT_CORPUS_PATH, help="JSONL corpus output path")
    miner_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-mine incidents from scratch instead of appending since the cached tip",
    )

    git_dumps_parser = subparsers.add_parser(
        "mine-git-dumps",
        help="Mine git log statistics into the calibration cache",
    )
    git_dumps_parser.add_argument("--revision", default="origin/master", help="Revision to walk")
    git_dumps_parser.add_argument("--output", help="Calibration directory override")
    git_dumps_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-mine all git-* files even when the cache for this ref/tip is complete",
    )

    azure_parser = subparsers.add_parser(
        "mine-azure",
        help="Fetch Azure PR build timelines into the calibration cache",
    )
    azure_parser.add_argument("--output", help="Calibration directory override")
    azure_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Concurrent Azure timeline HTTP fetches (default: SCOUT_AZURE_WORKERS or 6; use 1 for serial)",
    )
    azure_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch Azure from scratch (default: resume missing timelines only)",
    )

    labels_parser = subparsers.add_parser(
        "mine-labels",
        help="Join Azure timelines with git paths into model-* calibration files",
    )
    labels_parser.add_argument("--output", help="Calibration directory override")
    labels_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-run join even when model-* already exists in the calibration dir",
    )

    score_parser = subparsers.add_parser("score-pr", help="Heuristic break-risk score from changed paths")
    score_parser.add_argument("--base", default="origin/master", help="Diff base ref")
    score_parser.add_argument(
        "--brief",
        default=None,
        help="Score a scout-brief.json's change instead of BASE...HEAD: its base_sha...head_sha in the "
        "checkout, or only its hotspots when the checkout lacks those commits (files_complete: false)",
    )
    score_parser.add_argument(
        "--calibration",
        help="Directory with scoring-plan.json from mine-labels (default: adapter calibration cache)",
    )

    fidelity_parser = subparsers.add_parser(
        "check-fidelity",
        help="Compare adapter coverage job groups with Azure timeline job keys",
    )
    fidelity_parser.add_argument("--output", help="Calibration directory with azure-pr-job-timelines.json")

    mine_group = subparsers.add_parser("mine", help="extract records from a local clone")
    mine_sub = mine_group.add_subparsers(dest="subcommand", required=True)
    mine_commit = mine_sub.add_parser("commit", help="emit the JSON record of one commit")
    mine_commit.add_argument("--repo", required=True)
    mine_commit.add_argument("--commit", required=True)
    mine_commit.add_argument("--names-from", default="HEAD")
    mine_commit.add_argument("--output")

    dataset_group = subparsers.add_parser("dataset", help="build the commit dataset")
    dataset_sub = dataset_group.add_subparsers(dest="subcommand", required=True)
    dataset_build = dataset_sub.add_parser("build", help="mine first-parent history into a dataset")
    dataset_build.add_argument("--repo", required=True)
    dataset_build.add_argument("--rev", required=True)
    dataset_build.add_argument("--repo-type", choices=available_adapters())
    dataset_build.add_argument("--out")
    dataset_build.add_argument("--workers", type=int, default=0)
    dataset_build.add_argument("--exclude")
    dataset_build.add_argument("--no-szz", action="store_true")
    dataset_build.add_argument("--cache", default=str(DEFAULT_SCOUT_CACHE))
    dataset_build.add_argument("--refresh", action="store_true")

    ml_group = subparsers.add_parser("ml", help="risk and similarity baselines over a dataset")
    ml_sub = ml_group.add_subparsers(dest="subcommand", required=True)
    ml_train = ml_sub.add_parser("train-risk", help="train and evaluate commit risk models")
    ml_train.add_argument("--dataset", required=True)
    ml_train.add_argument("--label", default="bug_introducing", choices=("bug_introducing", "reverted_within_90d"))
    ml_train.add_argument("--out", default="models")
    ml_train.add_argument("--seed", type=int, default=0)
    ml_train.add_argument("--refresh", action="store_true")
    ml_train.add_argument("--final", action="store_true")
    ml_similar = ml_sub.add_parser("similar", help="similar commits in a dataset")
    ml_similar.add_argument("--dataset", required=True)
    similar_target = ml_similar.add_mutually_exclusive_group(required=True)
    similar_target.add_argument("--commit")
    similar_target.add_argument("--evaluate", action="store_true")
    ml_similar.add_argument("-k", type=int, default=10)
    ml_score = ml_sub.add_parser("score", help="LLM-ready risk bundle for one commit")
    ml_score.add_argument("--dataset", required=True)
    ml_score.add_argument("--model", required=True)
    ml_score.add_argument("--repo", required=True)
    ml_score.add_argument("--commit", required=True)
    ml_score.add_argument("-k", type=int, default=5)
    ml_pr = ml_sub.add_parser("train-pr-job", help="train PR-job model (gated by fidelity)")
    ml_pr.add_argument("--calibration")
    ml_pr.add_argument("--out", default="models")
    ml_pr.add_argument("--repo-type", choices=available_adapters(), required=True)
    ml_pr.add_argument("--repo-root", required=True)
    ml_pr.add_argument("--final", action="store_true", help="refit a kept model on train, valid and test")
    ml_walk = ml_sub.add_parser("walk-forward", help="score held-out commits into the online ledger")
    ml_walk.add_argument("--dataset", required=True)
    ml_walk.add_argument("--model", required=True)
    ml_walk.add_argument("--repo-type", choices=available_adapters(), required=True)
    ml_watch = ml_sub.add_parser("watch", help="score open PRs with the phase-0 heuristic into the online ledger")
    ml_watch.add_argument("--remote", required=True)
    ml_watch.add_argument("--repo-type", choices=available_adapters(), required=True)
    ml_watch.add_argument("--calibration")
    ml_watch.add_argument("--max-prs", type=int, default=100)
    ml_grade = ml_sub.add_parser("grade", help="fill PR outcomes from Azure gold labels and score the ledger")
    ml_grade.add_argument("--repo-type", choices=available_adapters(), required=True)
    ml_grade.add_argument("--calibration")

    backtest_parser = subparsers.add_parser(
        "backtest",
        help="Grade D6 on the seed corpus: recall on incidents, flag rate on controls. Offline once captured",
    )
    backtest_parser.add_argument(
        "--adapter",
        default="sonic-buildimage",
        choices=available_adapters(),
        help="Repository adapter to grade against (the seed corpus exists for sonic-buildimage only)",
    )
    backtest_parser.add_argument(
        "--capture",
        action="store_true",
        help="Allow network: mine the corpus and capture missing item fixtures. Without it, replay only",
    )
    backtest_parser.add_argument(
        "--corpus-dir", help="Pinned corpus directory (default: <cache>/eval/backtest/<adapter>)"
    )
    backtest_parser.add_argument("--revision", help="Pin the history tip when mining a new corpus")

    return parser.parse_args(argv)


def _open_source(args: argparse.Namespace) -> RepoSource:
    """Build the one source object every command reads through."""
    if args.remote:
        return RemoteRepo(
            args.remote,
            cache_root=Path(args.cache_dir) if args.cache_dir else None,
            depth=args.depth,
            max_depth=args.max_depth,
        )

    repo_root = Path(args.repo_root or ".").resolve()
    if not (repo_root / ".git").exists():
        raise ValueError(f"Not a git working copy: {repo_root}. Pass --repo-root, or --remote to fetch instead")
    return LocalCheckout(repo_root)


def _adapter(args: argparse.Namespace) -> Optional[RepoAdapter]:
    return get_adapter(args.repo_type) if args.repo_type else None


def _run_ingest(args: argparse.Namespace, source: RepoSource) -> int:
    chosen = [name for name, value in (("--range", args.commit_range), ("--sync", args.sync), ("--pr", args.pr))
              if value]
    if len(chosen) != 1:
        logger.fatal("Exactly one of --range, --sync or --pr is required; got %s", chosen or "none")
        return 2

    if args.pr:
        if not isinstance(source, RemoteRepo):
            logger.fatal("--pr fetches a pull request, so it needs --remote")
            return 2
        fetched = source.fetch_pull_request(args.pr)
        logger.info(
            "Pull request %d is %s..%s, fetched at depth %d in %d fetch(es) and %.2fs, cache now %.1f MB",
            args.pr,
            fetched.base_sha[:9],
            fetched.head_sha[:9],
            fetched.depth,
            fetched.fetches,
            fetched.duration_s,
            source.cache_size_bytes() / 1e6,
        )
        spec = ChangeSetSpec(
            base_ref=fetched.base_sha,
            head_ref=fetched.head_sha,
            mode=MODE_RANGE,
            include_merges=args.include_merges,
            context_lines=args.context_lines,
        )
    elif args.commit_range:
        spec = ChangeSetSpec.from_range(
            args.commit_range,
            mode=MODE_RANGE,
            include_merges=args.include_merges,
            context_lines=args.context_lines,
        )
        if isinstance(source, RemoteRepo):
            source.fetch_range(spec.base_ref, spec.head_ref, merge_base=spec.merge_base)
    else:
        local_ref, upstream_ref = args.sync
        spec = ChangeSetSpec(
            base_ref=local_ref,
            head_ref=upstream_ref,
            mode=MODE_SYNC,
            merge_base=True,
            include_merges=args.include_merges,
            context_lines=args.context_lines,
        )

    change_set = resolve(spec, source, _adapter(args))
    payload = json.dumps(change_set.to_dict(), indent=2, sort_keys=True)

    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        logger.info("Wrote change set to %s", args.output)
    else:
        print(payload)
    return 0


def _run_tree(args: argparse.Namespace, source: RepoSource) -> int:
    """Demonstrate and use the cheap half of the source API.

    Everything here answers from tree metadata, so against a blob-filtered remote it
    transfers nothing beyond the fetch that resolved the revision.
    """
    if args.pr:
        if not isinstance(source, RemoteRepo):
            logger.fatal("--pr fetches a pull request, so it needs --remote")
            return 2
        revision = source.fetch_pull_request(args.pr).head_sha
    else:
        revision = source.rev_parse(args.rev)

    started = time.monotonic()
    paths = source.paths_matching(revision, args.pattern) if args.pattern else source.list_paths(revision)
    elapsed = time.monotonic() - started

    logger.info(
        "Listed %d path(s) at %s in %.3fs with %d file read(s)",
        len(paths),
        revision[:9],
        elapsed,
        source.blob_reads,
    )
    if args.count:
        print(len(paths))
    else:
        print("\n".join(paths))
    return 0


def _brief_spec(args: argparse.Namespace, source: RepoSource) -> Tuple[Optional[ChangeSetSpec], str, str]:
    """What the brief is about: a pull request, a range, or a whole tree at one revision."""
    chosen = [name for name, value in (("--pr", args.pr), ("--range", args.commit_range), ("--rev", args.rev))
              if value]
    if len(chosen) != 1:
        raise ValueError(f"Exactly one of --pr, --range or --rev is required; got {chosen or 'none'}")

    if args.pr:
        if not isinstance(source, RemoteRepo):
            raise ValueError("--pr fetches a pull request, so it needs --remote")
        fetched = source.fetch_pull_request(args.pr)
        spec = ChangeSetSpec(
            base_ref=fetched.base_sha,
            head_ref=fetched.head_sha,
            mode=MODE_RANGE,
            context_lines=args.context_lines,
        )
        return spec, fetched.head_sha, "pr"

    if args.commit_range:
        spec = ChangeSetSpec.from_range(args.commit_range, mode=MODE_RANGE, context_lines=args.context_lines)
        if isinstance(source, RemoteRepo):
            source.fetch_range(spec.base_ref, spec.head_ref, merge_base=spec.merge_base)
        return spec, source.rev_parse(spec.head_ref), MODE_RANGE

    return None, source.rev_parse(args.rev), MODE_TREE


def _run_brief(args: argparse.Namespace, source: RepoSource) -> int:
    """Stage 1 end to end. Deterministic, offline past the fetch, and always emits."""
    if args.fixture:
        fixture = load_fixture(Path(args.fixture))
        source = fixture.source()
        args.rev = args.rev or fixture.rev
        repo = fixture.repo
        adapter = get_adapter(args.repo_type or fixture.adapter)
    else:
        repo = args.remote or str(Path(args.repo_root or ".").resolve().name)
        adapter = None

    spec, rev, mode = _brief_spec(args, source)
    if adapter is None:
        adapter = _adapter(args) or resolve_adapter(paths=source.list_paths(rev))

    run = run_static(
        source,
        repo=repo,
        rev=rev,
        adapter=adapter,
        spec=spec,
        mode=mode,
        hotspot_limit=args.hotspots,
    )

    if args.stdout:
        print(run.brief.to_json(), end="")
    else:
        write_brief(run, Path(args.output))

    coverage = run.brief.coverage
    # The counting rules are printed as an equation rather than a result, because on
    # upstream the platform count and the declaration count are both 287 and a reader who
    # is shown only one of them will reasonably assume they are the same quantity.
    logger.info(
        "%s: %d declaration(s) less %d shared directory(ies) plus %d aliased directory(ies) is "
        "%d platform(s); %d job group(s) "
        "(%s, loose scan %s); affected %d, covered %d, uncovered %d, ambiguous %d",
        adapter.name,
        coverage["declarations_in_tree"],
        len(coverage["excluded_as_non_platform"]),
        coverage.get("aliased_platforms", 0),
        coverage["platforms_in_tree"],
        coverage["job_groups"],
        coverage["parse"]["scope"],
        "agrees" if coverage["parse"]["loose_scan_agrees"] else "DISAGREES",
        len(coverage["affected"]),
        len(coverage["covered"]),
        len(coverage["uncovered"]),
        len(coverage["ambiguous"]),
    )
    return 0


def _review_provider(args: argparse.Namespace, fixture: Optional[ReviewFixture]) -> Optional[Provider]:
    """The provider `--provider` names, or None for a deliberately degraded run."""
    if args.provider == "none":
        return None
    spec = ollama_spec(args.model, max_output_tokens=REVIEW_MAX_OUTPUT_TOKENS)
    if args.provider == "replay":
        directory = Path(args.replay_dir) if args.replay_dir else (fixture.replay_dir if fixture else None)
        if directory is None:
            raise ValueError("--provider replay needs --replay-dir, or a --fixture that names its replay directory")
        try:
            return ReplayProvider.from_fixtures(directory)
        except ProviderError as error:
            # Degrade rather than refuse: the preflight fails the same way and the run still ships its brief.
            logger.warning("Replay provider has nothing to serve: %s", error)
            return ReplayProvider(directory, spec)
    provider: Provider = OllamaProvider(spec, base_url=args.ollama_url, timeout_s=args.timeout)
    if args.record:
        provider = RecordingProvider(provider, Path(args.record))
    return provider


def _review_spec(args: argparse.Namespace, source: RepoSource) -> Tuple[ChangeSetSpec, str, str]:
    chosen = [name for name, value in (("--pr", args.pr), ("--range", args.commit_range)) if value]
    if len(chosen) != 1:
        raise ValueError(f"Exactly one of --pr, --range or --fixture is required; got {chosen or 'none'}")
    if args.pr:
        if not isinstance(source, RemoteRepo):
            raise ValueError("--pr fetches a pull request, so it needs --remote")
        fetched = source.fetch_pull_request(args.pr)
        spec = ChangeSetSpec(base_ref=fetched.base_sha, head_ref=fetched.head_sha, mode=MODE_RANGE,
                             context_lines=args.context_lines)
        return spec, fetched.head_sha, "pr"
    spec = ChangeSetSpec.from_range(args.commit_range, mode=MODE_RANGE, context_lines=args.context_lines)
    if isinstance(source, RemoteRepo):
        source.fetch_range(spec.base_ref, spec.head_ref, merge_base=spec.merge_base)
    return spec, source.rev_parse(spec.head_ref), MODE_RANGE


def _run_review(args: argparse.Namespace, source: Optional[RepoSource]) -> int:
    """Stages 1 to 4 end to end. Advisory: a degraded run is a successful run (NFR-6)."""
    if args.fixture and (args.pr or args.commit_range):
        raise ValueError("--fixture replays a pinned change set, so it takes neither --pr nor --range")
    fixture = ReviewFixture.load(Path(args.fixture)) if args.fixture else None
    provider = _review_provider(args, fixture)
    output = Path(args.output_dir)

    if fixture is not None:
        result = review_fixture(fixture.path, provider=provider, output_dir=output, hotspot_limit=args.hotspots,
                                deadline_s=args.deadline)
    else:
        spec, rev, mode = _review_spec(args, source)
        adapter = _adapter(args) or resolve_adapter(paths=source.list_paths(rev))
        change_set = resolve(spec, source, adapter)
        repo = args.repo_name or args.remote or str(Path(args.repo_root or ".").resolve().name)
        result = run_review(source, repo=repo, adapter=adapter, rev=rev, change_set=change_set, provider=provider,
                            output_dir=output, mode=mode, hotspot_limit=args.hotspots, deadline_s=args.deadline)
    _log_review(result)
    return 0


def _log_review(result: ReviewResult) -> None:
    run = result.report.payload["run"]
    coverage = result.brief.coverage
    logger.info(
        "Review %s in %.1fs (static %.2fs, agent %.1fs): affected %d, covered %d, uncovered %d, ambiguous %d; "
        "%d finding(s); %d of %d question(s) put, %d answered; %d model call(s), %d tokens in, %d out",
        run["status"], result.duration_s, run["static_duration_s"], run["agent_duration_s"],
        len(coverage["affected"]), len(coverage["covered"]), len(coverage["uncovered"]), len(coverage["ambiguous"]),
        len(result.report.findings), run["questions_asked"], run["questions_in_brief"], run["questions_answered"],
        run["model_calls"], run["cost"]["input_tokens"], run["cost"]["output_tokens"],
    )
    for question in result.agent.questions:
        logger.info("  %s %s: %s in %.1fs over %d call(s)%s", question.id, question.kind, question.status,
                    question.latency_s, len(question.calls), f" ({question.reason})" if question.reason else "")
    logger.info("  checks fired: %s", ", ".join(f"{name} {count}" for name, count in run["checks"].items()))
    if result.report.payload["degraded_reason"]:
        logger.warning("Degraded: %s", result.report.payload["degraded_reason"])
    for name, path in sorted(result.paths.items()):
        logger.info("  wrote %s: %s", name, path)


def _calibration_adapter(args: argparse.Namespace, source: RepoSource) -> RepoAdapter:
    adapter = _adapter(args)
    if adapter is None:
        if isinstance(source, LocalCheckout):
            adapter = resolve_adapter(root=source.root)
        else:
            raise ValueError(f"{args.command} needs --repo-type when using --remote")
    return adapter


def _calibration_out(args: argparse.Namespace, source: RepoSource, adapter: RepoAdapter) -> Path:
    """Calibration dump directory. ``mine-incidents --output`` is a corpus file, not this path."""
    output = getattr(args, "output", None)
    if output and getattr(args, "command", None) != "mine-incidents":
        return Path(output)
    cache_dir = getattr(args, "cache_dir", None)
    return eval_calibration_dir(adapter, source, Path(cache_dir) if cache_dir else None)


def _require_local(source: RepoSource, command: str) -> Optional[LocalCheckout]:
    if not isinstance(source, LocalCheckout):
        logger.fatal("%s needs a working copy; pass --repo-root", command)
        return None
    return source


def _run_mine_git_dumps(args: argparse.Namespace, source: RepoSource) -> int:
    checkout = _require_local(source, "mine-git-dumps")
    if checkout is None:
        return 2
    adapter = _adapter(args) or resolve_adapter(root=checkout.root)
    out = _calibration_out(args, source, adapter)
    logger.info("mine-git-dumps writing under %s (ref %s)", out, args.revision)
    tip = run_git_dumps(
        adapter,
        GitRepo(checkout.root),
        str(out),
        ref=args.revision,
        resume=not args.refresh,
    )
    logger.info("Wrote git dumps for %s at %s under %s", adapter.name, tip[:12], out)
    return 0


def _run_mine_azure(args: argparse.Namespace, source: RepoSource) -> int:
    adapter = _calibration_adapter(args, source)
    out = _calibration_out(args, source, adapter)
    logger.info("mine-azure writing under %s", out)
    run_azure(adapter, str(out), workers=args.workers, resume=not args.refresh)
    logger.info("Wrote Azure dumps under %s", out)
    return 0


def _run_mine_labels(args: argparse.Namespace, source: RepoSource) -> int:
    checkout = _require_local(source, "mine-labels")
    if checkout is None:
        return 2
    adapter = _adapter(args) or resolve_adapter(root=checkout.root)
    out = _calibration_out(args, source, adapter)
    run_labels(adapter, str(out), repo=GitRepo(checkout.root), resume=not args.refresh)
    logger.info("Wrote model-* join under %s", out)
    return 0


def _run_score_pr(args: argparse.Namespace, source: RepoSource) -> int:
    checkout = _require_local(source, "score-pr")
    if checkout is None:
        return 2
    adapter = _adapter(args) or resolve_adapter(root=checkout.root)
    cal_dir = Path(args.calibration) if args.calibration else _calibration_out(args, source, adapter)
    if args.brief:
        payload = score_from_brief(args.brief, adapter, calibration_dir=cal_dir, repo=GitRepo(checkout.root))
    else:
        payload = score_pr_repo(GitRepo(checkout.root), adapter, args.base, calibration_dir=cal_dir)
    # Primary output is JSON on stdout; --quiet must not silence it (redirect-friendly).
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _run_check_fidelity(args: argparse.Namespace, source: RepoSource) -> int:
    adapter = _calibration_adapter(args, source)
    out = _calibration_out(args, source, adapter)
    from .eval._io import load
    from .eval.fidelity import check_coverage_fidelity, check_mgmt_topology_fidelity

    timelines = load(str(out), "azure-pr-job-timelines.json")
    azure_jobs = set()
    for build in timelines.get("builds") or []:
        for group in (build.get("groupJobs") or {}).values():
            azure_jobs.update(group.keys())
    cfg = join_cfg(adapter)
    model_groups = set((cfg.get("jobs") or {}).get("gold") or [])
    excluded = set(excluded_jobs(cfg))
    result = check_coverage_fidelity(model_groups, azure_jobs, excluded=excluded)
    if adapter.name == "sonic-mgmt":
        pretest = set((cfg.get("jobs") or {}).get("pretest") or [])
        kvm_model = model_groups - pretest
        kvm_azure = azure_jobs - pretest - excluded
        topo = check_mgmt_topology_fidelity(kvm_model, kvm_azure)
        result = {"coverage": result, "topology": topo, "ok": result["ok"] and topo["ok"]}
    if args.quiet:
        logger.info("check-fidelity: ok=%s", result["ok"])
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def _run_backtest(args: argparse.Namespace) -> int:
    try:
        payload = run_backtest(
            adapter_name=args.adapter,
            cache_root=Path(args.cache_dir) if args.cache_dir else None,
            capture=args.capture,
            directory=Path(args.corpus_dir) if args.corpus_dir else None,
            revision=args.revision,
        )
    except BacktestError as error:
        logger.error("%s", error)
        return 2
    if not args.quiet:
        summary = {key: value for key, value in payload.items() if key != "rows"}
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _run_miner(args: argparse.Namespace, source: RepoSource) -> int:
    if not isinstance(source, LocalCheckout):
        logger.fatal("mine-incidents walks whole history, so it needs a working copy; pass --repo-root")
        return 2

    adapter = _adapter(args) or resolve_adapter(root=source.root)
    out = _calibration_out(args, source, adapter)
    incidents = run_incidents_cache(
        GitRepo(source.root),
        out,
        revision=args.revision,
        grep=args.grep,
        limit=args.limit,
        refresh=args.refresh,
    )
    write_corpus(incidents, Path(args.output))
    stats = summarize(incidents)
    if args.quiet:
        logger.info(
            "mine-incidents: %d reverts, %d linked, corpus %s",
            stats["reverts"],
            stats["linked"],
            args.output,
        )
    else:
        print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


def _pop_global_quiet(argv: list[str]) -> Tuple[bool, list[str]]:
    quiet = False
    rest: list[str] = []
    for arg in argv:
        if arg in ("-q", "--quiet"):
            quiet = True
        else:
            rest.append(arg)
    return quiet, rest


def _mining_argv(argv: list[str]) -> bool:
    """True when ``argv`` is a mining group, optionally after the ``--cache-dir`` both parsers share."""
    index = 0
    while index < len(argv) and argv[index].startswith("--cache-dir"):
        index += 1 if "=" in argv[index] else 2
    return index < len(argv) and argv[index] in MINING_GROUPS


def run(argv: Optional[list[str]] = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    global_quiet, argv = _pop_global_quiet(argv)
    if _mining_argv(argv):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
        try:
            return mining_main(argv, quiet=global_quiet)
        except TaxonomyError as error:
            logger.fatal("mining failed: %s", error)
            return 1

    args = _parse_args(argv)
    if global_quiet:
        args.quiet = True
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if args.repo_root and args.remote:
        logger.fatal("--repo-root reads a working copy and --remote fetches one; pass only one")
        return 2

    try:
        if args.command == "backtest":
            return _run_backtest(args)
        source = _open_source(args) if not getattr(args, "fixture", None) else None
        if args.command == "ingest":
            return _run_ingest(args, source)
        if args.command == "tree":
            return _run_tree(args, source)
        if args.command == "brief":
            return _run_brief(args, source)
        if args.command == "review":
            return _run_review(args, source)
        if args.command == "mine-git-dumps":
            return _run_mine_git_dumps(args, source)
        if args.command == "mine-azure":
            return _run_mine_azure(args, source)
        if args.command == "mine-labels":
            return _run_mine_labels(args, source)
        if args.command == "score-pr":
            return _run_score_pr(args, source)
        if args.command == "check-fidelity":
            return _run_check_fidelity(args, source)
        if args.command == "mine-incidents":
            return _run_miner(args, source)
        logger.fatal("Unknown command %s", args.command)
        return 2
    except (GitError, TaxonomyError, RepoAdapterError, ValueError, FixtureError, SchemaError, EntityIndexError,
            PipelineParseError, BriefContractError, ValidationError, ReportContractError) as error:
        logger.fatal("%s failed: %s", args.command, error)
        return 1
