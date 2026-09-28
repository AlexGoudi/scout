import hashlib
import json

import joblib
import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import run_scout
from scout_impl.dataset.build import build_dataset
from scout_impl.dataset.shards import iter_json
from scout_impl.ml.matrix import column_spec, load_table, spec_from_dict
from scout_impl.ml.risk import load_model, predict
from scout_impl.ml.score import score_commit
from scout_impl.ml.similar import SET_WEIGHT, TEXT_WEIGHT, Entry, SimilarityIndex, known_outcomes
from unit_test_split_export import year_history

UNKNOWN = {"reverted": None, "bug_introducing": None}


def entry(index, *, labels=UNKNOWN, split="train", is_merge=False, landed=None):
    return Entry(
        sha=f"{index:040x}", index=index, landed=index * 100 if landed is None else landed, subject=f"commit {index}",
        change_type="feature", split=split, is_merge=is_merge, labels=labels,
    )


def small_index(entries, texts, items):
    return SimilarityIndex(entries, texts, items, fit_until=10**9)


def test_score_mixes_text_cosine_and_set_jaccard():
    texts = ["kernel driver patch", "bgp route flap", "bgp route flap", "bgp kernel"]
    items = [{"file:d"}, {"file:a", "file:b"}, {"file:a", "file:c"}, {"file:a"}]
    index = small_index([entry(i) for i in range(4)], texts, items)
    top = index.query_position(2, k=3)
    assert [item["sha"] for item in top] == [entry(1).sha, entry(0).sha]
    assert top[1]["score"] == 0.0
    first = top[0]
    assert first["text_similarity"] == pytest.approx(1.0)
    assert first["jaccard"] == pytest.approx(1 / 3)
    assert first["score"] == pytest.approx(TEXT_WEIGHT * 1.0 + SET_WEIGHT / 3)
    assert first["shared"] == ["file:a"]


def test_neighbours_are_older_and_never_merges_holdout_or_the_query():
    entries = [entry(0), entry(1, is_merge=True), entry(2, split="holdout"), entry(3), entry(4), entry(5)]
    texts = ["same words here"] * len(entries)
    items = [{"file:x"}] * len(entries)
    index = small_index(entries, texts, items)
    shas = [item["sha"] for item in index.query_position(4, k=10)]
    assert shas == [entry(3).sha, entry(0).sha]
    assert index.query_position(0, k=10) == []
    record_like = {
        "message": {"subject": "same words here", "sections": {}, "body": ""},
        "files": [{"old_path": None, "new_path": "x"}],
        "areas": {"components": [], "features": [], "entities": []},
    }
    assert [item["sha"] for item in index.query_record(record_like, landed=301, k=10)] == [
        entry(3).sha, entry(0).sha,
    ]


def test_outcomes_count_only_once_they_have_landed():
    labels = {"reverted": True, "reverted_landed": 500, "bug_introducing": True, "fixed_landed": 200}
    assert known_outcomes(labels, 300) == {"reverted": False, "bug_introducing": True}
    assert known_outcomes(labels, 500) == {"reverted": False, "bug_introducing": True}
    assert known_outcomes(labels, 501) == {"reverted": True, "bug_introducing": True}
    assert known_outcomes({"reverted": False, "bug_introducing": None}, 900) == {
        "reverted": False, "bug_introducing": None,
    }


def test_lift_compares_neighbour_rates_of_positive_and_negative_queries():
    bug = {"reverted": False, "bug_introducing": True, "fixed_landed": 50, "reverted_landed": None}
    clean = {"reverted": False, "bug_introducing": False}
    entries = [entry(0, labels=bug), entry(1, labels=clean), entry(2), entry(3)]
    texts = ["alpha beta", "gamma delta", "alpha beta", "gamma delta"]
    items = [{"file:a"}, {"file:g"}, {"file:a"}, {"file:g"}]
    index = small_index(entries, texts, items)
    report = index.evaluate_lift({entries[2].sha: True, entries[3].sha: False}, "bug_introducing", k=1)
    assert report["neighbour_positive_rate"] == {"positive_queries": 1.0, "negative_queries": 0.0}
    assert report["lift"] is None
    report = index.evaluate_lift({entries[2].sha: True, entries[3].sha: False}, "bug_introducing", k=3)
    assert report["neighbour_positive_rate"] == {"positive_queries": 0.5, "negative_queries": 0.5}
    assert report["lift"] == 1.0


def fake_model(dataset, path):
    """A risk bundle in the shape 'ml train-risk' writes, fitted to a stand-in label."""
    table = load_table(dataset)
    spec = column_spec(table)
    frame = table[~table["is_merge"].astype(bool)]
    x = spec.transform(frame)
    y = (frame["churn"] > frame["churn"].median()).to_numpy()
    logistic = make_pipeline(StandardScaler(), LogisticRegression()).fit(x, y)
    card_sha = hashlib.sha256((dataset / "dataset_card.json").read_bytes()).hexdigest()
    joblib.dump(
        {
            "label": "bug_introducing",
            "spec": spec.to_dict(),
            "prevalence": float(y.mean()),
            "models": {"logistic": logistic},
            "calibrators": {"logistic": (1.0, 0.0)},
            "selected": "logistic",
            "reference_scores": np.sort(logistic.predict_proba(x)[:, 1]),
            "card": {"dataset": {"card_sha256": card_sha}, "seed": 0},
        },
        path,
    )
    return path


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("similar")
    repo, shas = year_history(root / "repo")
    exclude = root / "exclude.txt"
    exclude.write_text(f"{shas[20]}\n")
    build_dataset(repo=repo.path, rev=shas[-2], out=root / "previous", workers=1, cache=None, exclude=exclude)
    build_dataset(repo=repo.path, rev="HEAD", out=root / "full", workers=1, cache=None, exclude=exclude)
    model = fake_model(root / "previous", root / "risk-bug_introducing.joblib")
    return repo, shas, root, model


def test_dataset_neighbours_respect_landing_order(corpus):
    repo, shas, root, model = corpus
    index = SimilarityIndex.load(root / "full")
    revert = index.query_sha(shas[12][:10], k=3)
    assert revert["query"]["sha"] == shas[12]
    reverted = next(item for item in revert["neighbours"] if item["sha"] == shas[10])
    assert reverted["outcomes_known_at_query"]["reverted"] is False
    for position in range(len(shas)):
        landed = index.entries[position].landed
        for item in index.query_position(position, k=len(shas)):
            assert shas.index(item["sha"]) < position and item["sha"] != shas[20]
            assert index.entries[index.position[item["sha"]]].landed <= landed
    later = index.query_position(30, k=len(shas))
    assert next(item for item in later if item["sha"] == shas[10])["outcomes_known_at_query"]["reverted"] is True


def test_score_bundle_for_a_dataset_commit(corpus):
    repo, shas, root, model = corpus
    bundle = score_commit(root / "previous", model, repo.path, shas[30][:12], k=3)
    assert set(bundle) == {"bundle_version", "commit", "document", "risk", "similar", "provenance"}
    assert bundle["commit"]["sha"] == shas[30] and bundle["commit"]["in_dataset"] is True
    document = next(item for item in iter_json(root / "previous" / "llm") if item["sha"] == shas[30])
    assert bundle["document"] == document
    risk = bundle["risk"]
    assert 0 < risk["probability"] < 1 and 0 <= risk["percentile"] <= 100 and len(risk["reasons"]) == 5
    assert len(bundle["similar"]) == 3
    assert all(shas.index(item["sha"]) < 30 for item in bundle["similar"])


def test_a_new_commit_scores_as_it_would_in_a_rebuilt_dataset(corpus):
    repo, shas, root, model = corpus
    bundle = score_commit(root / "previous", model, repo.path, shas[-1], k=5)
    assert bundle["commit"]["in_dataset"] is False and bundle["commit"]["split"] is None
    full = load_table(root / "full")
    saved = load_model(model)
    x = spec_from_dict(saved["spec"]).transform(full[full["sha"] == shas[-1]])
    assert bundle["risk"]["probability"] == pytest.approx(float(predict(saved, x)[0]), abs=1e-6)
    rebuilt = next(item for item in iter_json(root / "full" / "llm") if item["sha"] == shas[-1])
    assert bundle["document"] == rebuilt
    assert len(bundle["similar"]) == 5 and all(item["sha"] in shas[:-1] for item in bundle["similar"])


def test_score_rejects_a_model_from_another_build(corpus):
    repo, shas, root, model = corpus
    with pytest.raises(ValueError, match="different build"):
        score_commit(root / "full", model, repo.path, shas[-1])


def test_cli_similar_and_score(corpus, capsys):
    repo, shas, root, model = corpus
    dataset = str(root / "previous")
    assert run_scout.main(["ml", "similar", "--dataset", dataset, "--commit", shas[25][:10], "-k", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)["neighbours"]) == 2
    assert run_scout.main(["ml", "similar", "--dataset", dataset, "--evaluate"]) == 0
    assert set(json.loads(capsys.readouterr().out)["labels"]) == {"bug_introducing", "reverted"}
    arguments = ["ml", "score", "--dataset", dataset, "--model", str(model), "--repo", str(repo.path),
                 "--commit", shas[-1]]
    assert run_scout.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["commit"]["sha"] == shas[-1]
    assert run_scout.main(["ml", "similar", "--dataset", dataset, "--commit", "0" * 12]) == 1
