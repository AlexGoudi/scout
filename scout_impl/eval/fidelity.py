"""Coverage-model fidelity: parsed pipeline job groups vs Azure Build jobs (HLD 9.1 / R3)."""

from __future__ import annotations


def check_coverage_fidelity(
    coverage_job_groups: set[str],
    azure_build_jobs: set[str],
    excluded: set[str] | frozenset[str] | None = None,
) -> dict:
    """
    Compare gold / coverage job names with jobs seen in Azure PR timelines.

    Names in ``excluded`` may appear in Azure but are not part of the labeling model
    (e.g. sonic-mgmt VPP elastictest); they are dropped from the Azure side before compare.
    """
    excluded_set = set(excluded or ())
    ignored = sorted(excluded_set & azure_build_jobs)
    azure_compare = azure_build_jobs - excluded_set
    only_parser = sorted(coverage_job_groups - azure_compare)
    only_azure = sorted(azure_compare - coverage_job_groups)
    ok = not only_parser and not only_azure
    payload = {
        "ok": ok,
        "only_in_coverage_model": only_parser,
        "only_in_azure_timelines": only_azure,
    }
    if ignored:
        payload["ignored_excluded_in_azure"] = ignored
    return payload


def check_mgmt_topology_fidelity(pipeline_topologies: set[str], kvm_jobs: set[str]) -> dict:
    """sonic-mgmt: PR_TOPOLOGY_TYPE vs KVM elastictest jobs observed in Azure."""
    only_pipe = sorted(pipeline_topologies - kvm_jobs)
    only_kvm = sorted(kvm_jobs - pipeline_topologies)
    return {
        "ok": not only_pipe and not only_kvm,
        "only_in_pipeline_model": only_pipe,
        "only_in_azure_kvm": only_kvm,
    }
