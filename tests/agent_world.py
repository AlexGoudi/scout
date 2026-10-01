"""A small, complete world for the agent and report tests: two trees, a change, a brief, a model.

The brief is written by hand rather than produced by the static stage, so these tests pin
stage 2 to the brief's *contract* and not to how stage 1 happens to fill it today. It is
validated against `schemas/scout-brief-1.0.json` on every use, so it cannot drift from the
contract either. The shape mirrors the two demo cases: a shared file that three platforms
reach through symlinks, two of them declaring a family no job group builds; and one edit
repeated across three platforms whose family PR CI builds only for arm64 and armhf.
"""

import copy
import json
from typing import Any, Dict, List, Optional, Sequence, Union

from scout_impl.core.review_fixture import PairedFixtureSource
from scout_impl.diffparse import parse_diff
from scout_impl.models import ChangeSet, ChangeSetSpec, CommitInfo
from scout_impl.provider import Completion, Message, ModelSpec, Provider, ProviderError, TokenUsage, ToolSpec
from scout_impl.repos import get_adapter
from scout_impl.static.fixtures import TreeFixture
from scout_impl.static.schema import validate_brief

HEAD = "1" * 40
BASE = "2" * 40
COMMON = "device/acme/x86_64-acme_common"
PMON = f"{COMMON}/pmon_daemon_control.json"
LINK_TARGET = "../x86_64-acme_common/pmon_daemon_control.json"

DNX = "platform:acme/x86_64-acme_dnx-r0"
DNX2 = "platform:acme/x86_64-acme_dnx2-r0"
BCM = "platform:acme/x86_64-acme_bcm-r0"
PRE_AMD64 = "platform:acme/x86_64-acme_pre-r0"
PRE_ARM64 = "platform:acme/arm64-acme_pre-r0"
PRE_ARMHF = "platform:acme/armhf-acme_pre-r0"
PRESTERA = (PRE_AMD64, PRE_ARM64, PRE_ARMHF)

PIPELINE = """stages:
- stage: Build
  jobs:
  - template: .azure-pipelines/azure-pipelines-build.yml
    parameters:
      jobGroups:
      - name: broadcom
      - name: marvell-prestera-arm64
        variables:
          PLATFORM_NAME: marvell-prestera
          PLATFORM_ARCH: arm64
      - name: marvell-prestera-armhf
        variables:
          PLATFORM_NAME: marvell-prestera
          PLATFORM_ARCH: armhf
"""
ARM64_ITEM = ("      - name: marvell-prestera-arm64\n        variables:\n          PLATFORM_NAME: marvell-prestera\n"
              "          PLATFORM_ARCH: arm64")
ARMHF_ITEM = ("      - name: marvell-prestera-armhf\n        variables:\n          PLATFORM_NAME: marvell-prestera\n"
              "          PLATFORM_ARCH: armhf")

PMON_HEAD = '{\n    "skip_fancontrol": true,\n    "xcvrd": {\n        "poll_interval": 10\n    }\n}\n'
PMON_BASE = '{\n    "skip_fancontrol": true\n}\n'

DIFF = f"""diff --git a/{PMON} b/{PMON}
index 1111111..2222222 100644
--- a/{PMON}
+++ b/{PMON}
@@ -1,3 +1,6 @@
 {{
-    "skip_fancontrol": true
+    "skip_fancontrol": true,
+    "xcvrd": {{
+        "poll_interval": 10
+    }}
 }}
""" + "".join(
    f"""diff --git a/device/acme/{name}/platform_asic b/device/acme/{name}/platform_asic
index 3333333..4444444 100644
--- a/device/acme/{name}/platform_asic
+++ b/device/acme/{name}/platform_asic
@@ -1 +1 @@
-marvell
+marvell-prestera
""" for name in ("arm64-acme_pre-r0", "armhf-acme_pre-r0", "x86_64-acme_pre-r0"))


def _files(head: bool) -> Dict[str, Union[str, tuple]]:
    prestera = "marvell-prestera\n" if head else "marvell\n"
    files: Dict[str, Union[str, tuple]] = {
        "azure-pipelines.yml": PIPELINE,
        PMON: PMON_HEAD if head else PMON_BASE,
        f"{COMMON}/platform_asic": "broadcom\nbroadcom-dnx\n",
        "device/acme/x86_64-acme_dnx-r0/platform_asic": "broadcom-dnx\n",
        "device/acme/x86_64-acme_dnx2-r0/platform_asic": "broadcom-dnx\n",
        "device/acme/x86_64-acme_bcm-r0/platform_asic": "broadcom\n",
        "device/acme/x86_64-acme_dnx-r0/README.md": "A platform nobody builds.\n",
        "device/acme/x86_64-acme_pre-r0/platform_asic": prestera,
        "device/acme/arm64-acme_pre-r0/platform_asic": prestera,
        "device/acme/armhf-acme_pre-r0/platform_asic": prestera,
    }
    for name in ("x86_64-acme_dnx-r0", "x86_64-acme_dnx2-r0", "x86_64-acme_bcm-r0"):
        files[f"device/acme/{name}/pmon_daemon_control.json"] = ("120000", LINK_TARGET)
    return files


def source() -> PairedFixtureSource:
    head = TreeFixture.from_files(_files(True), repo="acme/buildimage", rev=HEAD, adapter="sonic-buildimage")
    base = TreeFixture.from_files(_files(False), repo="acme/buildimage", rev=BASE, adapter="sonic-buildimage")
    return PairedFixtureSource(head, base)


def change_set(diff: str = DIFF) -> ChangeSet:
    commit = CommitInfo(sha=HEAD, parents=[BASE], subject="Poll transceiver temperature; rename marvell",
                        files=parse_diff(diff, get_adapter("sonic-buildimage")))
    return ChangeSet(base_sha=BASE, head_sha=HEAD, spec=ChangeSetSpec(base_ref=BASE, head_ref=HEAD),
                     repo="sonic-buildimage", commits=[commit])


def _platform(entity_id: str, arch: str) -> Dict[str, Any]:
    name = entity_id.split(":", 1)[1]
    return {"id": entity_id, "kind": "platform", "members": 1, "resolved_via": "blob",
            "source": f"device/{name}/platform_asic", "arch": arch}


def _group(name: str, family: str, arch: str) -> Dict[str, Any]:
    return {"id": f"ci_job_group:{name}", "kind": "ci_job_group", "members": 0, "resolved_via": "blob",
            "source": "azure-pipelines.yml", "arch": arch, "family": family}


def _family(name: str, members: int) -> Dict[str, Any]:
    return {"id": f"asic_family:{name}", "kind": "asic_family", "members": members, "resolved_via": "blob",
            "source": "device/acme/x86_64-acme_bcm-r0/platform_asic"}


AMBIGUITY_QUESTION = {
    "id": "q-001", "rule": "BI-R3", "kind": "ambiguity", "unresolved": "u-001",
    "entities": ["asic_family:marvell-prestera", "ci_job_group:marvell-prestera-arm64",
                 "ci_job_group:marvell-prestera-armhf"],
    "ask": "u-001 carries the answer BI-R3 and BI-R4 imply for each platform declaring marvell-prestera. It stands "
           "unless you contest it for a named platform with cited counter-evidence.",
    "required_evidence": ["cause", "affected", "contract"], "prior": 0.55,
    "budget": {"tool_calls": 12, "blob_reads": 6},
}
MATERIALITY_QUESTION = {
    "id": "q-002", "rule": "BI-R1", "kind": "materiality", "entities": [DNX, DNX2],
    "ask": "No Build stage job group builds any ASIC family these 2 platform(s) declare. Does this change materially "
           "put them at risk, or does it only pass through their directories?",
    "required_evidence": ["cause", "affected"], "prior": 0.7,
    "budget": {"tool_calls": 12, "blob_reads": 6},
}


def brief(questions: Optional[Sequence[str]] = ("q-001", "q-002"), budget: Optional[Dict[str, int]] = None,
          question_budget: Optional[Dict[str, int]] = None, mode: str = "range") -> Dict[str, Any]:
    """The world's brief, with only the named questions, validated against the brief schema."""
    asked = [copy.deepcopy(q) for q in (AMBIGUITY_QUESTION, MATERIALITY_QUESTION) if q["id"] in (questions or ())]
    for question in asked:
        if question_budget is not None:
            question["budget"] = dict(question_budget)
    payload = {
        "schema_version": "1.0",
        "brief": {"id": "11111111-2222-3333-4444-555555555555", "repo": "acme/buildimage",
                  "adapter": {"name": "sonic-buildimage", "version": "1.0", "rule_pack_sha": "0123456789abcdef"},
                  "mode": mode, "base_sha": BASE if mode != "tree" else "", "head_sha": HEAD, "tree_paths": 16,
                  "measured_at": "2026-09-25T00:00:00Z", "static_duration_s": 0.05, "blobs_read": 7,
                  "status": "complete"},
        "hotspots": [{"id": "h-001", "path": PMON, "path_class": "platform_data", "change_kind": "modified",
                      "score": 0.5, "score_breakdown": {"path_class": 0.2, "entity_fanout": 0.1,
                                                        "coverage_gap": 0.2, "ambiguity": 0.0},
                      "entities": [BCM, DNX, DNX2]}],
        "entities": sorted([
            _platform(DNX, "amd64"), _platform(DNX2, "amd64"), _platform(BCM, "amd64"),
            _platform(PRE_AMD64, "amd64"), _platform(PRE_ARM64, "arm64"), _platform(PRE_ARMHF, "armhf"),
            _family("broadcom", 1), _family("broadcom-dnx", 2), _family("marvell-prestera", 3),
            _group("broadcom", "broadcom", "amd64"), _group("marvell-prestera-arm64", "marvell-prestera", "arm64"),
            _group("marvell-prestera-armhf", "marvell-prestera", "armhf"),
            {"id": "vendor:acme", "kind": "vendor", "members": 6, "resolved_via": "tree", "source": "device/acme"},
        ], key=lambda entity: entity["id"]),
        "coverage": {
            "model": "pr-build-stage", "job_groups": 3,
            "job_group_names": ["broadcom", "marvell-prestera-arm64", "marvell-prestera-armhf"],
            "parse": {"scope": "Build", "strict": True, "loose_scan_agrees": True},
            "declarations_in_tree": 7, "platforms_in_tree": 6, "excluded_as_non_platform": [COMMON],
            "kept_without_hwsku": 6,
            "affected": sorted([BCM, DNX, DNX2, *PRESTERA]), "covered": [BCM], "uncovered": [DNX, DNX2],
            "ambiguous": sorted(PRESTERA),
        },
        "rules": [
            {"id": "BI-R1", "statement": "A platform is exercised by the PR pipeline only if some Build stage job "
                                         "group builds an ASIC family it declares.", "derivation": "static",
             "citations": [{"path": "azure-pipelines.yml", "line_start": 1, "line_end": 15, "rev": "head",
                            "role": "contract"}]},
            {"id": "BI-R3", "statement": "A job group builds its family only for the architecture its PLATFORM_ARCH "
                                         "names; a group naming none builds amd64.", "derivation": "static",
             "citations": [
                 {"path": "azure-pipelines.yml", "line_start": 8, "line_end": 11, "rev": "head", "role": "contract",
                  "quote": ARM64_ITEM},
                 {"path": "azure-pipelines.yml", "line_start": 12, "line_end": 15, "rev": "head", "role": "contract",
                  "quote": ARMHF_ITEM}]},
            {"id": "BI-R4", "statement": "A platform's architecture is its directory name's prefix: x86_64 means "
                                         "amd64.", "derivation": "convention",
             "citations": [{"path": "device/acme/arm64-acme_pre-r0/platform_asic", "line_start": 1, "line_end": 1,
                            "rev": "head", "role": "contract"}]},
        ],
        "questions": asked,
        "unresolved": [{
            "id": "u-001", "kind": "family_name_arity",
            "summary": "Job group names are architecture-qualified; the platforms declare the unqualified family.",
            "candidates": [{"rule": "string-equality", "uncovered": 5}, {"rule": "architecture-aware", "uncovered": 3}],
            "entities": list(AMBIGUITY_QUESTION["entities"]), "adjudicated_by": "agent",
            "rule_candidate": {
                "rules": ["BI-R3", "BI-R4"], "derivation": "naming-convention-inference",
                "statement": "Under BI-R3 and BI-R4, 2 of the 3 ambiguous platform(s) are built for their own CPU "
                             "architecture and 1 is not, so 3 of the 6 affected platform(s) are never built.",
                "uncovered": 3,
                "platforms": [
                    {"entity": PRE_ARM64, "arch": "arm64", "families": ["marvell-prestera"], "resolution": "covered",
                     "job_groups": ["ci_job_group:marvell-prestera-arm64"]},
                    {"entity": PRE_ARMHF, "arch": "armhf", "families": ["marvell-prestera"], "resolution": "covered",
                     "job_groups": ["ci_job_group:marvell-prestera-armhf"]},
                    {"entity": PRE_AMD64, "arch": "amd64", "families": ["marvell-prestera"],
                     "resolution": "uncovered", "job_groups": []},
                ],
            },
        }] if "q-001" in (questions or ()) else [],
        "entity_closure": True,
        "budget": dict(budget or {"max_tool_calls": 60, "max_blob_reads": 30, "max_input_tokens": 120000}),
    }
    validate_brief(payload)
    return payload


def spec() -> ModelSpec:
    return ModelSpec(provider="scripted", model_id="scripted-model-2026-09-25", max_output_tokens=384)


class ScriptedProvider(Provider):
    """Plays back a queue of replies, one per call, recording every request it was sent."""

    def __init__(self, replies: Sequence[Any] = (), preflight_error: Optional[str] = None) -> None:
        super().__init__(spec())
        self.replies: List[Any] = list(replies)
        self.requests: List[Dict[str, Any]] = []
        self.preflight_error = preflight_error
        self.preflights = 0

    def preflight(self) -> None:
        self.preflights += 1
        if self.preflight_error:
            raise ProviderError(self.preflight_error)

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        self.requests.append({"messages": [message.to_dict() for message in messages],
                              "schema": tools[0].parameters if tools else None, "timeout_s": timeout_s})
        if not self.replies:
            raise AssertionError("the scripted provider ran out of replies: the agent asked more than expected")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(text=text, usage=TokenUsage(input_tokens=500, output_tokens=40), model_id=spec().model_id)


class RefusingProvider(ScriptedProvider):
    """A provider that fails the test if it is ever touched, preflight included."""

    def preflight(self) -> None:
        raise AssertionError("the provider was preflighted, but this run must cost no model call at all")

    def _complete(self, messages: List[Message], tools: List[ToolSpec], timeout_s: Optional[float]) -> Completion:
        raise AssertionError("the provider was called, but this run must cost no model call at all")


def answers(*items: Dict[str, Any]) -> Dict[str, Any]:
    return {"action": "answer", "answers": list(items)}


def ambiguity(group: str, covered: bool, job_group: str = "", cite: Sequence[str] = ("E1", "E3", "E4"),
              reason: str = "per the rule") -> Dict[str, Any]:
    return {"group": group, "reason": reason, "covered": covered, "job_group": job_group, "cite": list(cite)}


def materiality(group: str, verdict: str = "material", cite: Sequence[str] = ("E1", "E3"),
                reason: str = "the change adds an xcvrd option every linked platform loads") -> Dict[str, Any]:
    return {"group": group, "reason": reason, "verdict": verdict, "cite": list(cite)}
