"""Commit mining, dataset build, and ML baseline CLI handlers."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
from pathlib import Path
from typing import Any, Callable, Sequence

from .mining.gitio import GitError
from .mining.taxonomy import TaxonomyError, load_taxonomy

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path(__file__).resolve().parents[1] / ".scout-cache"
MINING_GROUPS = ("mine", "dataset", "ml")
SCOUT_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_BY_REPO = {
    "sonic-buildimage": SCOUT_ROOT / "scout_impl" / "repos" / "buildimage" / "taxonomy.yaml",
    "sonic-mgmt": SCOUT_ROOT / "scout_impl" / "repos" / "mgmt" / "taxonomy.yaml",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scout mining, dataset and ML commands")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Log progress only; do not print JSON summaries to stdout",
    )
    parser.add_argument(
        "--cache-dir",
        help="Cache root whose eval/ holds the calibration directories and the online ledger "
        "(default: $SCOUT_CACHE_DIR, else the XDG cache)",
    )
    groups = parser.add_subparsers(dest="group", required=True)

    mine = groups.add_parser("mine", help="extract records from a local clone").add_subparsers(
        dest="command", required=True
    )
    commit = mine.add_parser("commit", help="emit the JSON record of one commit")
    commit.add_argument("--repo", required=True)
    commit.add_argument("--commit", required=True)
    commit.add_argument("--names-from", default="HEAD")
    commit.add_argument("--output")

    dataset = groups.add_parser("dataset", help="build the commit dataset").add_subparsers(
        dest="command", required=True
    )
    build = dataset.add_parser("build", help="mine first-parent history into a dataset")
    build.add_argument("--repo", required=True)
    build.add_argument("--rev", required=True)
    build.add_argument("--repo-type", choices=tuple(TAXONOMY_BY_REPO))
    build.add_argument("--out")
    build.add_argument("--workers", type=int, default=0)
    build.add_argument("--exclude")
    build.add_argument("--no-szz", action="store_true")
    build.add_argument("--cache", default=str(DEFAULT_CACHE))
    build.add_argument("--refresh", action="store_true")

    ml = groups.add_parser("ml", help="risk and similarity baselines").add_subparsers(dest="command", required=True)
    train = ml.add_parser("train-risk", help="train commit risk models")
    train.add_argument("--dataset", required=True)
    train.add_argument("--label", default="bug_introducing", choices=("bug_introducing", "reverted_within_90d"))
    train.add_argument("--out", default="models")
    train.add_argument("--seed", type=int, default=0)
    train.add_argument("--refresh", action="store_true")
    train.add_argument("--final", action="store_true")

    pr_job = ml.add_parser("train-pr-job", help="train PR-job model gated by fidelity")
    pr_job.add_argument("--calibration", help="adapter calibration directory")
    pr_job.add_argument("--out", default="models")
    pr_job.add_argument("--repo-type", choices=("sonic-mgmt", "sonic-buildimage"), required=True)
    pr_job.add_argument("--repo-root", required=True)
    pr_job.add_argument("--final", action="store_true", help="refit a kept model on train, valid and test")

    walk = ml.add_parser("walk-forward", help="score held-out commits into the online ledger")
    walk.add_argument("--dataset", required=True)
    walk.add_argument("--model", required=True, help="a risk model trained without --final")
    walk.add_argument("--repo-type", choices=("sonic-mgmt", "sonic-buildimage"), required=True)

    watch = ml.add_parser("watch", help="score open PRs with the phase-0 heuristic into the online ledger")
    watch.add_argument("--remote", required=True)
    watch.add_argument("--repo-type", choices=("sonic-mgmt", "sonic-buildimage"), required=True)
    watch.add_argument("--calibration", help="adapter calibration cache with scoring-plan.json")
    watch.add_argument("--max-prs", type=int, default=100, help="new PR heads to score per run")

    grade = ml.add_parser("grade", help="fill PR outcomes from Azure gold labels and score the ledger")
    grade.add_argument("--repo-type", choices=("sonic-mgmt", "sonic-buildimage"), required=True)
    grade.add_argument("--calibration", help="adapter calibration cache with model-dataset-pr.json")

    similar = ml.add_parser("similar", help="similar commits in a dataset")
    similar.add_argument("--dataset", required=True)
    target = similar.add_mutually_exclusive_group(required=True)
    target.add_argument("--commit")
    target.add_argument("--evaluate", action="store_true")
    similar.add_argument("-k", type=int, default=10)

    score = ml.add_parser("score", help="LLM-ready risk bundle for one commit")
    score.add_argument("--dataset", required=True)
    score.add_argument("--model", required=True)
    score.add_argument("--repo", required=True)
    score.add_argument("--commit", required=True)
    score.add_argument("-k", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None, quiet: bool = False) -> int:
    arguments = build_parser().parse_args(argv)
    if quiet:
        arguments.quiet = True
    arguments.cache_root = Path(arguments.cache_dir) if arguments.cache_dir else None
    try:
        handler = HANDLERS[(arguments.group, arguments.command)]
        return handler(arguments)
    except (GitError, TaxonomyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"error: {exc.msg} ({exc.url})", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: network: {exc.reason}", file=sys.stderr)
        return 1


def _mine_commit(arguments: argparse.Namespace) -> int:
    from .mining.gitio import Git
    from .mining.record import make_context, mine_commit, record_to_json

    git = Git(arguments.repo)
    names_from = arguments.names_from if git.resolve_optional(arguments.names_from) else arguments.commit
    record = mine_commit(git, arguments.commit, make_context(git, names_from))
    return _emit(record_to_json(record, pretty=True), arguments.output)


def _dataset_build(arguments: argparse.Namespace) -> int:
    from .dataset.build import build_dataset

    taxonomy = None
    if arguments.repo_type:
        taxonomy = load_taxonomy(TAXONOMY_BY_REPO[arguments.repo_type])
    card = build_dataset(
        repo=arguments.repo,
        rev=arguments.rev,
        out=arguments.out,
        workers=arguments.workers or None,
        exclude=arguments.exclude,
        szz=not arguments.no_szz,
        cache=arguments.cache,
        taxonomy=taxonomy,
        repo_type=arguments.repo_type or "sonic-buildimage",
        refresh=arguments.refresh,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    if not arguments.quiet:
        summary = {
            "commits": card["commits"],
            "splits": card["splits"]["counts"],
            "reverts": card["labels"]["reverts"],
            "labels": card["labels"]["as_of_snapshot"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _ml_train(arguments: argparse.Namespace) -> int:
    from .ml.risk import train_risk

    card = train_risk(
        arguments.dataset,
        arguments.label,
        arguments.out,
        seed=arguments.seed,
        refresh=arguments.refresh,
        final=arguments.final,
    )
    if arguments.quiet:
        metrics = card["test_metrics"].get("gradient_boosting") or card["test_metrics"].get("logistic") or {}
        roc = metrics.get("roc_auc", {}).get("value")
        logger.info("train-risk: label=%s out=%s test roc_auc=%s", arguments.label, arguments.out, roc)
    else:
        print(json.dumps(card["test_metrics"], indent=2, sort_keys=True))
    return 0


def _ml_pr_job(arguments: argparse.Namespace) -> int:
    from .ml.pr_job import train_pr_job

    card = train_pr_job(
        calibration_dir=Path(arguments.calibration) if arguments.calibration else None,
        out=Path(arguments.out),
        repo_type=arguments.repo_type,
        repo_root=Path(arguments.repo_root),
        final=arguments.final,
        cache_root=arguments.cache_root,
    )
    if arguments.quiet:
        logger.info("train-pr-job: wrote %s", arguments.out)
    else:
        print(json.dumps(card, indent=2, sort_keys=True))
    return 0


def _ml_walk(arguments: argparse.Namespace) -> int:
    from .ml.online import walk_forward

    summary = walk_forward(arguments.dataset, arguments.model, arguments.repo_type, cache_root=arguments.cache_root)
    if arguments.quiet:
        logger.info("walk-forward: %s", summary)
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _ml_watch(arguments: argparse.Namespace) -> int:
    from .ml.online import watch_open_prs

    summary = watch_open_prs(
        arguments.remote,
        arguments.repo_type,
        Path(arguments.calibration) if arguments.calibration else None,
        max_prs=arguments.max_prs,
        cache_root=arguments.cache_root,
    )
    if arguments.quiet:
        logger.info("watch: %s", summary)
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _ml_grade(arguments: argparse.Namespace) -> int:
    from .ml.online import grade_ledger

    summary = grade_ledger(
        arguments.repo_type,
        Path(arguments.calibration) if arguments.calibration else None,
        cache_root=arguments.cache_root,
    )
    if arguments.quiet:
        logger.info("grade: %s", summary)
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _ml_similar(arguments: argparse.Namespace) -> int:
    from .ml.similar import SimilarityIndex, evaluate_similarity

    if arguments.evaluate:
        result = evaluate_similarity(arguments.dataset, arguments.k)
        if arguments.quiet:
            bug = result.get("labels", {}).get("bug_introducing", {})
            logger.info(
                "similar evaluate: split=%s k=%s bug_introducing lift=%s",
                result.get("split"),
                result.get("k"),
                bug.get("lift"),
            )
            return 0
        return _emit(_json(result), None)
    index = SimilarityIndex.load(arguments.dataset)
    return _emit(_json(index.query_sha(arguments.commit, arguments.k)), None)


def _ml_score(arguments: argparse.Namespace) -> int:
    from .ml.score import score_commit

    bundle = score_commit(arguments.dataset, arguments.model, arguments.repo, arguments.commit, k=arguments.k)
    return _emit(_json(bundle), None)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _emit(text: str, output: str | None) -> int:
    data = text.encode("utf-8")
    if output:
        Path(output).write_bytes(data)
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    return 0


HANDLERS: dict[tuple[str, str], Callable[[argparse.Namespace], int]] = {
    ("mine", "commit"): _mine_commit,
    ("dataset", "build"): _dataset_build,
    ("ml", "train-risk"): _ml_train,
    ("ml", "train-pr-job"): _ml_pr_job,
    ("ml", "walk-forward"): _ml_walk,
    ("ml", "watch"): _ml_watch,
    ("ml", "grade"): _ml_grade,
    ("ml", "similar"): _ml_similar,
    ("ml", "score"): _ml_score,
}
