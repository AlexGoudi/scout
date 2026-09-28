"""Adapter for `sonic-net/sonic-mgmt`, the test and lab automation repository.

The path rules and their ranking are HLD section 4.3 verbatim: playbooks and
`ansible/library/` first, then shared test infrastructure, then the data-file families,
then the pipeline, then leaf tests, then anything unrecognized, then documentation.
Documentation ranks below `other` because a `.md` change is the one class that reliably
invalidates nothing.
"""

from .base import (
    AzureDevOpsSpec,
    CalibrationSpec,
    CiSurface,
    ConstantCoverageSpec,
    EntitySource,
    FileEntitySpec,
    GitHubApiSpec,
    PathClass,
    PathKind,
    RepoAdapter,
)
from .sonic_mgmt_calibration import JOIN_TABLES

NAME = "sonic-mgmt"

ANSIBLE_CODE = PathClass(
    repo=NAME,
    id="ansible_code",
    rank=1,
    kind=PathKind.CODE,
    description="Playbooks and Ansible modules that generate minigraphs and configuration",
)
TEST_COMMON = PathClass(
    repo=NAME,
    id="test_common",
    rank=2,
    kind=PathKind.TEST,
    description="Shared test infrastructure and conftest fixtures every feature test inherits",
)
ANSIBLE_DATA = PathClass(
    repo=NAME,
    id="ansible_data",
    rank=3,
    kind=PathKind.DATA,
    description="Topology, testbed and lab-graph data families the playbooks read",
)
PIPELINE = PathClass(
    repo=NAME,
    id="pipeline",
    rank=4,
    kind=PathKind.BUILD,
    description="Azure Pipelines definitions and the PR-checker configuration",
)
FEATURE_TEST = PathClass(
    repo=NAME,
    id="feature_test",
    rank=5,
    kind=PathKind.TEST,
    description="A leaf test under a feature directory",
)
OTHER = PathClass(
    repo=NAME,
    id="other",
    rank=6,
    kind=PathKind.OTHER,
    description="Unrecognized path",
)
DOCUMENTATION = PathClass(
    repo=NAME,
    id="documentation",
    rank=7,
    kind=PathKind.DOCUMENTATION,
    description="Prose, which invalidates no artifact",
)

PATH_CLASSES = (
    ANSIBLE_CODE,
    TEST_COMMON,
    ANSIBLE_DATA,
    PIPELINE,
    FEATURE_TEST,
    OTHER,
    DOCUMENTATION,
)

# Evaluated in order, first match wins; `*` spans directory separators.
PATH_RULES = (
    ("*.md", DOCUMENTATION),
    ("docs/*", DOCUMENTATION),
    ("ansible/vars/*", ANSIBLE_DATA),
    ("ansible/files/*.csv", ANSIBLE_DATA),
    ("ansible/library/*", ANSIBLE_CODE),
    ("ansible/module_utils/*", ANSIBLE_CODE),
    ("ansible/*", ANSIBLE_CODE),
    ("conftest.py", TEST_COMMON),
    ("*/conftest.py", TEST_COMMON),
    ("tests/common/*", TEST_COMMON),
    ("tests/common2/*", TEST_COMMON),
    (".azure-pipelines/*", PIPELINE),
    ("azure-pipelines.yml", PIPELINE),
    ("tests/*", FEATURE_TEST),
)

ENTITY_SOURCES = (
    EntitySource(
        kind="topology",
        globs=("ansible/vars/topo_*.yml",),
        description="Topology definitions; the family behind the disabled_host_interfaces gap",
    ),
    EntitySource(
        kind="testbed",
        globs=("ansible/testbed.yaml", "ansible/vtestbed.yaml"),
        description="Physical and vlab testbed declarations",
    ),
    EntitySource(
        kind="test_case",
        globs=("tests/*/test_*.py",),
        description="Test modules, indexed by their pytest.mark.topology markers",
    ),
    EntitySource(
        kind="conditional_mark",
        globs=("tests/common/plugins/conditional_mark/tests_mark_conditions*.yaml",),
        description="Skip and xfail rules that silently narrow coverage",
    ),
    EntitySource(
        kind="lab_graph",
        globs=("ansible/files/sonic_*_devices.csv", "ansible/files/sonic_*_links.csv"),
        description="Device and link inventory; deferred with detector D6",
    ),
)

CI_SURFACES = (
    CiSurface(
        name="impacted-area-pr-checker",
        config_path=".azure-pipelines/impacted_area_testing/constant.py",
        description="PR_TOPOLOGY_TYPE decides which topologies the PR checkers run, and so the coverage gap",
    ),
)

# The second adapter exists to falsify the claim that the core is repo-agnostic, and it
# ships at smoke level with no detector of its own (HLD section 3). A topology is a file,
# not a directory carrying a declaration, so none of counting rules C1 to C4 has anything
# to act on here; the index reports zero for each rather than a number that looks measured.
ENTITY_MODEL = FileEntitySpec(
    kind="topology",
    family_kind="topology_type",
    glob="ansible/vars/topo_*.yml",
    name_pattern=r"^ansible/vars/topo_(?P<name>.+)\.yml$",
)

# `PR_TOPOLOGY_TYPE` names the checkers, and `PR_CHECKER_TOPOLOGY_NAME` says which topology
# each one actually runs — `t1_checker` runs `t1-lag`, not `t1`. That second mapping is
# definitive rather than architecture-qualified, so it resolves coverage instead of making
# it ambiguous, which is the distinction `JobGroup.qualifier` carries.
COVERAGE_SPEC = ConstantCoverageSpec(
    model="pr-checker-topology",
    path=".azure-pipelines/impacted_area_testing/constant.py",
    constant="PR_TOPOLOGY_TYPE",
    strip_suffix="_checker",
    alias_constant="PR_CHECKER_TOPOLOGY_NAME",
)

_GH = JOIN_TABLES["github"]
_AZ = JOIN_TABLES["azure"]
GITHUB_API = GitHubApiSpec(
    owner=_GH["owner"],
    repo=_GH["repo"],
    ref=_GH.get("ref", "origin/master"),
    user_agent=_GH.get("user_agent", "sonic-scout"),
)
AZURE_DEVOPS = AzureDevOpsSpec(
    org_url=_AZ["org_url"],
    pipeline_name=_AZ["pipeline_name"],
    definition_id=int(_AZ["definition_id"]),
    pr_builds=int(_AZ.get("pr_builds") or 3000),
    pr_timelines=int(_AZ.get("pr_timelines") or 800),
    builds_per_pr=int(_AZ.get("builds_per_pr") or 3),
    yaml_name_filter=_AZ.get("yaml_name_filter") or "",
    name_search=_AZ.get("name_search") or "",
    official_definitions=tuple(sorted((_AZ.get("official_definitions") or {}).items())),
)

ADAPTER = RepoAdapter(
    name=NAME,
    summary="SONiC test and lab automation: topologies, testbeds, playbooks and the pytest suite",
    markers=(
        ("ansible/testbed-cli.sh",),
        ("ansible/vars", "tests/common"),
    ),
    path_classes=PATH_CLASSES,
    path_rules=PATH_RULES,
    fallback=OTHER,
    entity_sources=ENTITY_SOURCES,
    ci_surfaces=CI_SURFACES,
    entity_model=ENTITY_MODEL,
    coverage_spec=COVERAGE_SPEC,
    github_api=GITHUB_API,
    azure_devops=AZURE_DEVOPS,
    calibration=CalibrationSpec(tables=JOIN_TABLES),
)
