import json
import re

import pytest

from scout_impl.mining.taxonomy import TaxonomyError, classify, file_class, glob_to_regex, load_taxonomy


@pytest.fixture(scope="module")
def taxonomy():
    return load_taxonomy()


def matches(pattern, path):
    return re.fullmatch(glob_to_regex(pattern), path) is not None


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("*.md", "README.md", True),
        ("*.md", "a/b/README.md", True),
        ("rules/**", "rules/x.mk", True),
        ("rules/**", "src/rules/x.mk", False),
        ("**/tests/**", "tests/t.py", True),
        ("**/tests/**", "src/a/tests/t.py", True),
        ("**/tests/**", "src/a/tests_x/t.py", False),
        ("src/*/patch/**", "src/frr/patch/a.patch", True),
        ("src/*/patch/**", "src/frr/x/patch/a.patch", False),
        ("platform/*/docker-*.mk", "platform/mellanox/docker-syncd-mlnx.mk", True),
        ("Makefile*", "src/x/Makefile.am", True),
        ("files/build/**", "files/build/versions/a", True),
    ],
)
def test_glob_semantics(pattern, path, expected):
    assert matches(pattern, path) is expected


def test_components_are_multi_label_and_unmapped_paths_reported(taxonomy):
    areas = classify(
        ["src/sonic-yang-models/yang-models/sonic-port.yang", "weird/place.bin"], {}, taxonomy
    )
    assert [item.id for item in areas.components] == ["packages", "yang-models"]
    assert areas.unmapped_paths == ("weird/place.bin",)


def test_feature_areas_come_from_the_datapath_prefixes(taxonomy):
    bgp = next(area for area in taxonomy.features if area.id == "bgp")
    prefix = bgp.prefixes[0]
    areas = classify([f"{prefix}/sub/file.py", f"{prefix}-lookalike/file.py"], {}, taxonomy)
    assert [(item.id, item.path_count) for item in areas.features] == [("bgp", 1)]
    assert all(not area.id.startswith("sonic-mgmt") for area in taxonomy.features)


def test_entities_from_device_platform_docker_and_gitlinks(taxonomy):
    paths = [
        "device/mellanox/x86_64-mlnx_msn2700-r0/Mellanox-SN2700/port_config.ini",
        "device/mellanox/x86_64-mlnx_msn2700-r0/plugins/sfputil.py",
        "device/common/profiles/x.json",
        "platform/mellanox/docker-syncd-mlnx.mk",
        "platform/template/docker-gbsyncd-base.mk",
        "dockers/docker-orchagent/Dockerfile.j2",
        "rules/docker-lldp.mk",
    ]
    areas = classify(paths, {"src/sonic-swss": "sonic-swss"}, taxonomy)
    assert [entity.id for entity in areas.entities] == [
        "vendor:mellanox",
        "platform:x86_64-mlnx_msn2700-r0",
        "hwsku:Mellanox-SN2700",
        "asic:mellanox",
        "docker:lldp",
        "docker:orchagent",
        "docker:syncd-mlnx",
        "submodule:sonic-swss",
    ]
    vendor = areas.entities[0]
    assert vendor.path_count == 2


@pytest.mark.parametrize(
    "path, expected",
    [
        ("src/sonic-frr/patch/0001-fix.patch", "patch"),
        ("src/sonic-frr/patch/series", "patch"),
        ("src/sonic-config-engine/tests/test_cfggen.py", "test"),
        ("src/sonic-bgpcfgd/bgpcfgd/main.py", "code"),
        ("README.md", "doc"),
        ("src/sonic-yang-models/yang-models/sonic-port.yang", "yang"),
        ("rules/sonic-utilities.mk", "build"),
        ("src/sonic-host-services/debian/control", "build"),
        ("device/arista/x86_64-arista_7050_qx32/default_sku", "config"),
        ("files/image_config/ntp/ntp.conf.j2", "config"),
        ("LICENSE", "doc"),
        ("src/unknown.xyz", "other"),
    ],
)
def test_file_class_first_match_wins(taxonomy, path, expected):
    assert file_class(path, binary=False, gitlink=False, taxonomy=taxonomy) == expected


def test_gitlink_and_binary_override_rules(taxonomy):
    assert file_class("src/a.py", binary=True, gitlink=False, taxonomy=taxonomy) == "binary"
    assert file_class("src/sonic-swss", binary=False, gitlink=True, taxonomy=taxonomy) == "submodule"


def test_taxonomy_sha_covers_the_yaml_and_the_feature_map(tmp_path):
    features = tmp_path / "features.json"
    features.write_text(json.dumps({"bgp": ["repo/src/bgp"]}))
    base = f"schema_version: 1\nfeatures:\n  source: {features}\n  prefix: repo/\n"
    first = tmp_path / "a.yaml"
    first.write_text(base)
    second = tmp_path / "b.yaml"
    second.write_text(base + "bots: [x]\n")
    one, two = load_taxonomy(first), load_taxonomy(second)
    assert one.sha256 != two.sha256
    assert [(area.id, area.prefixes) for area in one.features] == [("bgp", ("src/bgp",))]
    features.write_text(json.dumps({"bgp": ["repo/src/bgp2"]}))
    load_taxonomy.cache_clear()
    assert load_taxonomy(first).sha256 != one.sha256


def test_invalid_taxonomy_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 2\n")
    with pytest.raises(TaxonomyError):
        load_taxonomy(path)
