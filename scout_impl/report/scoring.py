"""Confidence, HLD section 4.9: the arithmetic is proven, and the judgement is banded apart from it.

Every D6 finding has two halves. The deterministic half is a fact about two files in a
tree Scout has already read, cited into both, and it is `proven` by construction; that is a
claim about the counting, not about whether the platforms matter. The adjudication half is
the model's judgement, and only it is banded, from four factors:

1. **Derivation.** An adjudication, so never `proven`.
2. **Evidence completeness.** The roles the question requires, against the roles of the
   evidence the answer cited *and the resolver re-read*. An answer citing only the change,
   when the question asked for the change and the platform, is incomplete.
3. **Breadth.** Platforms covered by complete answers; 25 is a stronger signal than 1.
4. **Prior.** The question's, from the detector.

| Band | When | Action |
| --- | --- | --- |
| `high` | Complete, broad, not contested, prior not weak | Reported |
| `medium` | Complete but narrow, or contested against the rule candidate | Reported while fewer than five rank higher |
| `low` | No complete answer, the model could not decide, or a weak prior | Kept in the JSON, not in the comment |

**The constants are provisional.** FR-10 wants the posting threshold set from the backtest,
and the backtest harness stopped partway (plan Section 0), so `BROAD_AT`, `WEAK_PRIOR` and
the score's shape are stated here to be tuned against it, not claimed as calibrated.
"""

import math
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

BAND_PROVEN = "proven"
BAND_HIGH = "high"
BAND_MEDIUM = "medium"
BAND_LOW = "low"
BAND_RANK = {BAND_PROVEN: 4, BAND_HIGH: 3, BAND_MEDIUM: 2, BAND_LOW: 1, None: 0}

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_RANK = {SEVERITY_HIGH: 3, SEVERITY_MEDIUM: 2, SEVERITY_LOW: 1}

BROAD_AT = 3
BREADTH_SATURATION = 16
WEAK_PRIOR = 0.5
CONTESTED_FACTOR = 0.8
MEDIUM_POSTING_LIMIT = 5


@dataclass(frozen=True)
class Confidence:
    """One adjudication's band and score, and the factors that produced them."""

    band: Optional[str]
    score: Optional[float]
    completeness: float
    breadth: int
    prior: float
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {"band": self.band, "score": self.score, "completeness": round(self.completeness, 3),
                "breadth": self.breadth, "prior": self.prior, "reason": self.reason}


def completeness(required: Sequence[str], cited: Sequence[str]) -> float:
    wanted = set(required)
    if not wanted:
        return 1.0
    return len(wanted & set(cited)) / len(wanted)


def breadth_factor(breadth: int) -> float:
    """0.5 for nothing, rising logarithmically to 1.0 at `BREADTH_SATURATION` platforms."""
    if breadth <= 0:
        return 0.5
    return 0.5 + 0.5 * min(1.0, math.log2(1 + breadth) / math.log2(1 + BREADTH_SATURATION))


def band_adjudication(
    prior: float,
    required: Sequence[str],
    answers: Sequence[Tuple[Sequence[str], int]],
    contested: bool,
    undecided: bool,
) -> Confidence:
    """Band a question's adjudication from its kept answers, each `(roles cited, platforms covered)`.

    Only answers whose cited roles are complete count towards breadth: an incomplete answer
    is kept in the JSON, but it is not evidence of anything the band should rest on.
    """
    if not answers:
        return Confidence(band=None, score=None, completeness=0.0, breadth=0, prior=prior,
                          reason="no answer survived the checks")
    scores = [completeness(required, roles) for roles, _ in answers]
    complete = [count for (roles, count), score in zip(answers, scores) if score == 1.0]
    breadth = sum(complete)
    best = max(scores)
    score = prior * best * breadth_factor(breadth) * (CONTESTED_FACTOR if contested else 1.0)
    thin = len(answers) - len(complete)
    aside = f"; {thin} other answer(s) cite too few evidence roles to count" if thin and complete else ""

    if not complete:
        missing = sorted(set(required) - set(role for roles, _ in answers for role in roles))
        band, reason = BAND_LOW, (f"no answer cites every required role ({', '.join(required)})"
                                  + (f"; none cites {', '.join(missing)}" if missing else ""))
    elif prior < WEAK_PRIOR:
        band, reason = BAND_LOW, f"the question's prior, {prior}, is weak"
    elif undecided:
        band, reason = BAND_LOW, "the model could not decide from the evidence"
    elif contested:
        band, reason = BAND_MEDIUM, f"contested against the brief's rule candidate, which stands{aside}"
    elif breadth >= BROAD_AT:
        band, reason = BAND_HIGH, f"complete evidence over {breadth} platform(s){aside}"
    else:
        band, reason = BAND_MEDIUM, f"complete evidence, but over only {breadth} platform(s){aside}"
    return Confidence(band=band, score=round(score, 3), completeness=best, breadth=breadth, prior=prior,
                      reason=reason)


def severity_for(never_built: int, benign: bool) -> str:
    """How bad the finding is if true: by how many platforms go unbuilt, unless judged benign."""
    if benign:
        return SEVERITY_LOW
    if never_built >= BROAD_AT:
        return SEVERITY_HIGH
    return SEVERITY_MEDIUM if never_built > 0 else SEVERITY_LOW


def posts(band: Optional[str], higher_ranked: int) -> bool:
    """Whether an adjudication of this band is shown in the comment (HLD 4.9's Action column)."""
    if band == BAND_HIGH:
        return True
    if band == BAND_MEDIUM:
        return higher_ranked < MEDIUM_POSTING_LIMIT
    return False
