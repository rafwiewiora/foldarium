"""Strict, portable contract for released references that cannot be scored."""
from collections.abc import Mapping
import math
import re

REFERENCE_DISPOSITION_POLICY = "foldarium.released-reference-disposition/v1"


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
    if not isinstance(d, Mapping) or d.get("policy") != REFERENCE_DISPOSITION_POLICY or d.get("code") != "insufficient_reference_coverage":
        raise ValueError("invalid unscorable reference disposition")
    if set(d) != {"policy", "code", "component_id", "expected_heavy_atoms", "observed_heavy_atoms", "explicitly_unobserved_heavy_atoms", "reference_coverage", "minimum_reference_coverage", "reference_sha256"} or not isinstance(d.get("reference_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", d["reference_sha256"]):
        raise ValueError("invalid unscorable reference fields or digest")
    expected, observed, missing = (d.get(k) for k in ("expected_heavy_atoms", "observed_heavy_atoms", "explicitly_unobserved_heavy_atoms"))
    if any(isinstance(n, bool) or not isinstance(n, int) or abs(n) > 9007199254740991 for n in (expected, observed, missing)) or not 0 < observed < expected or missing != expected - observed:
        raise ValueError("invalid unscorable reference counts")
    coverage = d.get("reference_coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(coverage) or coverage != observed / expected or coverage >= .8 or d.get("minimum_reference_coverage") != .8:
        raise ValueError("invalid unscorable reference coverage")
    if not isinstance(d.get("component_id"), str) or not d["component_id"].strip():
        raise ValueError("invalid unscorable component")
    choices = item.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("unscorable item requires all original choices")
    for choice in choices:
        if not isinstance(choice, Mapping) or choice.get("reference_sha256") != d["reference_sha256"] or any(k not in choice or choice[k] is not None for k in ("rmsd", "correct", "accepted_correct")):
            raise ValueError("unscorable choices must have explicit null metrics")
    return True
