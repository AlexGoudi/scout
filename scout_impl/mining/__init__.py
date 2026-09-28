"""Per-commit record extraction from a local Git clone.

The record is the unit the dataset and the models are built from. It holds only facts
intrinsic to one commit; anything that needs later history lives in ``scout_impl.dataset``.
"""

EXTRACTOR_VERSION = "1.1.0"
