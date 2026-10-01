"""D6, the CI coverage gap — the one committed detector.

The deterministic half is settled before this module runs: changed paths have already
resolved to platforms, platforms to ASIC families, families to Build job groups, and the
result is three disjoint sets. What is left is to write down, for the agent stage, which
invariants that arithmetic rests on and which question it cannot answer.

There is exactly one such question and it is `u-001`. Job group names are
architecture-qualified — `marvell-prestera-arm64`, `marvell-prestera-armhf`,
`aspeed-arm64` — while the platforms declare the unqualified family, and whether a given
platform matches depends on an architecture `platform_asic` never states.

**The static rule decides; the agent confirms or contests.** Both candidate totals go into
the brief, and so does the architecture rule's answer for every individual platform, in
`rule_candidate`. The agent's job is not to re-derive that set — it is to contest a named
platform's answer with cited counter-evidence, or to leave it standing. The entities stay
in `coverage.ambiguous` either way, because a convention read off a directory name is
weaker than a declaration and the report bands it accordingly.

No verification method is registered, and that is the honest answer rather than a gap:
the claim is a fact about two files in a tree Scout has already fetched, shipped with
citations into both, and re-running the lookup would return the same answer over the same
bytes.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..static.coverage import (
    ARCHITECTURE_AWARE,
    RESOLUTION_COVERED,
    RESOLUTION_UNCOVERED,
    RESOLUTION_UNDETERMINED,
    STRING_EQUALITY,
    CoverageResult,
)
from ..static.pipeline import CoverageModel
from ..static.platforms import Entity, EntityIndex
from .base import (
    DERIVATION_CONVENTION,
    QUESTION_AMBIGUITY,
    QUESTION_MATERIALITY,
    ROLE_AFFECTED,
    ROLE_CAUSE,
    ROLE_CONTRACT,
    BAND_PROVEN,
    VERIFY_NONE,
    Citation,
    Detector,
    Question,
    Rule,
    Synthesis,
    Unresolved,
)

# One question, one adjudication, a handful of files. The budget is deliberately small:
# the expensive work was done deterministically and the agent is paying only to decide
# what the static stage refused to guess at.
QUESTION_BUDGET = {"tool_calls": 12, "blob_reads": 6}
AMBIGUITY_PRIOR = 0.55
DETECTOR_PRIOR = 0.70

# Rules that exist only to be the terms `u-001` is adjudicated under. A brief with nothing
# ambiguous states neither: there is no question for them to bound, and a rule the brief
# cannot cite an instance of is worse than no rule at all.
AMBIGUITY_RULES = ("architecture_qualified_job_groups", "platform_directory_prefix")
AMBIGUITY_RULE = "architecture_qualified_job_groups"


def synthesize(
    index: EntityIndex,
    model: CoverageModel,
    coverage: CoverageResult,
    rules: Tuple[Any, ...],
    rev: str = "head",
) -> Synthesis:
    """Turn the coverage result into the brief's rules, questions and unresolved items."""
    triggered = bool(coverage.affected)
    stated_rules: List[Rule] = []
    architecture_rules: List[str] = []
    for spec in rules:
        conditional = spec.citation_kind in AMBIGUITY_RULES
        if conditional and not coverage.ambiguous:
            continue
        rule = _rule(spec, index, model, coverage, rev)
        if conditional and not rule.citations:
            continue
        stated_rules.append(rule)
        if conditional:
            architecture_rules.append(rule.id)
    stated = tuple(stated_rules)
    adjudication_rule = next(
        (spec.id for spec in rules if spec.citation_kind == AMBIGUITY_RULE and spec.id in architecture_rules),
        stated[0].id if stated else "BI-R1",
    )

    unresolved: List[Unresolved] = []
    questions: List[Question] = []

    if coverage.ambiguous:
        families = sorted({family for names in coverage.ambiguous_families.values() for family in names})
        groups = sorted({name for family in families for name in model.alias_families.get(family, ())})
        entities = tuple(
            [f"{index.family_kind}:{family}" for family in families]
            + [f"ci_job_group:{name}" for name in groups]
        )
        counts = coverage.uncovered_under

        unresolved.append(
            Unresolved(
                id="u-001",
                kind="family_name_arity",
                summary=(
                    "Job group names are architecture-qualified; some platforms declare the unqualified "
                    "family name. Which answer holds depends on each platform's architecture, which its "
                    "platform_asic file does not state."
                ),
                candidates=(
                    {"rule": STRING_EQUALITY, "uncovered": counts[STRING_EQUALITY]},
                    {"rule": ARCHITECTURE_AWARE, "uncovered": counts[ARCHITECTURE_AWARE]},
                ),
                entities=entities,
                rule_candidate=(_rule_candidate(index, model, coverage, architecture_rules)
                                if len(architecture_rules) == len(AMBIGUITY_RULES) else None),
            )
        )
        questions.append(
            Question(
                id="q-001",
                rule=adjudication_rule,
                entities=entities,
                ask=(
                    f"The architecture rule has already decided all {len(coverage.ambiguous)} platform(s) "
                    f"declaring {', '.join(families)} against the architecture-qualified job groups "
                    f"{', '.join(groups)}; its per-platform answers are in this item's rule_candidate. "
                    f"Confirm it, or contest a specific platform's answer with cited counter-evidence — a "
                    f"pipeline line or a platform file showing the architecture is not what the directory "
                    f"name implies. Do not re-derive the whole set."
                ),
                required_evidence=(ROLE_CAUSE, ROLE_AFFECTED, ROLE_CONTRACT),
                prior=AMBIGUITY_PRIOR,
                budget=dict(QUESTION_BUDGET),
                kind=QUESTION_AMBIGUITY,
                unresolved="u-001",
            )
        )

    if coverage.uncovered:
        questions.append(
            Question(
                id=f"q-{len(questions) + 1:03d}",
                rule=stated[0].id if stated else "BI-R1",
                entities=tuple(f"{index.kind}:{item}" for item in coverage.uncovered),
                ask=(
                    f"No job group in either build stage, BuildVS or Build, builds any ASIC family these "
                    f"{len(coverage.uncovered)} platform(s) declare. Does this change materially put them at "
                    f"risk, or does it only pass through their directories? Cite the change as cause and each "
                    f"platform's declaration as affected."
                ),
                required_evidence=(ROLE_CAUSE, ROLE_AFFECTED),
                prior=DETECTOR_PRIOR,
                budget=dict(QUESTION_BUDGET),
                kind=QUESTION_MATERIALITY,
            )
        )

    return Synthesis(
        rules=stated,
        questions=tuple(questions),
        unresolved=tuple(unresolved),
        triggered=triggered,
    )


def _rule(spec: Any, index: EntityIndex, model: CoverageModel, coverage: CoverageResult, rev: str) -> Rule:
    if spec.citation_kind == "architecture_qualified_job_groups":
        citations = _job_group_citations(model, coverage, rev)
    elif spec.citation_kind == "platform_directory_prefix":
        citations = _prefix_citations(index, coverage, rev)
    else:
        citation = _citation(spec.citation_kind, index, model, rev)
        citations = (citation,) if citation else ()
    return Rule(
        id=spec.id,
        statement=spec.statement,
        derivation=spec.derivation,
        citations=citations,
    )


def _rule_candidate(
    index: EntityIndex,
    model: CoverageModel,
    coverage: CoverageResult,
    rule_ids: Sequence[str],
) -> Dict[str, Any]:
    """The answer BI-R3 and BI-R4 imply for each ambiguous entity, and the count it implies.

    A group covers an entity when it builds a family the entity declares for the entity's
    own architecture. An entity whose name follows no known prefix is `undetermined`
    rather than guessed, and counts towards neither side.
    """
    platforms: List[Dict[str, Any]] = []
    implied = coverage.resolution_counts
    for entity_id in coverage.ambiguous:
        entity = index.by_id(entity_id)
        groups = coverage.ambiguous_job_groups.get(entity_id, ())
        platforms.append({
            "entity": f"{index.kind}:{entity_id}",
            "arch": (entity.arch if entity is not None else "") or "unknown",
            "families": list(entity.families if entity is not None else ()),
            "resolution": coverage.ambiguous_resolution.get(entity_id, RESOLUTION_UNDETERMINED),
            "job_groups": [f"ci_job_group:{name}" for name in groups],
        })

    # The same number the `architecture-aware` candidate reports, because it is the same
    # call; computing it separately lets the two drift apart.
    total = coverage.uncovered_under[ARCHITECTURE_AWARE]
    undetermined = (f" and {implied[RESOLUTION_UNDETERMINED]} follow no known prefix"
                    if implied[RESOLUTION_UNDETERMINED] else "")
    return {
        "rules": list(rule_ids),
        "candidate": ARCHITECTURE_AWARE,
        "derivation": DERIVATION_CONVENTION,
        "statement": (
            f"Under {' and '.join(rule_ids)}, {implied[RESOLUTION_COVERED]} of the {len(platforms)} ambiguous "
            f"platform(s) are built for their own CPU architecture, {implied[RESOLUTION_UNCOVERED]} are not"
            f"{undetermined}, so {total} of the {len(coverage.affected)} affected platform(s) are never built "
            f"by PR CI."
        ),
        "uncovered": total,
        "platforms": platforms,
    }


def _job_group_citations(model: CoverageModel, coverage: CoverageResult, rev: str) -> Tuple[Citation, ...]:
    """The architecture-qualified groups the ambiguity turns on, then the default they depart from."""
    families = {family for names in coverage.ambiguous_families.values() for family in names}
    citations = [
        Citation(path=model.path, line_start=group.line, line_end=max(group.line, group.line_end), rev=rev,
                 role=ROLE_CONTRACT, quote=model.quote(model.path, group.line, max(group.line, group.line_end)))
        for group in sorted(model.job_groups, key=lambda item: item.line)
        if group.is_arch_qualified and group.family in families and group.line
    ]
    if not citations:
        return ()
    for path, start, end in model.arch_contract:
        citations.append(Citation(path=path, line_start=start, line_end=end, rev=rev,
                                  role=ROLE_CONTRACT, quote=model.quote(path, start, end)))
    return tuple(citations)


def _prefix_citations(index: EntityIndex, coverage: CoverageResult, rev: str) -> Tuple[Citation, ...]:
    """Declarations whose directory names carry the convention, one per architecture.

    The tree states the convention nowhere a free read reaches, so the evidence is the
    paths themselves, and the strongest form of it is one machine present under two
    prefixes. That pair is preferred when the ambiguous set holds one. Symlinked
    declarations are skipped because line 1 of a link is its target, not a family.
    No quote is carried: the lines were read into a set, and stage 2 reads them afresh.
    """
    candidates: List[Entity] = sorted(
        (entity for entity in (index.by_id(item) for item in coverage.ambiguous)
         if entity is not None and entity.arch and not entity.link_hops),
        key=lambda entity: entity.id,
    )
    machines: Dict[Tuple[str, str], List[Entity]] = {}
    for entity in candidates:
        machine = entity.name.split("-", 1)[1] if "-" in entity.name else entity.name
        machines.setdefault((entity.vendor, machine), []).append(entity)

    chosen: List[Entity] = []
    for key in sorted(machines):
        if len({entity.arch for entity in machines[key]}) > 1:
            chosen.extend(machines[key])
            break
    for entity in candidates:
        if entity.arch not in {item.arch for item in chosen}:
            chosen.append(entity)

    return tuple(
        Citation(path=entity.declaration_path, line_start=1, line_end=1, rev=rev, role=ROLE_CONTRACT)
        for entity in sorted(chosen, key=lambda item: item.id)
    )


def _citation(kind: str, index: EntityIndex, model: CoverageModel, rev: str) -> Optional[Citation]:
    """Where in the tree the rule is written down. A rule with none is an adapter bug."""
    if kind == "coverage_model":
        first = min((group.line for group in model.job_groups if group.line), default=0)
        last = max((group.line for group in model.job_groups if group.line), default=0)
        start = min([line for line in model.stage_lines.values() if line] + [first]) if model.stage_lines else first
        return Citation(path=model.path, line_start=start, line_end=last, rev=rev, role=ROLE_CONTRACT)

    if kind == "multi_family_declaration":
        for declaration in index.multi_family:
            return Citation(
                path=declaration.resolved_path or declaration.path,
                line_start=1,
                line_end=len(declaration.families),
                rev=rev,
                role=ROLE_CONTRACT,
            )
        # No dual-family declaration in this tree, so cite any declaration as the shape
        # the rule is about rather than emitting a rule with no citation.
        for declaration in index.declarations:
            if declaration.resolved:
                return Citation(
                    path=declaration.resolved_path or declaration.path,
                    line_start=1,
                    line_end=len(declaration.families),
                    rev=rev,
                    role=ROLE_CONTRACT,
                )
    return None


DETECTOR = Detector(
    id="D6",
    name="CI coverage gap",
    repos=("sonic-buildimage",),
    trigger="A changed path resolves to one or more platforms or HWSKUs",
    question="Which platforms does this change reach, and does the PR pipeline build or exercise them?",
    required_evidence=(ROLE_CAUSE, ROLE_AFFECTED, ROLE_CONTRACT),
    verification=VERIFY_NONE,
    prior=DETECTOR_PRIOR,
    band=BAND_PROVEN,
    synthesize=synthesize,
)
