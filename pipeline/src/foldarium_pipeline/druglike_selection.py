"""Chemistry-only preference for a small medicinal-chemistry-style Weekly draw.

This is a presentation heuristic, not a clinical drug/activity classifier. It
never changes the full prediction/benchmark population. Known metabolites can
be useful drugs, and valid drugs can fall outside this preferred property range.
Experimental coordinates, correctness, pose scores and votes are not inputs.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any

from .contracts import canonical_json
from .quiz import manifest_sha256
from .selection import ARTIFACT_COMPONENTS
from .weekly_quiz import clone_weekly_quiz_manifests

DRUGLIKE_PREFERENCE_POLICY = "foldarium.medchem-presentation-preference/v1"
DRUGLIKE_RDKIT_VERSION = "2025.03.6"
# Exact identities, not a generic sugar/purine/substructure exclusion. The
# antagonist ZMA, for example, is retained despite its nitrogen-rich ring core.
DEPRIORITIZED_COMPONENTS = ARTIFACT_COMPONENTS | frozenset(
    "ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL ADN 5AD".split()
)
PROPERTY_RULES = {
    "minimum_rings": 1,
    "minimum_qed": 0.5,
    "molecular_weight_range": [150.0, 600.0],
    "maximum_tpsa": 140.0,
    "maximum_rotatable_bonds": 10,
    "maximum_hbond_acceptors": 10,
    "maximum_hbond_donors": 5,
    "maximum_rule_of_five_violations": 1,
    "maximum_amide_bonds_exclusive": 4,
    "excluded_acyclic_saturated_carbon_chain_length": 10,
}


class DruglikePreferenceError(ValueError):
    """Chemistry preference cannot be bound to the exact source population."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _runtime():
    # Lazy import keeps ordinary controller/import-only paths dependency-free.
    try:
        from rdkit import Chem, rdBase
        from rdkit.Chem import QED, rdMolDescriptors
    except ImportError as exc:
        raise DruglikePreferenceError("chemistry preference requires the pinned RDKit dependency") from exc
    if rdBase.rdkitVersion != DRUGLIKE_RDKIT_VERSION:
        raise DruglikePreferenceError("chemistry preference requires pinned RDKit " + DRUGLIKE_RDKIT_VERSION)
    return Chem, rdBase, QED, rdMolDescriptors


def _assess_item(item, private, Chem, rdBase, QED, rdMolDescriptors):
    item_id = item["id"]
    eligibility = private.get("ligand_eligibility")
    if not isinstance(eligibility, Mapping):
        return {"item_id": item_id, "status": "unavailable", "preferred": False,
                "reasons": ["source-ligand-chemistry-unavailable"]}
    smiles = eligibility.get("smiles")
    if not isinstance(smiles, str) or not smiles.strip():
        return {"item_id": item_id, "status": "unavailable", "preferred": False,
                "reasons": ["source-ligand-smiles-unavailable"]}
    raw_digest = hashlib.sha256(smiles.encode("utf-8")).hexdigest()
    if eligibility.get("smiles_sha256") not in (None, raw_digest):
        raise DruglikePreferenceError("source ligand SMILES digest differs for " + item_id)
    component = eligibility.get("component_id")
    if not isinstance(component, str) or not component.strip():
        raise DruglikePreferenceError("source ligand component is invalid for " + item_id)
    component = component.strip().upper()
    public_component = item.get("ligand", {}).get("component_id")
    if public_component is not None and (not isinstance(public_component, str) or public_component.strip().upper() != component):
        raise DruglikePreferenceError("source ligand component binding differs for " + item_id)
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        return {"item_id": item_id, "component_id": component, "smiles_sha256": raw_digest,
                "status": "unavailable", "preferred": False, "reasons": ["invalid-source-smiles"]}
    molecule = Chem.RemoveHs(molecule)
    props = QED.properties(molecule)
    amide = Chem.MolFromSmarts("[NX3][CX3](=[OX1])")
    # !R matters: a saturated macrocycle is not an acyclic lipid tail.
    chain = Chem.MolFromSmarts("".join(["[C;X4;!R]"] * 10))
    descriptors = {
        "qed": float(QED.qed(molecule)), "molecular_weight": float(props.MW),
        "logp": float(props.ALOGP), "hbond_acceptors": int(props.HBA),
        "hbond_donors": int(props.HBD), "tpsa": float(props.PSA),
        "rotatable_bonds": int(props.ROTB), "ring_count": int(rdMolDescriptors.CalcNumRings(molecule)),
        "aromatic_ring_count": int(props.AROM), "qed_alerts": int(props.ALERTS),
        "heavy_atoms": int(molecule.GetNumHeavyAtoms()), "formal_charge": int(Chem.GetFormalCharge(molecule)),
        "amide_bonds": len(molecule.GetSubstructMatches(amide)),
        "long_acyclic_aliphatic_chain": bool(molecule.HasSubstructMatch(chain)),
        "fragment_count": len(Chem.GetMolFrags(molecule)),
    }
    if any(isinstance(value, float) and not math.isfinite(value) for value in descriptors.values()):
        raise DruglikePreferenceError("nonfinite chemistry descriptor for " + item_id)
    descriptors["rule_of_five_violations"] = sum((props.MW > 500, props.ALOGP > 5, props.HBA > 10, props.HBD > 5))
    reasons = []
    for rejected, code in (
        (component in DEPRIORITIZED_COMPONENTS, "known-metabolite-or-artifact-component"),
        (descriptors["fragment_count"] != 1, "disconnected-ligand"),
        (descriptors["ring_count"] < 1, "no-ring-scaffold"),
        (descriptors["qed"] < .5, "qed-below-preference"),
        (not 150 <= props.MW <= 600, "molecular-weight-outside-preference"),
        (props.PSA > 140, "polar-surface-area-above-preference"),
        (props.ROTB > 10, "too-many-rotatable-bonds"),
        (props.HBA > 10, "too-many-hbond-acceptors"),
        (props.HBD > 5, "too-many-hbond-donors"),
        (descriptors["rule_of_five_violations"] > 1, "multiple-rule-of-five-violations"),
        (descriptors["amide_bonds"] >= 4, "peptide-like-amide-count"),
        (descriptors["long_acyclic_aliphatic_chain"], "long-acyclic-aliphatic-chain"),
    ):
        if rejected:
            reasons.append(code)
    return {
        "item_id": item_id, "component_id": component, "status": "assessed",
        "source_smiles": smiles, "smiles_sha256": raw_digest,
        "canonical_smiles": Chem.MolToSmiles(molecule),
        "source_ligand_eligibility_policy": eligibility.get("policy"),
        "preferred": not reasons, "reasons": reasons, "descriptors": descriptors,
    }


def assess_druglike_candidates(blind_manifest: Mapping[str, Any], private_index: Mapping[str, Any]) -> dict[str, Any]:
    """Return exact eligible IDs and private evidence for the existing random draw.

    If one to four preferred items exist, use those items without padding. With
    zero preferred items, explicitly retain the source population as a legacy
    fallback; that fallback must not be represented as a drug-like assignment.
    The caller adds this audit to its draw and recomputes the draw digest before
    registration. No source object or pre-existing frozen selection is mutated.
    """
    if not isinstance(blind_manifest, Mapping) or not isinstance(private_index, Mapping):
        raise DruglikePreferenceError("source manifests must be objects")
    try:
        clone_weekly_quiz_manifests(blind_manifest, private_index, round_id=blind_manifest.get("round_id"))
    except (ValueError, TypeError) as exc:
        raise DruglikePreferenceError("invalid chemistry preference source population") from exc
    if not blind_manifest.get("items"):
        raise DruglikePreferenceError("source population is empty")
    runtime = _runtime()
    private_by_id = {item["id"]: item for item in private_index["items"]}
    rows = [_assess_item(item, private_by_id[item["id"]], *runtime)
            for item in sorted(blind_manifest["items"], key=lambda row: row["id"])]
    preferred = [row["item_id"] for row in rows if row["preferred"]]
    all_ids = [row["item_id"] for row in rows]
    fallback = not preferred
    audit = {
        "schema_version": 1, "policy": DRUGLIKE_PREFERENCE_POLICY,
        "rdkit_version": runtime[1].rdkitVersion,
        "status": "legacy-fallback-no-preferred" if fallback else "preferred",
        "fallback_reason": "no-preferred-ligands" if fallback else None,
        "source_round_id": blind_manifest["round_id"],
        "source_blind_manifest_sha256": manifest_sha256(blind_manifest),
        "source_private_index_sha256": _digest(private_index),
        "source_item_count": len(all_ids), "preferred_count": len(preferred),
        "eligible_item_ids": all_ids if fallback else preferred,
        "property_rules": {key: list(value) if isinstance(value, list) else value for key, value in PROPERTY_RULES.items()},
        "deprioritized_components": sorted(DEPRIORITIZED_COMPONENTS),
        "descriptor_rows": rows,
        "scope": "presentation preference only; full benchmark population unchanged",
    }
    audit["evidence_sha256"] = _digest(audit)
    return audit
