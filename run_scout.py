#!/usr/bin/env python3
"""Entrypoint for the SONiC Scout offline utilities."""

from scout_impl.cli import run


if __name__ == "__main__":
    raise SystemExit(run())
