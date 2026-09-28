"""Adapter for `sonic-net/sonic-buildimage`, the repository that builds the image.

A first cut. The rules below were derived by walking a checkout, and a fuller survey of
the repository is in flight separately; the table is meant to be corrected in place.

Ranking follows the same principle as the `sonic-mgmt` adapter — breadth of blast
radius, not severity — which for this repository orders as: build rules reach every
image on every platform, image templates and init config reach every image at boot,
platform data reaches the HWSKUs that share it, a container reaches whichever images
install it, and a component source reaches its own component. Two placements are worth
stating because the intuitive order is different:

- `platform_data` outranks `container` and `component_source` even though a single
  `device/<vendor>/<platform>/<hwsku>/` file looks narrow, because the vendor-wide and
  `device/common/` files in the same class fan out across hundreds of HWSKUs, and
  because this is the class where a change reaches hardware.
- `version_pin` ranks below `platform_data` despite technically affecting every image.
  The files under `files/build/versions/` are machine-generated and churn in bulk, so
  ranking them by breadth would swamp the prefilter. A pin moving out of step with the
  source it pins is a detector question, not a ranking one.
"""

from .base import (
    CiSurface,
    DirectoryEntitySpec,
    EntitySource,
    PathClass,
    PathKind,
    PipelineCoverageSpec,
    RepoAdapter,
    RuleSpec,
)

NAME = "sonic-buildimage"

BUILD_RULE = PathClass(
    repo=NAME,
    id="build_rule",
    rank=1,
    kind=PathKind.BUILD,
    description="Make rules and build scripts that decide what every image contains",
)
IMAGE_TEMPLATE = PathClass(
    repo=NAME,
    id="image_template",
    rank=2,
    kind=PathKind.DATA,
    description="Jinja templates rendered into every built image",
)
IMAGE_CONFIG = PathClass(
    repo=NAME,
    id="image_config",
    rank=3,
    kind=PathKind.CODE,
    description="Init, installer and host config that runs on every device at boot",
)
PLATFORM_DATA = PathClass(
    repo=NAME,
    id="platform_data",
    rank=4,
    kind=PathKind.DATA,
    description="Per-platform and per-HWSKU data: the surface where a change reaches hardware",
)
VERSION_PIN = PathClass(
    repo=NAME,
    id="version_pin",
    rank=5,
    kind=PathKind.DATA,
    description="Pinned package and image versions; machine-generated and high churn",
)
CONTAINER = PathClass(
    repo=NAME,
    id="container",
    rank=6,
    kind=PathKind.BUILD,
    description="A docker image definition and its supervisor and start scripts",
)
COMPONENT_SOURCE = PathClass(
    repo=NAME,
    id="component_source",
    rank=7,
    kind=PathKind.CODE,
    description="In-tree component sources and the submodule pointers under src/",
)
PIPELINE = PathClass(
    repo=NAME,
    id="pipeline",
    rank=8,
    kind=PathKind.BUILD,
    description="Azure Pipelines and GitHub Actions definitions",
)
OTHER = PathClass(
    repo=NAME,
    id="other",
    rank=9,
    kind=PathKind.OTHER,
    description="Unrecognized path",
)
DOCUMENTATION = PathClass(
    repo=NAME,
    id="documentation",
    rank=10,
    kind=PathKind.DOCUMENTATION,
    description="Prose, which invalidates no artifact",
)

PATH_CLASSES = (
    BUILD_RULE,
    IMAGE_TEMPLATE,
    IMAGE_CONFIG,
    PLATFORM_DATA,
    VERSION_PIN,
    CONTAINER,
    COMPONENT_SOURCE,
    PIPELINE,
    OTHER,
    DOCUMENTATION,
)

# Evaluated in order, first match wins; `*` spans directory separators.
PATH_RULES = (
    ("*.md", DOCUMENTATION),
    ("doc/*", DOCUMENTATION),
    ("docs/*", DOCUMENTATION),
    # Pins live under files/build/versions/ today; the bare versions/ spelling is carried
    # because the directory has moved before and the rule is cheaper than a missed class.
    ("files/build/versions/*", VERSION_PIN),
    ("versions/*", VERSION_PIN),
    ("files/build_templates/*", IMAGE_TEMPLATE),
    ("files/build_scripts/*", BUILD_RULE),
    ("files/*", IMAGE_CONFIG),
    ("installer/*", IMAGE_CONFIG),
    ("*.mk", BUILD_RULE),
    ("*.dep", BUILD_RULE),
    ("Makefile*", BUILD_RULE),
    ("rules/*", BUILD_RULE),
    ("scripts/*", BUILD_RULE),
    ("sonic-slave-*/*", BUILD_RULE),
    ("onie-*", BUILD_RULE),
    ("build_*.sh", BUILD_RULE),
    ("functions.sh", BUILD_RULE),
    ("get_docker-base.sh", BUILD_RULE),
    ("push_docker.sh", BUILD_RULE),
    ("device/*", PLATFORM_DATA),
    ("platform/*", PLATFORM_DATA),
    ("dockers/*", CONTAINER),
    ("src/*", COMPONENT_SOURCE),
    (".gitmodules", COMPONENT_SOURCE),
    (".azure-pipelines/*", PIPELINE),
    (".github/*", PIPELINE),
    ("azure-pipelines.yml", PIPELINE),
)

ENTITY_SOURCES = (
    EntitySource(
        kind="hwsku",
        globs=("device/*/port_config.ini",),
        description="HWSKU directories, identified by the port map every one of them carries",
    ),
    EntitySource(
        kind="platform",
        globs=("device/*/platform.json", "device/*/platform_asic"),
        description="Platform directories and the ASIC they declare",
    ),
    EntitySource(
        kind="qos_profile",
        globs=("device/*/pg_profile_lookup.ini", "device/*/qos.json.j2", "device/*/buffers.json.j2"),
        description="Per-HWSKU QoS and buffer data, which only a subset of HWSKUs declares",
    ),
    EntitySource(
        kind="container",
        globs=("dockers/*/Dockerfile.j2",),
        description="Docker images built into the appliance",
    ),
    EntitySource(
        kind="build_rule",
        globs=("rules/*.mk", "platform/*/*.mk"),
        description="Make rules naming the packages and platforms a build produces",
    ),
    EntitySource(
        kind="version_pin",
        globs=("files/build/versions/*/versions-*",),
        description="Pinned package versions per build target",
    ),
    EntitySource(
        kind="image_template",
        globs=("files/build_templates/*.j2",),
        description="Templates rendered into the image at build time",
    ),
    EntitySource(
        kind="submodule",
        globs=(".gitmodules",),
        description="Submodule pointers; a bump here changes component source without a diff in-tree",
    ),
)

CI_SURFACES = (
    CiSurface(
        name="pr-build",
        config_path=".azure-pipelines/azure-pipelines-build.yml",
        description="Which platform and architecture combinations a pull request actually builds",
    ),
    CiSurface(
        name="official-build",
        config_path=".azure-pipelines/official-build.yml",
        description="The wider platform matrix built after merge, and so not a pre-merge signal",
    ),
)

# The counting rules of HLD section 4.3.1, as data. C1 is `max_link_hops`, C2 is implied
# by the extractor parsing `declaration_file` to a set, C3 is `shared_suffix`, and C4 is
# the deliberate absence of `hwsku_markers` from the exclusion test — they are recorded so
# the brief can publish how many platforms own no HWSKU and were kept anyway, never to
# decide whether a directory is a platform. `device/arista/x86_64-arista_7800_sup` is the
# fixture that holds those two apart: a chassis supervisor owning no HWSKU, which the
# plausible-looking rule would have discarded along with 29 other supervisors and fabric
# cards. C5 lives in the coverage query, not here.
#
# `arch_prefixes` is the ONIE platform string, `<arch>-<vendor>_<machine>-r<rev>`, read
# as the Debian architecture the pipeline's PLATFORM_ARCH uses. It is the one input to
# rule BI-R4 and it is a convention: `platform_asic` states no architecture, which is why
# the answer it implies goes into the brief as a candidate rather than as a fact.
#
# C6, `alias_directories` and `reverse_reach`, is the rule that `device/` shares data by
# git symlink and that both directions of a link carry meaning. Outbound: three entity
# directories are themselves mode-120000 links to a sibling — `x86_64-arista_7280cr3k_32d4`,
# `x86_64-arista_7280cr3k_32p4` and `x86_64-accton_as9516bf_32d-r0` — and each is a distinct
# ONIE platform, a real deployable box running its target's data. A tree listing records a
# symlinked directory as one entry with nothing under it, so without this rule they are
# invisible; it is also why a filesystem glob, which follows them, counts three more
# platforms than a count taken from git. Inbound: 1,853 symlinks under `device/` point at
# files in other platforms' directories and in the shared ones, so a change to one of those
# files is a change to every platform linking at it. C3 keeps the shared directories out of
# the platform *count* and C6 puts their *contents* back into every platform's reach; the
# two are not in tension, they answer different questions.
ENTITY_MODEL = DirectoryEntitySpec(
    kind="platform",
    family_kind="asic_family",
    root="device",
    declaration_file="platform_asic",
    shared_suffix="_common",
    hwsku_markers=("port_config.ini", "hwsku.json"),
    identity_marker="default_sku",
    arch_prefixes=(("x86_64", "amd64"), ("arm64", "arm64"), ("armhf", "armhf")),
    alias_directories=True,
    reverse_reach=True,
)

# Both stages that schedule `.azure-pipelines/azure-pipelines-build.yml` are in scope,
# because both run on a pull request and both therefore build. Scoping to the stage
# literally named `Build` drops `vs`, `vpp` and `alpinevs` and moves the headline from
# 196/88 to 194/90; HLD section 6.2.2 names the scope as `Build` while HLD section 6.2
# publishes the nine-group list that only `Build` plus `BuildVS` produces. The nine-group
# list is the normative one, so it wins, and the divergence is recorded here next to the
# code that depends on it. Every name in `stages` must exist or the parse fails loudly.
COVERAGE_SPEC = PipelineCoverageSpec(
    model="pr-build-stage",
    path="azure-pipelines.yml",
    stages=("Build", "BuildVS"),
    group_parameter="jobGroups",
    family_variable="PLATFORM_NAME",
    arch_variable="PLATFORM_ARCH",
    default_arch="amd64",
)

RULES = (
    RuleSpec(
        id="BI-R1",
        statement=(
            "A platform is exercised by the PR pipeline only if a job group in one of its build stages — "
            "BuildVS or Build — builds an ASIC family that the platform declares in its platform_asic "
            "file."
        ),
        citation_kind="coverage_model",
    ),
    RuleSpec(
        id="BI-R2",
        statement=(
            "A platform_asic file may declare more than one ASIC family, one per line. A platform is "
            "covered if any declared family is built."
        ),
        citation_kind="multi_family_declaration",
    ),
    # BI-R3 and BI-R4 are stated only when a brief has an ambiguous set to adjudicate:
    # they are the rules `u-001` is decided under, and nothing else rests on them.
    RuleSpec(
        id="BI-R3",
        statement=(
            "A job group in either build stage, BuildVS or Build, builds its ASIC family only for the "
            "CPU architecture its PLATFORM_ARCH variable names, and a group naming none builds amd64. By the naming "
            "convention this makes explicit, a group named <family>-arm64 builds only arm64 platforms, "
            "<family>-armhf only armhf platforms, and a group named exactly <family> builds amd64. A "
            "platform is covered only if some group builds a family it declares for the platform's own "
            "CPU architecture."
        ),
        citation_kind="architecture_qualified_job_groups",
    ),
    RuleSpec(
        id="BI-R4",
        statement=(
            "A platform's CPU architecture is the prefix of its device directory name before the first "
            "dash, following the ONIE platform string <arch>-<vendor>_<machine>-r<rev>: x86_64 means "
            "amd64, and arm64 and armhf mean themselves. platform_asic states no architecture, so this is "
            "a naming convention rather than a declaration."
        ),
        citation_kind="platform_directory_prefix",
        derivation="convention",
    ),
)

ADAPTER = RepoAdapter(
    name=NAME,
    summary="SONiC image build: make rules, platform data, docker images and component sources",
    markers=(
        ("slave.mk", "Makefile.work"),
        ("rules/config", "platform"),
    ),
    path_classes=PATH_CLASSES,
    path_rules=PATH_RULES,
    fallback=OTHER,
    entity_sources=ENTITY_SOURCES,
    ci_surfaces=CI_SURFACES,
    entity_model=ENTITY_MODEL,
    coverage_spec=COVERAGE_SPEC,
    rules=RULES,
    detectors=("D6",),
)
