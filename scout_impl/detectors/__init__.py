"""The detector catalog (HLD section 4.7): what Scout looks for, as data.

Each detector is a spec — trigger, question, required evidence roles, verification
method, confidence prior — plus a synthesis function that turns the static stage's result
into the rules, questions and unresolved items the brief carries. Adding one is a module
and a `register_detector` call; the agent loop is not touched (NFR-11).

Exactly one is committed for 1 October: D6, the CI coverage gap. The rest of the catalog
is in HLD section 9.2 and is deliberately absent rather than stubbed.
"""

from typing import Dict, List

from . import coverage_gap
from .base import Detector, DetectorError, Question, Rule, Unresolved

_DETECTORS: Dict[str, Detector] = {}


def register_detector(detector: Detector) -> None:
    _DETECTORS[detector.id] = detector


def available_detectors() -> List[str]:
    return sorted(_DETECTORS)


def get_detector(detector_id: str) -> Detector:
    detector = _DETECTORS.get(detector_id)
    if detector is None:
        raise DetectorError(f"Unknown detector {detector_id!r}; available: {available_detectors()}")
    return detector


def detectors_for(adapter) -> List[Detector]:
    """The detectors an adapter declares, in catalog order. Empty is a legitimate answer."""
    return [get_detector(detector_id) for detector_id in adapter.detectors]


register_detector(coverage_gap.DETECTOR)

__all__ = [
    "Detector",
    "DetectorError",
    "Question",
    "Rule",
    "Unresolved",
    "available_detectors",
    "detectors_for",
    "get_detector",
    "register_detector",
]
