"""The conformance suite: model-free, offline assertions over pinned trees.

This file exists so that the directory is a package and its `conftest.py` is imported as
`conformance.conftest` rather than as a second top-level `conftest`, which would shadow
`tests/conftest.py` and break the `from conftest import TempGitRepo` the unit suite uses.
"""
