#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import unittest
import urllib.error
from unittest.mock import patch

from scout_impl.eval import _http
from scout_impl.eval._http import github_token
from scout_impl.eval._io import progress_line


class ProgressLine(unittest.TestCase):
    def test_bar(self):
        self.assertIn("5/10", progress_line(5, 10, "x", width=10, unit="u"))


class GithubToken(unittest.TestCase):
    def test_env(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}):
            self.assertEqual(github_token(), "tok")


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "nope", {}, io.BytesIO(b"{}"))


class Retries(unittest.TestCase):
    def _get(self, url, codes):
        calls = []

        def urlopen(request, timeout):
            calls.append(request.full_url)
            raise _http_error(request.full_url, codes[min(len(calls), len(codes)) - 1])

        with patch.object(_http.urllib.request, "urlopen", urlopen), patch.object(_http.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _http.get(url)
        return caught.exception, len(calls)

    def test_a_rejected_github_token_fails_at_once_and_says_why(self):
        error, calls = self._get("https://api.github.com/repos/x/y/pulls", [401])
        self.assertEqual(calls, 1)
        self.assertIn("bad credentials", error.msg)

    def test_a_missing_resource_is_not_retried(self):
        error, calls = self._get("https://dev.azure.com/x/_apis/build/builds/1/timeline", [404])
        self.assertEqual((calls, error.code), (1, 404))

    def test_a_server_error_is_retried(self):
        _, calls = self._get("https://dev.azure.com/x/_apis/build/builds", [502])
        self.assertEqual(calls, 5)


if __name__ == "__main__":
    unittest.main()
