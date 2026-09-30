#!/usr/bin/env python3
"""Integration stubs: fidelity, calibration, score-pr brief view."""

from __future__ import annotations

import json
import tempfile
import unittest

from scout_impl.eval.calibration import pick_threshold_from_valid, scorecard_on_test
from scout_impl.eval.fidelity import check_coverage_fidelity
from scout_impl.eval.score_pr import score_from_brief
from scout_impl.repos import get_adapter
from scout_impl.static.hotspots import WEIGHT_PATH_CLASS


class Fidelity(unittest.TestCase):
    def test_mismatch_fails(self):
        result = check_coverage_fidelity({"a", "b"}, {"b", "c"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["only_in_coverage_model"], ["a"])

    def test_excluded_azure_jobs_do_not_fail(self):
        result = check_coverage_fidelity({"a", "b"}, {"a", "b", "vpp"}, excluded={"vpp"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["only_in_azure_timelines"], [])
        self.assertEqual(result["ignored_excluded_in_azure"], ["vpp"])


class Calibration(unittest.TestCase):
    def test_threshold(self):
        scores = [0.2, 0.8, 0.9, 0.1]
        labels = [0, 1, 1, 0]
        threshold = pick_threshold_from_valid(scores, labels)
        self.assertIn("threshold", threshold)
        card = scorecard_on_test([1, 0, 1, 0], labels)
        self.assertEqual(card["tp"], 1)


class HotspotWeights(unittest.TestCase):
    def test_static_weight(self):
        self.assertGreater(WEIGHT_PATH_CLASS, 0.0)


class ScorePrBrief(unittest.TestCase):
    def test_brief_view(self):
        adapter = get_adapter("sonic-buildimage")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"hotspots": [{"path": "slave.mk"}]}, handle)
            path = handle.name
        out = score_from_brief(path, adapter)
        self.assertEqual(out["source"], "scout-brief-view")
        self.assertIn("slave.mk", out["files"])


if __name__ == "__main__":
    unittest.main()
