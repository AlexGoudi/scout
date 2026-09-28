"""Stage 1: the deterministic static analyzer (HLD section 4.3).

No model, no credential, no network beyond the fetch stage 0 already performed. Every
module here is a program with a written specification and a conformance suite, which is
what replaces a verification stage for the committed detector (HLD section 4.8).

This package deliberately imports nothing at package level. The repo adapters declare
their static-analysis knowledge as data in `scout_impl/repos/base.py`, and the mechanisms
here read those declarations back — so `scout_impl.static.platforms` imports
`scout_impl.repos`, and an eager re-export here would close that into an import cycle the
first time an adapter module is loaded. Import the submodule you want.
"""
