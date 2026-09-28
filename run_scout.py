#!/usr/bin/env python3
"""Entrypoint for the SONiC Scout offline utilities.

`mine`, `dataset` and `ml` are the commit mining, commit dataset and ML baseline commands
defined here; every other command is served by `scout_impl.cli`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

DEFAULT_CACHE = Path(__file__).resolve().parent / ".scout-cache"
MINING_GROUPS = ("mine", "dataset", "ml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_scout.py",
        description="Scout commit mining, the commit dataset and the ML baseline.",
    )
    groups = parser.add_subparsers(dest="group", required=True)

    mine = groups.add_parser("mine", help="extract records from a local clone").add_subparsers(
        dest="command", required=True
    )
    commit = mine.add_parser("commit", help="emit the JSON record of one commit")
    commit.add_argument("--repo", required=True, help="path to a local clone; only ever read")
    commit.add_argument("--commit", required=True, help="revision to mine")
    commit.add_argument(
        "--names-from", default="HEAD", help="redact every author and committer name reachable from here"
    )
    commit.add_argument("--output", help="write here instead of stdout")

    dataset = groups.add_parser("dataset", help="build the commit dataset").add_subparsers(
        dest="command", required=True
    )
    build = dataset.add_parser("build", help="mine the first-parent history of --rev into a dataset")
    build.add_argument("--repo", required=True, help="path to a full local clone; only ever read")
    build.add_argument("--rev", required=True, help="snapshot revision; the dataset covers its first-parent chain")
    build.add_argument("--out", help="output directory (default: corpus/buildimage-<short sha>)")
    build.add_argument("--workers", type=int, default=0, help="worker processes (default: CPU count)")
    build.add_argument("--exclude", help="file of commit SHAs to move into the holdout split")
    build.add_argument("--no-szz", action="store_true", help="skip the SZZ bug-introducing label")
    build.add_argument("--cache", default=str(DEFAULT_CACHE), help="record and blame cache directory")

    ml = groups.add_parser("ml", help="risk and similarity baselines over a dataset").add_subparsers(
        dest="command", required=True
    )
    train = ml.add_parser("train-risk", help="train and evaluate the commit risk models")
    train.add_argument("--dataset", required=True, help="dataset directory from 'dataset build'")
    train.add_argument("--label", default="bug_introducing", choices=("bug_introducing", "reverted_within_90d"))
    train.add_argument("--out", default="models", help="directory for the model, card and report")
    train.add_argument("--seed", type=int, default=0)

    similar = ml.add_parser("similar", help="earlier commits most similar to one dataset commit")
    similar.add_argument("--dataset", required=True)
    target = similar.add_mutually_exclusive_group(required=True)
    target.add_argument("--commit", help="SHA or unique prefix of a dataset commit")
    target.add_argument(
        "--evaluate", action="store_true", help="report neighbour lift for each label over the test split"
    )
    similar.add_argument("-k", type=int, default=10)

    score = ml.add_parser("score", help="LLM-ready risk bundle for one commit")
    score.add_argument("--dataset", required=True)
    score.add_argument("--model", required=True, help="a risk-<label>.joblib from 'ml train-risk'")
    score.add_argument("--repo", required=True, help="the clone the commit lives in")
    score.add_argument("--commit", required=True)
    score.add_argument("-k", type=int, default=5, help="similar commits to include")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    from scout_impl.mining.gitio import GitError
    from scout_impl.mining.taxonomy import TaxonomyError

    try:
        handler = HANDLERS[(arguments.group, arguments.command)]
        return handler(arguments)
    except (GitError, TaxonomyError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _mine_commit(arguments: argparse.Namespace) -> int:
    from scout_impl.mining.gitio import Git
    from scout_impl.mining.record import make_context, mine_commit, record_to_json

    git = Git(arguments.repo)
    names_from = arguments.names_from if git.resolve_optional(arguments.names_from) else arguments.commit
    record = mine_commit(git, arguments.commit, make_context(git, names_from))
    return _emit(record_to_json(record, pretty=True), arguments.output)


def _dataset_build(arguments: argparse.Namespace) -> int:
    from scout_impl.dataset.build import build_dataset

    card = build_dataset(
        repo=arguments.repo,
        rev=arguments.rev,
        out=arguments.out,
        workers=arguments.workers or None,
        exclude=arguments.exclude,
        szz=not arguments.no_szz,
        cache=arguments.cache,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    summary = {
        "commits": card["commits"],
        "splits": card["splits"]["counts"],
        "reverts": card["labels"]["reverts"],
        "labels": card["labels"]["as_of_snapshot"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _ml_train(arguments: argparse.Namespace) -> int:
    from scout_impl.ml.risk import train_risk

    card = train_risk(arguments.dataset, arguments.label, arguments.out, seed=arguments.seed)
    print(json.dumps(card["test_metrics"], indent=2, sort_keys=True))
    return 0


def _ml_similar(arguments: argparse.Namespace) -> int:
    from scout_impl.ml.similar import SimilarityIndex, evaluate_similarity

    if arguments.evaluate:
        return _emit(_json(evaluate_similarity(arguments.dataset, arguments.k)), None)
    index = SimilarityIndex.load(arguments.dataset)
    return _emit(_json(index.query_sha(arguments.commit, arguments.k)), None)


def _ml_score(arguments: argparse.Namespace) -> int:
    from scout_impl.ml.score import score_commit

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


HANDLERS = {
    ("mine", "commit"): _mine_commit,
    ("dataset", "build"): _dataset_build,
    ("ml", "train-risk"): _ml_train,
    ("ml", "similar"): _ml_similar,
    ("ml", "score"): _ml_score,
}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in MINING_GROUPS:
        sys.exit(main())

    from scout_impl.cli import run

    raise SystemExit(run())
