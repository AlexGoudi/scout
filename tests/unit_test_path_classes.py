from scout_impl.repos import resolve_adapter
from scout_impl.repos.sonic_mgmt import (
    ANSIBLE_CODE,
    ANSIBLE_DATA,
    DOCUMENTATION,
    FEATURE_TEST,
    OTHER,
    PIPELINE,
    TEST_COMMON,
)

# Path classes are adapter-scoped now, so classification goes through the adapter that
# owns the rules rather than a global table. The assertions are otherwise unchanged:
# sonic-mgmt must classify exactly as it did before the adapter boundary existed.
classify_path = resolve_adapter("sonic-mgmt").classify


def test_ansible_library_and_playbooks_rank_highest() -> None:
    assert classify_path("ansible/library/generate_golden_config_db.py") == ANSIBLE_CODE
    assert classify_path("ansible/module_utils/graph_utils.py") == ANSIBLE_CODE
    assert classify_path("ansible/config_sonic_basedon_testbed.yml") == ANSIBLE_CODE
    assert ANSIBLE_CODE.rank == 1


def test_data_file_families_are_separated_from_ansible_code() -> None:
    assert classify_path("ansible/vars/topo_mx.yml") == ANSIBLE_DATA
    assert classify_path("ansible/files/sonic_nokia_links.csv") == ANSIBLE_DATA
    assert ANSIBLE_DATA.rank > TEST_COMMON.rank


def test_shared_test_infrastructure_outranks_feature_tests() -> None:
    assert classify_path("tests/common/plugins/conditional_mark/__init__.py") == TEST_COMMON
    assert classify_path("tests/conftest.py") == TEST_COMMON
    assert classify_path("tests/bgp/test_bgp_fact.py") == FEATURE_TEST
    assert TEST_COMMON.rank < FEATURE_TEST.rank


def test_pipeline_and_documentation_paths() -> None:
    assert classify_path(".azure-pipelines/impacted_area_testing/constant.py") == PIPELINE
    assert classify_path("azure-pipelines.yml") == PIPELINE
    assert classify_path("docs/scout-hld.md") == DOCUMENTATION
    assert classify_path("tests/bgp/README.md") == DOCUMENTATION


def test_unclassified_paths_outrank_documentation() -> None:
    assert classify_path("setup-container.sh") == OTHER
    assert OTHER.rank < DOCUMENTATION.rank


def test_leading_dot_slash_is_normalized() -> None:
    assert classify_path("./ansible/vars/topo_t0.yml") == ANSIBLE_DATA
