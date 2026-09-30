#!/usr/bin/env python3
"""Entrypoint for the SONiC Scout offline utilities."""

from scout_impl.cli import run
from scout_impl.mining_cli import build_parser, main

__all__ = ("build_parser", "main", "run")

if __name__ == "__main__":
    raise SystemExit(run())
