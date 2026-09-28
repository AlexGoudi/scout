#!/usr/bin/env python3
"""Adapter calibration tables and path rules."""

from __future__ import annotations

import unittest

from scout_impl.eval.calibration_rules import bucket_of, is_headline_revert, is_revert, jobs_for_path, last_pr
from scout_impl.repos import get_adapter


class LoadAdapter(unittest.TestCase):
    def test_sonic_mgmt(self):
        adapter = get_adapter("sonic-mgmt")
        self.assertEqual(adapter.github_api.repo, "sonic-mgmt")
        self.assertIn("t0", adapter.calibration.tables["jobs"]["gold"])

    def test_sonic_buildimage(self):
        adapter = get_adapter("sonic-buildimage")
        cfg = adapter.calibration.tables
        self.assertIn("vs", cfg["jobs"]["gold"])


class RevertRules(unittest.TestCase):
    def test_headline_vs_tagged(self):
        self.assertTrue(is_headline_revert('Revert "x"'))
        self.assertFalse(is_headline_revert("[tag] Revert \"x\""))
        self.assertTrue(is_revert("[tag] Revert \"x\""))


class PathRulesMgmt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = get_adapter("sonic-mgmt").calibration.tables

    def test_conftest_bucket(self):
        self.assertEqual(bucket_of("tests/conftest.py", self.cfg), "shared_pytest")
        path_class = get_adapter("sonic-mgmt").classify("tests/conftest.py")
        self.assertEqual(path_class.id, "test_common")

    def test_bgp_jobs(self):
        jobs = jobs_for_path("tests/bgp/test.py", self.cfg)
        self.assertIn("t0", jobs)
        self.assertNotIn("t0_vpp", jobs)


class PathRulesBuildimage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = get_adapter("sonic-buildimage").calibration.tables

    def test_slave_mk(self):
        self.assertEqual(bucket_of("slave.mk", self.cfg), "build_system")
        jobs = jobs_for_path("slave.mk", self.cfg)
        self.assertIn("broadcom", jobs)


class PrParse(unittest.TestCase):
    def test_last_pr(self):
        self.assertEqual(last_pr("Support BMC (#27557)"), 27557)


if __name__ == "__main__":
    unittest.main()
