"""What a detector is: a data-driven spec plus one synthesis function.

The spec side is the catalog row from HLD section 4.7 — trigger, question, the evidence
roles a valid answer needs, how the claim is verified, and its confidence prior. The
synthesis side is the only code a detector owns: given what the static stage established,
produce the rules the agent must test, the questions it may ask, and the facts that are
under-determined. Everything downstream of that — closure, citation resolution, scoring —
is shared, which is what makes "adding a detector must not require touching the agent
loop" (NFR-11) true rather than aspirational.

The three output types mirror the brief's blocks one for one, so the builder serializes
them without a translation layer that could drift from the schema.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

# Evidence roles, from HLD section 4.6.
ROLE_CAUSE = "cause"
ROLE_AFFECTED = "affected"
ROLE_PRECEDENT = "precedent"
ROLE_CONTRACT = "contract"

VERIFY_NONE = "none"

BAND_PROVEN = "proven"

# What a question asks the agent to do, so stage 2 dispatches on data rather than on a
# question id whose numbering depends on which sets happened to be non-empty.
QUESTION_AMBIGUITY = "ambiguity"
QUESTION_MATERIALITY = "materiality"

DERIVATION_CONVENTION = "naming-convention-inference"


class DetectorError(RuntimeError):
    """A detector is unknown, or was asked for something it does not model."""


@dataclass(frozen=True)
class Citation:
    """One file-and-line reference. FR-7: a claim without one is dropped, not caveated."""

    path: str
    line_start: int
    line_end: int
    rev: str = "head"
    role: str = ROLE_CONTRACT
    quote: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "rev": self.rev,
            "role": self.role,
        }
        if self.quote:
            payload["quote"] = self.quote
        return payload


@dataclass(frozen=True)
class Rule:
    """An invariant stated as fact, with a citation into the tree that establishes it."""

    id: str
    statement: str
    derivation: str
    citations: Tuple[Citation, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "derivation": self.derivation,
            "citations": [item.to_dict() for item in self.citations],
        }


@dataclass(frozen=True)
class Question:
    """One bounded thing the agent is asked, with the budget it may spend answering."""

    id: str
    rule: str
    entities: Tuple[str, ...]
    ask: str
    required_evidence: Tuple[str, ...]
    prior: float
    budget: Dict[str, int]
    kind: str = ""
    unresolved: str = ""

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "rule": self.rule,
            "entities": list(self.entities),
            "ask": self.ask,
            "required_evidence": list(self.required_evidence),
            "prior": self.prior,
            "budget": dict(self.budget),
        }
        if self.kind:
            payload["kind"] = self.kind
        if self.unresolved:
            payload["unresolved"] = self.unresolved
        return payload


@dataclass(frozen=True)
class Unresolved:
    """An under-determined fact, carrying both candidate answers. Never silently picked."""

    id: str
    kind: str
    summary: str
    candidates: Tuple[Dict[str, Any], ...]
    entities: Tuple[str, ...]
    adjudicated_by: str = "agent"
    # The answer the stated rules imply, per entity, marked as the inference it is. It
    # does not resolve the item: the entities stay ambiguous and the agent confirms or
    # contests it, and stage 2 compares the two rather than letting either win silently.
    rule_candidate: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "candidates": [dict(item) for item in self.candidates],
            "entities": list(self.entities),
            "adjudicated_by": self.adjudicated_by,
        }
        if self.rule_candidate is not None:
            payload["rule_candidate"] = self.rule_candidate
        return payload


@dataclass(frozen=True)
class Synthesis:
    """What one detector contributes to a brief."""

    rules: Tuple[Rule, ...] = ()
    questions: Tuple[Question, ...] = ()
    unresolved: Tuple[Unresolved, ...] = ()
    triggered: bool = False


@dataclass(frozen=True)
class Detector:
    """One catalog row, plus the function that turns a static result into brief content."""

    id: str
    name: str
    repos: Tuple[str, ...]
    trigger: str
    question: str
    required_evidence: Tuple[str, ...]
    verification: str
    prior: float
    band: str
    synthesize: Callable[..., Synthesis] = field(repr=False, default=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "repos": list(self.repos),
            "trigger": self.trigger,
            "question": self.question,
            "required_evidence": list(self.required_evidence),
            "verification": self.verification,
            "prior": self.prior,
            "band": self.band,
        }
