"""Strict, portable contract for released references that cannot be scored."""
from collections.abc import Mapping
import math
import re

REFERENCE_DISPOSITION_POLICY = "foldarium.released-reference-disposition/v1"
REFERENCE_BRIDGE_DISPOSITION_POLICY = "foldarium.released-reference-disposition/v2"


def validate_reference_disposition(item):
    """Return true for a fully specified unscorable item; reject ambiguous nulls."""
    if not isinstance(item, Mapping):
        raise ValueError("invalid reveal item")
    status = item.get("evaluation_status")
    if status is None or status == "scored":
        if item.get("reference_disposition") is not None:
            raise ValueError("scored item cannot carry a reference disposition")
        return False
    if status != "unscorable":
        raise ValueError("unknown evaluation status")
    d = item.get("reference_disposition")
    if not isinstance(d, Mapping):
        raise ValueError("invalid unscorable reference disposition")
    bridge = d.get("policy") == REFERENCE_BRIDGE_DISPOSITION_POLICY and d.get("code") == "missing_reference_bridge"
    legacy = d.get("policy") == REFERENCE_DISPOSITION_POLICY and d.get("code") == "insufficient_reference_coverage"
    if not (bridge or legacy):
        raise ValueError("invalid unscorable reference disposition")
    fields = {"policy", "code", "component_id", "expected_heavy_atoms", "observed_heavy_atoms", "explicitly_unobserved_heavy_atoms", "reference_coverage", "minimum_reference_coverage", "reference_sha256"}
    if bridge:
        fields |= {"missing_heavy_atom_ids", "observed_component_count", "missing_component_boundary_counts", "ccd_heavy_atom_graph_sha256"}
    if set(d) != fields or not isinstance(d.get("reference_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", d["reference_sha256"]):
        raise ValueError("invalid unscorable reference fields or digest")
    expected, observed, missing = (d.get(k) for k in ("expected_heavy_atoms", "observed_heavy_atoms", "explicitly_unobserved_heavy_atoms"))
    if any(isinstance(n, bool) or not isinstance(n, int) or abs(n) > 9007199254740991 for n in (expected, observed, missing)) or not 0 < observed < expected or missing != expected - observed:
        raise ValueError("invalid unscorable reference counts")
    coverage = d.get("reference_coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or coverage != observed / expected or not 0 < coverage < 1 or (bridge and coverage < .8) or (legacy and coverage >= .8) or d.get("minimum_reference_coverage") != .8:
        raise ValueError("invalid unscorable reference coverage")
    if bridge:
        atom_ids = d.get("missing_heavy_atom_ids")
        components = d.get("observed_component_count")
        boundaries = d.get("missing_component_boundary_counts")
        digest = d.get("ccd_heavy_atom_graph_sha256")
        if not isinstance(atom_ids, list) or len(atom_ids) != missing or any(not isinstance(x, str) or not x.strip() or x != x.strip() for x in atom_ids) or atom_ids != sorted(set(atom_ids)):
            raise ValueError("invalid bridge missing-atom proof")
        if isinstance(components, bool) or not isinstance(components, int) or not 2 <= components <= observed:
            raise ValueError("invalid bridge component proof")
        if not isinstance(boundaries, list) or not 1 <= len(boundaries) <= missing or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= components for x in boundaries) or boundaries != sorted(boundaries) or max(boundaries) < 2:
            raise ValueError("invalid bridge boundary proof")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid bridge component graph digest")
    if not isinstance(d.get("component_id"), str) or not d["component_id"].strip():
        raise ValueError("invalid unscorable component")
    choices = item.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("unscorable item requires all original choices")
    for choice in choices:
        if not isinstance(choice, Mapping) or choice.get("reference_sha256") != d["reference_sha256"] or any(k not in choice or choice[k] is not None for k in ("rmsd", "correct", "accepted_correct")):
            raise ValueError("unscorable choices must have explicit null metrics")
    return True
