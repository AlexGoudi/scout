#!/usr/bin/env python3
from __future__ import annotations

import unittest

from scout_impl.eval._pr_files import files_for_model, parse_numstat, path_bag_usable


class PathBag(unittest.TestCase):
    def test_two_dot_stripped(self):
        files = [{"path": "slave.mk", "additions": 1, "deletions": 0, "binary": False}]
        self.assertFalse(path_bag_usable("fetch_pull_head_two_dot"))
        self.assertEqual(files_for_model(files, "fetch_pull_head_two_dot"), [])
        self.assertEqual(files_for_model(files, "fetch_pull_head_merge_base"), files)

    def test_parse_numstat(self):
        raw = "1\t2\tfoo.py\n-\t-\tbin.dat\n"
        rows = parse_numstat(raw)
        self.assertTrue(rows[1]["binary"])


if __name__ == "__main__":
    unittest.main()
