import pandas as pd

from scout_impl.ml.matrix import column_spec


def test_identity_columns_never_reach_a_model():
    frame = pd.DataFrame(
        {
            "sha": ["a", "b"],
            "split": ["train", "test"],
            "change_type": ["code", "code"],
            "is_merge": [False, False],
            "churn": [3, 40],
            "is_test_only": [True, False],
            "author_is_bot": [False, True],
            "author_prior_commits": [1, 9],
            "author_first_commit": [True, False],
            "committer_prior_commits": [0, 2],
            "file_prior_authors": [2, 5],
            "label_bug_introducing": [False, True],
        }
    )
    spec = column_spec(frame)
    assert spec.numeric == ("churn", "is_test_only")
    assert spec.log_columns == ("churn",)
