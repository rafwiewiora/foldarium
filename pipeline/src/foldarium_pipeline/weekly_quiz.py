"""Coordinator-side Saturday assembly of aligned weekly quiz assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from .clustering import (
    PoseClusteringError,
    choice_order_digest,
    cluster_distance_matrix,
)
from .contracts import canonical_json, validate_prediction_task, validate_target, stable_id
from .evaluation import (
    EvaluationError,
    _mapped_rmsd,
    best_receptor_superposition,
    exact_complex_tm_superposition,
)
from .quiz import build_blind_manifest, manifest_sha256
from .supabase import IMMUTABLE_PUBLIC_CACHE_CONTROL
from .weekly_selector import (
    WeeklySelectorError,
    assert_no_forbidden_content,
    build_selector_kit,
    parse_selector_kit,
)
from .selection import (
    HEAVY_ATOM_MINIMUM,
    SELECTION_POLICY_VERSION,
    ligand_rejection_reason,
    select_ligand,
)

WEEKLY_QUIZ_STAGE_VERSION = 11
POCKET_RADIUS_ANGSTROM = 5.0
DISPLAY_ALIGNMENT_MIN_COMPLEX_SUPPORT_FRACTION = 0.20
DISPLAY_ALIGNMENT_MIN_CONTACT_CHAIN_SUPPORT_FRACTION = 0.20
DISPLAY_ALIGNMENT_MIN_ABSOLUTE_SUPPORT = 5
DISPLAY_ALIGNMENT_MIN_SIGNIFICANT_CONTACT_RESIDUES = 3
DISPLAY_ALIGNMENT_QA_POLICY = "shared-frame-global-coverage-and-contact-chain-support/v2"
DISPLAY_ALIGNMENT_WARNING_CODE = "substantial_predicted_protein_conformational_difference"
DISPLAY_ALIGNMENT_WARNING_MESSAGE = (
    "Predicted protein conformations differ substantially; poses use the best common alignment."
)
WEEKLY_PRESENTATION_MULTI_CLUSTER = "multi_cluster"
WEEKLY_PRESENTATION_SINGLE_CLUSTER = "single_cluster"
WEEKLY_PRESENTATION_POLICY = "multi-cluster-first-single-cluster-last/v1"
REQUIRED_METHODS = frozenset({"openfold3", "boltz2"})
LEGACY_LIGAND_ORDER_POLICY = "adapter-preserved-task-smiles-heavy-atom-order/legacy-v1"
SUPPORTED_LEGACY_LIGAND_ORDER = {
    "openfold3": "0.4.4",
    "boltz2": "2.2.1",
}
LIGAND_AUTOMORPHISM_CAP = 100_000
RECEPTOR_ANCHOR_POLICY = (
    "minimum-total-symmetric-exact-task-complex-normalized-tm-distance-medoid/v5"
)
RECEPTOR_ALIGNMENT_POLICY = "exact-task-complex-sequence-global-tm/v3"
RECEPTOR_ENTITY_POLICY = "all-input-protein-chain-sequences/v2"
RECEPTOR_PAIR_ORIENTATION_POLICY = "ascending-choice-digest/v1"
RECEPTOR_DISTANCE_POLICY = (
    "one-minus-fixed-correspondence-normalized-tm-score/v1"
)
LIGAND_CONFIDENCE_METRIC = "ligand_plddt"
LIGAND_CONFIDENCE_AGGREGATION = "arithmetic-mean-selected-ligand-heavy-atoms"
SMINA_SCORE_METRIC = "smina_affinity"
PROLIF_COUNT_METRIC = "prolif_hbond_residue_count"
WEEKLY_QUIZ_ENVIRONMENTS = frozenset({"production", "preview", "development"})
DEFAULT_ARTIFACT_DOWNLOAD_WORKERS = 8
DEFAULT_TARGET_ALIGNMENT_WORKERS = 1
MAX_TARGET_ALIGNMENT_WORKERS = 8
# Supabase Storage intermittently rejects the scheduler's large publication
# burst at eight concurrent requests. Public objects are content-addressed and
# publication is not latency-sensitive, so the reliable default is serial.
# Callers may still opt into bounded concurrency for controlled backfills.
DEFAULT_PUBLIC_UPLOAD_WORKERS = 1
MAX_PUBLIC_UPLOAD_WORKERS = 8
SELECTOR_KIT_ZIP_MEDIA_TYPE = "application/zip"
SELECTOR_TARGETS_JSON_MEDIA_TYPE = "application/json"
SELECTOR_ASSET_DOWNLOAD_ATTEMPTS = 4
SELECTOR_ASSET_DOWNLOAD_RETRY_SECONDS = 0.5


class WeeklyQuizAssemblyError(RuntimeError):
    """Raised when completed predictions cannot form a safe blind round."""


def _weekly_receptor_superposition(
    reference_model: Any,
    predicted_model: Any,
    *,
    expected_chain_sequences: Mapping[str, str],
) -> Mapping[str, Any]:
    alignment = dict(
        exact_complex_tm_superposition(
            reference_model,
            predicted_model,
            expected_chain_sequences=expected_chain_sequences,
        )
    )
    alignment["chain_selection_policy"] = RECEPTOR_ALIGNMENT_POLICY
    return alignment


def _dependencies() -> tuple[Any, Any, Any]:
    try:
        import gemmi
        import numpy
        from rdkit import Chem
    except (ImportError, ModuleNotFoundError) as exc:
        raise WeeklyQuizAssemblyError(
            "weekly quiz assembly requires Gemmi, NumPy, and RDKit"
        ) from exc
    return gemmi, numpy, Chem


def _safe_path(root: Path, relative: Any, field: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise WeeklyQuizAssemblyError(f"{field} must be a safe relative path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise WeeklyQuizAssemblyError(f"{field} must stay below the stage directory")
    path = (root / Path(*posix.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WeeklyQuizAssemblyError(f"{field} must stay below the stage directory") from exc
    return path


def _heavy_atoms(residue: Any) -> list[Any]:
    return [atom for atom in residue if atom.element.name != "H"]


def _selected_ligand(target: Mapping[str, Any]) -> tuple[str, int, set[str], str]:
    metadata = target.get("metadata")
    selected = metadata.get("selected_ligand") if isinstance(metadata, Mapping) else None
    if not isinstance(selected, Mapping):
        raise WeeklyQuizAssemblyError("target has no selected_ligand metadata")
    component = selected.get("component_id")
    heavy_atoms = selected.get("heavy_atoms")
    if not isinstance(component, str) or not component:
        raise WeeklyQuizAssemblyError("selected ligand component_id is invalid")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise WeeklyQuizAssemblyError("selected ligand heavy_atoms is invalid")
    ligand_entities = [
        entity
        for entity in target.get("entities", [])
        if isinstance(entity, Mapping) and entity.get("type") == "ligand"
    ]
    if len(ligand_entities) != 1:
        raise WeeklyQuizAssemblyError(
            "weekly clustering requires exactly one selected ligand entity"
        )
    ligand_entity = ligand_entities[0]
    smiles = ligand_entity.get("smiles")
    if not isinstance(smiles, str) or not smiles.strip():
        raise WeeklyQuizAssemblyError(
            "selected ligand requires task SMILES for weekly clustering"
        )
    chain_ids = {
        chain_id
        for chain_id in ligand_entity.get("chain_ids", [])
        if isinstance(chain_id, str)
    }
    return component, heavy_atoms, chain_ids, smiles.strip()


def _weekly_ligand_eligibility(
    component_id: str,
    heavy_atoms: int,
    smiles: str,
) -> dict[str, Any]:
    """Revalidate historical prediction tasks against the current shared policy."""

    if not isinstance(component_id, str) or not component_id.strip():
        raise WeeklyQuizAssemblyError("weekly ligand component ID is invalid")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise WeeklyQuizAssemblyError("weekly ligand heavy-atom count is invalid")
    if not isinstance(smiles, str) or not smiles.strip():
        raise WeeklyQuizAssemblyError("weekly ligand SMILES is invalid")
    component_id = component_id.strip().upper()
    smiles = smiles.strip()

    selected = select_ligand(
        [{"component_id": component_id, "smiles": smiles}],
        heavy_atom_minimum=HEAVY_ATOM_MINIMUM,
    )
    passed = selected is not None
    rejection_reason = ligand_rejection_reason(
        {"component_id": component_id, "smiles": smiles},
        heavy_atom_minimum=HEAVY_ATOM_MINIMUM,
    )
    if passed and selected["heavy_atoms"] != heavy_atoms:
        raise WeeklyQuizAssemblyError(
            "selected ligand heavy-atom metadata disagrees with the current policy"
        )
    return {
        "policy": SELECTION_POLICY_VERSION,
        "passed": passed,
        "component_id": component_id,
        "heavy_atoms": heavy_atoms,
        "smiles": smiles,
        "smiles_sha256": hashlib.sha256(smiles.encode("utf-8")).hexdigest(),
        "reason": rejection_reason,
    }


def ligand_eligibility_from_target(target: Mapping[str, Any]) -> dict[str, Any]:
    """Derive weekly ligand eligibility from one canonical target package."""

    component_id, heavy_atoms, _chain_ids, smiles = _selected_ligand(target)
    return _weekly_ligand_eligibility(component_id, heavy_atoms, smiles)


def _legacy_ligand_topology_graph(
    ligand_smiles: str,
) -> tuple[list[int], list[list[int]], Any, Any]:
    """Parse one task SMILES into a deterministic heavy-atom topology graph."""

    if not isinstance(ligand_smiles, str) or not ligand_smiles.strip():
        raise WeeklyQuizAssemblyError(
            "selected ligand requires canonical task SMILES for clustering"
        )
    _, _, Chem = _dependencies()
    source_molecule = Chem.MolFromSmiles(ligand_smiles.strip())
    if source_molecule is None:
        raise WeeklyQuizAssemblyError(
            "selected ligand task SMILES could not be parsed for clustering"
        )
    expected_elements: list[int] = []
    old_to_new: dict[int, int] = {}
    for atom in source_molecule.GetAtoms():
        atomic_number = int(atom.GetAtomicNum())
        if atomic_number == 1:
            continue
        old_to_new[atom.GetIdx()] = len(expected_elements)
        expected_elements.append(atomic_number)
    if not expected_elements:
        raise WeeklyQuizAssemblyError("selected ligand task SMILES has no heavy atoms")
    topology_builder = Chem.RWMol()
    for atomic_number in expected_elements:
        atom = Chem.Atom(atomic_number)
        atom.SetNoImplicit(True)
        topology_builder.AddAtom(atom)
    topology_edges: list[list[int]] = []
    for bond in source_molecule.GetBonds():
        begin = bond.GetBeginAtomIdx()
        end = bond.GetEndAtomIdx()
        if begin not in old_to_new or end not in old_to_new:
            continue
        left, right = sorted((old_to_new[begin], old_to_new[end]))
        topology_builder.AddBond(left, right, Chem.BondType.SINGLE)
        topology_edges.append([int(left), int(right)])
    topology_edges.sort()
    topology = topology_builder.GetMol()
    return expected_elements, topology_edges, topology, Chem


def legacy_ligand_topology_digest(ligand_smiles: str) -> dict[str, Any]:
    """Recompute immutable topology digest fields without automorphism enumeration."""

    expected_elements, topology_edges, _topology, _Chem = _legacy_ligand_topology_graph(
        ligand_smiles
    )
    topology_payload = {
        "atomic_numbers": expected_elements,
        "edges": topology_edges,
    }
    return {
        "source_smiles_sha256": hashlib.sha256(
            ligand_smiles.encode("utf-8")
        ).hexdigest(),
        "source_topology_sha256": hashlib.sha256(
            canonical_json(topology_payload).encode("utf-8")
        ).hexdigest(),
        "heavy_atom_count": len(expected_elements),
    }


def legacy_ligand_topology_audit(ligand_smiles: str) -> dict[str, Any]:
    """Recompute the clustering topology audit fields for one task SMILES."""

    expected_elements, topology_edges, topology, Chem = _legacy_ligand_topology_graph(
        ligand_smiles
    )
    mappings = topology.GetSubstructMatches(
        topology,
        uniquify=False,
        useChirality=False,
        maxMatches=LIGAND_AUTOMORPHISM_CAP + 1,
    )
    if not mappings:
        raise WeeklyQuizAssemblyError(
            "canonical ligand graph has no self mapping for clustering"
        )
    if len(mappings) > LIGAND_AUTOMORPHISM_CAP:
        raise WeeklyQuizAssemblyError(
            "canonical ligand graph exceeds the clustering automorphism limit"
        )
    digest = legacy_ligand_topology_digest(ligand_smiles)
    return {
        "policy": LEGACY_LIGAND_ORDER_POLICY,
        **digest,
        "automorphism_count": len(mappings),
        "automorphism_cap": LIGAND_AUTOMORPHISM_CAP,
        "rdkit_version": str(Chem.rdBase.rdkitVersion),
    }


def _weekly_presentation_group(cluster_count: int) -> str:
    if isinstance(cluster_count, bool) or not isinstance(cluster_count, int) or cluster_count < 1:
        raise WeeklyQuizAssemblyError("weekly presentation requires a positive cluster count")
    return (
        WEEKLY_PRESENTATION_MULTI_CLUSTER
        if cluster_count > 1
        else WEEKLY_PRESENTATION_SINGLE_CLUSTER
    )


def _weekly_presentation_key(item: Mapping[str, Any]) -> tuple[int, str]:
    group = item.get("presentation_group")
    target_id = item.get("target_id")
    if group not in {
        WEEKLY_PRESENTATION_MULTI_CLUSTER,
        WEEKLY_PRESENTATION_SINGLE_CLUSTER,
    } or not isinstance(target_id, str) or not target_id:
        raise WeeklyQuizAssemblyError("weekly item presentation provenance is invalid")
    return (group == WEEKLY_PRESENTATION_SINGLE_CLUSTER, target_id)


def _order_weekly_manifests(
    blind: dict[str, Any],
    private_index: dict[str, Any],
    ordered_item_ids: Iterable[str],
) -> None:
    """Bind the tested multi-cluster-first order into both manifest halves."""

    ordered_ids = list(ordered_item_ids)
    if (
        not ordered_ids
        or len(ordered_ids) != len(set(ordered_ids))
        or any(not isinstance(item_id, str) or not item_id for item_id in ordered_ids)
    ):
        raise WeeklyQuizAssemblyError("weekly manifest presentation order is invalid")
    order = {item_id: index for index, item_id in enumerate(ordered_ids)}
    for name, manifest in (("blind", blind), ("private", private_index)):
        items = manifest.get("items")
        if not isinstance(items, list):
            raise WeeklyQuizAssemblyError(f"{name} weekly manifest has no items")
        item_ids = [
            item.get("id") if isinstance(item, Mapping) else None for item in items
        ]
        if set(item_ids) != set(ordered_ids) or len(item_ids) != len(ordered_ids):
            raise WeeklyQuizAssemblyError(
                f"{name} weekly manifest item IDs differ from the staged order"
            )
        items.sort(key=lambda item: order[item["id"]])
    private_index["blind_manifest_sha256"] = manifest_sha256(blind)


def _receptor_complex(
    target: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Bind the shared display frame to every submitted protein chain."""

    chain_sequences: dict[str, str] = {}
    for entity in target.get("entities", []):
        if not isinstance(entity, Mapping) or entity.get("type") != "protein":
            continue
        sequence = entity.get("sequence")
        chain_ids = entity.get("chain_ids")
        if not isinstance(sequence, str) or not sequence or not isinstance(chain_ids, list):
            continue
        for chain_id in sorted(
            chain_id for chain_id in chain_ids if isinstance(chain_id, str) and chain_id
        ):
            if chain_id in chain_sequences:
                raise WeeklyQuizAssemblyError(
                    f"submitted protein chain {chain_id} occurs more than once"
                )
            chain_sequences[chain_id] = sequence
    if not chain_sequences:
        raise WeeklyQuizAssemblyError("weekly alignment requires an input protein entity")
    chains = [
        {
            "chain_id": chain_id,
            "sequence_length": len(chain_sequences[chain_id]),
            "sequence_sha256": hashlib.sha256(
                chain_sequences[chain_id].encode("ascii")
            ).hexdigest(),
        }
        for chain_id in sorted(chain_sequences)
    ]
    return (
        {
            "policy": RECEPTOR_ENTITY_POLICY,
            "chain_count": len(chains),
            "total_sequence_length": sum(row["sequence_length"] for row in chains),
            "chains": chains,
        },
        chain_sequences,
    )


def _prediction_ligand(model: Any, heavy_atoms: int, preferred_chains: set[str]) -> Any:
    preferred: list[Any] = []
    fallback: list[Any] = []
    for chain in model:
        for residue in chain:
            if len(_heavy_atoms(residue)) != heavy_atoms:
                continue
            fallback.append(residue)
            if chain.name in preferred_chains:
                preferred.append(residue)
    choices = preferred or fallback
    if len(choices) != 1:
        raise WeeklyQuizAssemblyError(
            f"prediction must contain one selected {heavy_atoms}-heavy-atom ligand; found {len(choices)}"
        )
    return choices[0]


def _ligand_confidence(ligand: Any) -> dict[str, Any]:
    """Normalize the selected ligand's atom pLDDTs without hiding method calibration.

    The pinned OpenFold3 and Boltz-2 versions both export per-atom pLDDT on a
    nominal 0--100 scale in the coordinate model's B/QA value.  We publish the
    heavy-atom mean and the originating method alongside it; consumers must not
    treat scores from the two separately trained confidence heads as calibrated
    probabilities or assume that equal values are interchangeable.
    """

    values: list[float] = []
    for atom in _heavy_atoms(ligand):
        value = float(atom.b_iso)
        if not math.isfinite(value) or value < 0.0 or value > 100.0:
            raise WeeklyQuizAssemblyError(
                "selected ligand contains an invalid 0-100 pLDDT value"
            )
        values.append(value)
    if not values:
        raise WeeklyQuizAssemblyError("selected ligand contains no heavy-atom pLDDT values")
    return {
        "metric": LIGAND_CONFIDENCE_METRIC,
        "value": round(sum(values) / len(values), 2),
        "scale_min": 0.0,
        "scale_max": 100.0,
        "aggregation": LIGAND_CONFIDENCE_AGGREGATION,
    }


def _choice_scoring_fields(result: Any, *, expected_pose_id: str) -> dict[str, Any]:
    """Validate a remote fixed-pose score before binding it into the stage digest."""

    if not isinstance(result, Mapping):
        raise WeeklyQuizAssemblyError("pose scorer returned no result object")
    if result.get("pose_id") != expected_pose_id:
        raise WeeklyQuizAssemblyError("pose scorer returned the wrong pose identity")
    if result.get("schema_version") != "foldarium.pose-score/v1" or result.get("status") != "succeeded":
        raise WeeklyQuizAssemblyError("pose scorer did not return a succeeded v1 score")
    scores = result.get("scores")
    provenance = result.get("provenance")
    if not isinstance(scores, Mapping) or not isinstance(provenance, Mapping):
        raise WeeklyQuizAssemblyError("pose scorer omitted score provenance")
    affinity = scores.get("smina_affinity_kcal_mol")
    if isinstance(affinity, bool) or not isinstance(affinity, (int, float)) \
            or not math.isfinite(float(affinity)):
        raise WeeklyQuizAssemblyError("pose scorer returned an invalid smina affinity")
    if provenance.get("mode") != "score_only":
        raise WeeklyQuizAssemblyError("pose scorer did not use smina score-only mode")
    scoring_function = provenance.get("scoring_function")
    if not isinstance(scoring_function, str) or not scoring_function:
        raise WeeklyQuizAssemblyError("pose scorer omitted its scoring function")
    interactions = result.get("interaction_summary")
    if not isinstance(interactions, Mapping) or interactions.get("engine") != "prolif":
        raise WeeklyQuizAssemblyError("pose scorer omitted its ProLIF interaction summary")
    interaction_count = interactions.get("count")
    interaction_policy = interactions.get("policy")
    if (
        isinstance(interaction_count, bool)
        or not isinstance(interaction_count, int)
        or interaction_count < 0
        or not isinstance(interaction_policy, str)
        or not interaction_policy
    ):
        raise WeeklyQuizAssemblyError("pose scorer returned an invalid ProLIF count")
    return {
        "smina_score": {
            "metric": SMINA_SCORE_METRIC,
            "value": float(affinity),
            "units": "kcal/mol",
            "protocol": "score_only",
            "scoring_function": scoring_function,
        },
        "interaction_count": {
            "metric": PROLIF_COUNT_METRIC,
            "value": interaction_count,
            "policy": interaction_policy,
        },
        "scoring": deepcopy(dict(result)),
    }


def _position(atom: Any, transform: Any, gemmi: Any) -> tuple[float, float, float]:
    value = transform.apply(gemmi.Position(atom.pos.x, atom.pos.y, atom.pos.z))
    return float(value.x), float(value.y), float(value.z)


def _identity_transform(gemmi: Any) -> Any:
    transform = gemmi.Transform()
    transform.mat.fromlist([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    transform.vec.fromlist([0.0, 0.0, 0.0])
    return transform


def _atom_line(
    record: str,
    serial: int,
    atom_name: str,
    residue_name: str,
    chain_name: str,
    residue_number: int,
    xyz: tuple[float, float, float],
    element: str,
) -> str:
    name = re.sub(r"[^A-Za-z0-9'\"*]", "", atom_name)[:4] or element[:2]
    residue = re.sub(r"[^A-Za-z0-9]", "", residue_name)[:3] or "UNK"
    return (
        f"{record:<6s}{serial:5d} {name:<4s} {residue:>3s} {chain_name[:1]:1s}"
        f"{residue_number:4d}    {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}"
        f"  1.00  0.00          {element[:2]:>2s}"
    )


def _write_ligand(path: Path, residue: Any, transform: Any, gemmi: Any) -> list[list[float]]:
    lines: list[str] = []
    coordinates: list[list[float]] = []
    for serial, atom in enumerate(_heavy_atoms(residue), start=1):
        xyz = _position(atom, transform, gemmi)
        coordinates.append(list(xyz))
        lines.append(
            _atom_line("HETATM", serial, atom.name, "LIG", "X", 1, xyz, atom.element.name)
        )
    if not lines:
        raise WeeklyQuizAssemblyError("selected ligand contains no heavy atoms")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")
    return coordinates


def _polymer_residues(model: Any) -> list[tuple[Any, Any]]:
    return [
        (chain, residue)
        for chain in model
        for residue in chain.get_polymer()
        if _heavy_atoms(residue)
    ]


def _minimum_display_support(aligned_residue_count: int, fraction: float) -> int:
    if aligned_residue_count <= 0:
        raise WeeklyQuizAssemblyError("display alignment has no aligned receptor residues")
    return min(
        aligned_residue_count,
        max(
            DISPLAY_ALIGNMENT_MIN_ABSOLUTE_SUPPORT,
            math.ceil(aligned_residue_count * fraction),
        ),
    )


def _ligand_contact_residue_counts(model: Any, ligand: Any, numpy: Any) -> dict[str, int]:
    """Count sequence-addressable receptor residues within the display pocket cutoff."""

    ligand_coordinates = numpy.array(
        [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in _heavy_atoms(ligand)],
        dtype=float,
    )
    if not len(ligand_coordinates):
        raise WeeklyQuizAssemblyError("display alignment ligand has no heavy atoms")
    counts: dict[str, int] = defaultdict(int)
    for chain, residue in _polymer_residues(model):
        residue_coordinates = numpy.array(
            [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in _heavy_atoms(residue)],
            dtype=float,
        )
        if len(residue_coordinates) and float(
            numpy.min(
                numpy.linalg.norm(
                    residue_coordinates[:, None] - ligand_coordinates[None],
                    axis=2,
                )
            )
        ) < POCKET_RADIUS_ANGSTROM:
            counts[chain.name] += 1
    return dict(sorted(counts.items()))


def _weekly_display_alignment_qa(
    alignment: Mapping[str, Any],
    *,
    contact_residue_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Classify when the best shared frame weakly supports the complex or binding chain."""

    robust = alignment.get("global_coverage")
    post_transform = alignment.get("post_transform_ca")
    if not isinstance(robust, Mapping) or not isinstance(post_transform, Mapping):
        raise WeeklyQuizAssemblyError("display alignment lacks post-transform QA provenance")
    aligned = robust.get("aligned_residue_count")
    retained = robust.get("retained_residue_count")
    per_chain = robust.get("per_chain")
    if (
        isinstance(aligned, bool)
        or not isinstance(aligned, int)
        or aligned <= 0
        or isinstance(retained, bool)
        or not isinstance(retained, int)
        or retained < 0
        or retained > aligned
        or not isinstance(per_chain, list)
    ):
        raise WeeklyQuizAssemblyError("display alignment global-coverage provenance is invalid")
    chain_support: dict[str, dict[str, int]] = {}
    for row in per_chain:
        if not isinstance(row, Mapping):
            raise WeeklyQuizAssemblyError("display alignment per-chain provenance is invalid")
        chain_id = row.get("chain_id")
        chain_aligned = row.get("aligned_residue_count")
        chain_retained = row.get("retained_residue_count")
        if (
            not isinstance(chain_id, str)
            or not chain_id
            or isinstance(chain_aligned, bool)
            or not isinstance(chain_aligned, int)
            or chain_aligned <= 0
            or isinstance(chain_retained, bool)
            or not isinstance(chain_retained, int)
            or not 0 <= chain_retained <= chain_aligned
            or chain_id in chain_support
        ):
            raise WeeklyQuizAssemblyError("display alignment per-chain provenance is invalid")
        chain_support[chain_id] = {
            "aligned_residue_count": chain_aligned,
            "retained_residue_count": chain_retained,
        }
    if (
        sum(row["aligned_residue_count"] for row in chain_support.values()) != aligned
        or sum(row["retained_residue_count"] for row in chain_support.values()) != retained
    ):
        raise WeeklyQuizAssemblyError(
            "display alignment per-chain provenance does not match complex totals"
        )
    if (
        post_transform.get("count") != aligned
        or post_transform.get("within_5_angstrom_count") != retained
    ):
        raise WeeklyQuizAssemblyError(
            "display alignment global coverage does not match its provenance"
        )

    failures: list[dict[str, Any]] = []
    minimum_complex_support = _minimum_display_support(
        aligned, DISPLAY_ALIGNMENT_MIN_COMPLEX_SUPPORT_FRACTION
    )
    if retained < minimum_complex_support:
        failures.append(
            {
                "code": "insufficient_complex_global_coverage",
                "aligned_residue_count": aligned,
                "retained_residue_count": retained,
                "minimum_retained_residue_count": minimum_complex_support,
            }
        )

    contact_chains: list[dict[str, Any]] = []
    for chain_id, contact_count in sorted(contact_residue_counts.items()):
        if (
            isinstance(contact_count, bool)
            or not isinstance(contact_count, int)
            or contact_count < 0
        ):
            raise WeeklyQuizAssemblyError("display alignment contact provenance is invalid")
        support = chain_support.get(chain_id)
        if support is None:
            raise WeeklyQuizAssemblyError(
                f"display alignment contact chain {chain_id} lacks global-coverage provenance"
            )
        minimum_chain_support = _minimum_display_support(
            support["aligned_residue_count"],
            DISPLAY_ALIGNMENT_MIN_CONTACT_CHAIN_SUPPORT_FRACTION,
        )
        significant = contact_count >= DISPLAY_ALIGNMENT_MIN_SIGNIFICANT_CONTACT_RESIDUES
        row = {
            "chain_id": chain_id,
            "contact_residue_count": contact_count,
            **support,
            "minimum_retained_residue_count": minimum_chain_support,
            "significant": significant,
        }
        contact_chains.append(row)
        if significant and support["retained_residue_count"] < minimum_chain_support:
            failures.append(
                {
                    "code": "unsupported_ligand_contact_chain",
                    **row,
                }
            )

    return {
        "policy": DISPLAY_ALIGNMENT_QA_POLICY,
        "passed": not failures,
        "thresholds": {
            "support_definition": "exact-task-sequence-matched-ca-within-5-angstrom/v1",
            "complex_support_fraction": DISPLAY_ALIGNMENT_MIN_COMPLEX_SUPPORT_FRACTION,
            "contact_chain_support_fraction": (
                DISPLAY_ALIGNMENT_MIN_CONTACT_CHAIN_SUPPORT_FRACTION
            ),
            "minimum_absolute_support": DISPLAY_ALIGNMENT_MIN_ABSOLUTE_SUPPORT,
            "minimum_significant_contact_residues": (
                DISPLAY_ALIGNMENT_MIN_SIGNIFICANT_CONTACT_RESIDUES
            ),
            "contact_cutoff_angstrom": POCKET_RADIUS_ANGSTROM,
        },
        "complex": {
            "aligned_residue_count": aligned,
            "retained_residue_count": retained,
            "minimum_retained_residue_count": minimum_complex_support,
        },
        "contact_chains": contact_chains,
        "post_transform_ca": deepcopy(dict(post_transform)),
        "failures": failures,
    }


def _write_polymer(
    path: Path,
    model: Any,
    *,
    near: Any | None,
    transform: Any,
    gemmi: Any,
    numpy: Any,
) -> None:
    chain_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    chain_names: dict[str, str] = {}
    lines: list[str] = []
    serial = 0
    residue_number = 0
    for chain, residue in _polymer_residues(model):
        atoms = _heavy_atoms(residue)
        coordinates = numpy.array(
            [_position(atom, transform, gemmi) for atom in atoms], dtype=float
        )
        if near is not None and (
            not len(coordinates)
            or float(numpy.min(numpy.linalg.norm(coordinates[:, None] - near[None], axis=2)))
            >= POCKET_RADIUS_ANGSTROM
        ):
            continue
        if chain.name not in chain_names:
            if len(chain_names) >= len(chain_alphabet):
                raise WeeklyQuizAssemblyError("too many receptor chains for browser PDB export")
            chain_names[chain.name] = chain_alphabet[len(chain_names)]
        residue_number += 1
        for atom in atoms:
            serial += 1
            lines.append(
                _atom_line(
                    "ATOM",
                    serial,
                    atom.name,
                    residue.name,
                    chain_names[chain.name],
                    residue_number,
                    _position(atom, transform, gemmi),
                    atom.element.name,
                )
            )
    if not lines:
        raise WeeklyQuizAssemblyError("browser protein/pocket export is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")


def _load_model(path: Path, gemmi: Any) -> tuple[Any, Any]:
    try:
        structure = gemmi.read_structure(str(path))
        structure.setup_entities()
        return structure, structure[0]
    except Exception as exc:
        raise WeeklyQuizAssemblyError(f"could not parse prediction coordinates: {path.name}") from exc


def _pairwise_pose_distances(
    ligands: list[Any],
    pose_coordinates: list[list[list[float]]],
    *,
    ligand_smiles: str,
    numpy: Any,
    Chem: Any,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Return canonical-graph-symmetry RMSDs in the shared receptor frame.

    The task SMILES is the authoritative graph. Both pinned adapters preserve
    its RDKit heavy-atom order in their output coordinates, although they use
    different atom names. Validating the ordered element sequence prevents a
    changed adapter/output contract from silently producing a partial score.
    The ligand coordinates have already received their receptor transform; no
    ligand Kabsch fit is performed here.
    """

    if not isinstance(ligand_smiles, str) or not ligand_smiles.strip():
        raise WeeklyQuizAssemblyError(
            "selected ligand requires canonical task SMILES for clustering"
        )
    expected_elements, topology_edges, topology, Chem = _legacy_ligand_topology_graph(
        ligand_smiles
    )
    for index, ligand in enumerate(ligands):
        atoms = _heavy_atoms(ligand)
        observed_elements = [atom.element.atomic_number for atom in atoms]
        if observed_elements != expected_elements:
            raise WeeklyQuizAssemblyError(
                "prediction ligand does not preserve task-SMILES "
                f"heavy-atom order for blind choice {index + 1}"
            )
        atom_names = [atom.name.strip() for atom in atoms]
        if any(not name for name in atom_names) or len(set(atom_names)) != len(atom_names):
            raise WeeklyQuizAssemblyError(
                f"prediction ligand atom names are not unique for blind choice {index + 1}"
            )

    mappings = topology.GetSubstructMatches(
        topology,
        uniquify=False,
        useChirality=False,
        maxMatches=LIGAND_AUTOMORPHISM_CAP + 1,
    )
    if not mappings:
        raise WeeklyQuizAssemblyError(
            "canonical ligand graph has no self mapping for clustering"
        )
    if len(mappings) > LIGAND_AUTOMORPHISM_CAP:
        raise WeeklyQuizAssemblyError(
            "canonical ligand graph exceeds the clustering automorphism limit"
        )

    digest = legacy_ligand_topology_digest(ligand_smiles)
    mapping_audit = {
        "policy": LEGACY_LIGAND_ORDER_POLICY,
        **digest,
        "automorphism_count": len(mappings),
        "automorphism_cap": LIGAND_AUTOMORPHISM_CAP,
        "rdkit_version": str(Chem.rdBase.rdkitVersion),
    }
    try:
        coordinates = [numpy.array(pose, dtype=float) for pose in pose_coordinates]
        expected_shape = (len(expected_elements), 3)
        for index, coordinate_array in enumerate(coordinates):
            if coordinate_array.shape != expected_shape or not bool(
                numpy.all(numpy.isfinite(coordinate_array))
            ):
                raise WeeklyQuizAssemblyError(
                    f"prediction ligand coordinates are invalid for blind choice {index + 1}"
                )
        matrix = [[0.0 for _ in ligands] for _ in ligands]
        for left in range(len(ligands)):
            for right in range(left + 1, len(ligands)):
                distance, _mapping = _mapped_rmsd(
                    coordinates[left], coordinates[right], mappings, numpy
                )
                if not math.isfinite(distance):
                    raise EvaluationError("ligand pair RMSD is not finite")
                matrix[left][right] = distance
                matrix[right][left] = distance
        return matrix, mapping_audit
    except EvaluationError as exc:
        raise WeeklyQuizAssemblyError(
            "canonical ligand graph does not support unambiguous clustering"
        ) from exc


def _select_receptor_medoid(
    choices: list[dict[str, Any]],
    *,
    round_id: str,
    target_id: str,
    aligner: Callable[[Any, Any], Mapping[str, Any]] = best_receptor_superposition,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Choose the prediction closest to all other receptors without method preference."""

    if not choices:
        raise WeeklyQuizAssemblyError("cannot select a receptor medoid without choices")
    ordered = [
        (
            choice_order_digest(
                round_id,
                target_id,
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            ),
            choice,
        )
        for choice in choices
    ]
    ordered.sort(key=lambda row: row[0])
    digests = [digest for digest, _choice in ordered]
    choices = [choice for _digest, choice in ordered]
    matrix = [[0.0 for _ in choices] for _ in choices]
    # Canonical digest orientation makes every unordered pair deterministic even
    # when callers provide choices in a different order.  The normalized TM
    # distance is then mirrored explicitly for a symmetric medoid objective.
    for reference_index, reference in enumerate(choices):
        for predicted_index in range(reference_index + 1, len(choices)):
            predicted = choices[predicted_index]
            if digests[reference_index] <= digests[predicted_index]:
                canonical_reference, canonical_predicted = reference, predicted
            else:
                canonical_reference, canonical_predicted = predicted, reference
            try:
                alignment = aligner(
                    canonical_reference["model"], canonical_predicted["model"]
                )
                distance = 1.0 - float(alignment["receptor_tm_score"])
            except (EvaluationError, KeyError, TypeError, ValueError) as exc:
                raise WeeklyQuizAssemblyError(
                    "could not compare receptors while selecting the shared medoid for "
                    f"{target_id}"
                ) from exc
            if not math.isfinite(distance) or not 0 <= distance <= 1:
                raise WeeklyQuizAssemblyError(
                    "receptor-medoid normalized TM distance must be between zero and one"
                )
            matrix[reference_index][predicted_index] = distance
            matrix[predicted_index][reference_index] = distance
    totals = [sum(row) for row in matrix]
    medoid_index = min(range(len(choices)), key=lambda index: (totals[index], digests[index]))
    distance_payload = {
        "choice_order": digests,
        "normalized_tm_distances": [
            [f"{value:.6f}" for value in row]
            for row in matrix
        ],
    }
    return choices[medoid_index], {
        "policy": RECEPTOR_ANCHOR_POLICY,
        "choice_digest": digests[medoid_index],
        "total_pairwise_receptor_distance": totals[medoid_index],
        "choice_order": digests,
        "total_pairwise_receptor_distances": totals,
        "pair_orientation_policy": RECEPTOR_PAIR_ORIENTATION_POLICY,
        "distance_policy": RECEPTOR_DISTANCE_POLICY,
        "distance_matrix_sha256": hashlib.sha256(
            canonical_json(distance_payload).encode("utf-8")
        ).hexdigest(),
    }


def _normalized_runs(
    runs: Iterable[Mapping[str, Any]], required_methods: frozenset[str]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in runs:
        if not isinstance(raw, Mapping):
            raise WeeklyQuizAssemblyError("campaign outputs must be objects")
        row = deepcopy(dict(raw))
        task = validate_prediction_task(row.get("task_payload"))
        if row.get("status") != "succeeded" or row.get("run_id") != task["task_id"]:
            raise WeeklyQuizAssemblyError("campaign output does not match a succeeded task")
        if (
            row.get("method") != task["method"]
            or row.get("method_version") != task["method_version"]
            or row.get("target_id") != task["target"]["target_id"]
        ):
            raise WeeklyQuizAssemblyError("campaign output identity disagrees with its task")
        samples = row.get("samples")
        if not isinstance(samples, list) or not samples:
            raise WeeklyQuizAssemblyError("campaign output has no prediction samples")
        grouped[task["target"]["target_id"]].append({**row, "task_payload": task})
    if not grouped:
        raise WeeklyQuizAssemblyError("campaign has no succeeded prediction outputs")
    for target_id, rows in grouped.items():
        methods = {str(row["method"]) for row in rows}
        if methods != set(required_methods):
            raise WeeklyQuizAssemblyError(
                f"target {target_id} requires exactly {sorted(required_methods)}; found {sorted(methods)}"
            )
        if len(rows) != len(methods):
            raise WeeklyQuizAssemblyError(f"target {target_id} has duplicate method runs")
    return grouped


def _artifact_suffix(artifact: Mapping[str, Any]) -> str:
    media_type = str(artifact.get("media_type") or "chemical/x-mmcif")
    return ".pdb" if "pdb" in media_type.lower() else ".cif"


def _prediction_artifact_records(
    grouped: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return one deterministic download record per content-addressed artifact."""

    records: dict[tuple[str, str], dict[str, Any]] = {}
    for target_id in sorted(grouped):
        for row in sorted(
            grouped[target_id],
            key=lambda value: (
                0 if value["method"] == "openfold3" else 1,
                value["run_id"],
            ),
        ):
            for sample in sorted(
                row["samples"],
                key=lambda value: (value.get("sample_index", 0), value["sample_id"]),
            ):
                artifact = sample.get("predicted_complex")
                if not isinstance(artifact, Mapping):
                    raise WeeklyQuizAssemblyError(
                        "prediction sample lacks predicted_complex metadata"
                    )
                digest = artifact.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise WeeklyQuizAssemblyError(
                        "prediction artifact has no valid SHA-256"
                    )
                suffix = _artifact_suffix(artifact)
                records.setdefault(
                    (digest, suffix),
                    {
                        "digest": digest,
                        "suffix": suffix,
                        "object_uri": artifact.get("object_uri"),
                    },
                )
    return [records[key] for key in sorted(records)]


def _materialize_artifact_cache(
    records: list[dict[str, Any]],
    cache_directory: Path,
    *,
    downloader: Callable[..., bytes],
    workers: int,
) -> dict[str, Path]:
    """Verify or atomically populate a local SHA-256 artifact cache."""

    cache_directory.mkdir(parents=True, exist_ok=True)

    def materialize(record: Mapping[str, Any]) -> tuple[str, Path]:
        digest = str(record["digest"])
        path = cache_directory / digest[:2] / f"{digest}{record['suffix']}"
        if path.is_file():
            try:
                cached = path.read_bytes()
            except OSError as exc:
                raise WeeklyQuizAssemblyError(
                    f"could not read cached prediction artifact {digest}"
                ) from exc
            if hashlib.sha256(cached).hexdigest() != digest:
                raise WeeklyQuizAssemblyError(
                    f"cached prediction artifact {digest} failed SHA-256 verification"
                )
            return digest, path
        content = downloader(record.get("object_uri"), expected_sha256=digest)
        if not isinstance(content, bytes) or not content:
            raise WeeklyQuizAssemblyError(
                "prediction artifact download returned no bytes"
            )
        if hashlib.sha256(content).hexdigest() != digest:
            raise WeeklyQuizAssemblyError(
                "prediction artifact content does not match SHA-256"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return digest, path

    paths: dict[str, Path] = {}
    if workers == 1 or len(records) <= 1:
        results = [materialize(record) for record in records]
    else:
        ordered: list[tuple[str, Path] | None] = [None] * len(records)
        with ThreadPoolExecutor(
            max_workers=min(workers, len(records)),
            thread_name_prefix="foldarium-artifact-download",
        ) as executor:
            futures = {
                executor.submit(materialize, record): index
                for index, record in enumerate(records)
            }
            try:
                for future in as_completed(futures):
                    ordered[futures[future]] = future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        if any(result is None for result in ordered):
            raise WeeklyQuizAssemblyError("artifact cache population was incomplete")
        results = [result for result in ordered if result is not None]
    for digest, path in results:
        existing = paths.get(digest)
        if existing is not None and existing.read_bytes() != path.read_bytes():
            raise WeeklyQuizAssemblyError(
                f"content-addressed artifact {digest} resolved inconsistently"
            )
        paths[digest] = path
    return paths


def _receptor_medoid_job(
    payload: tuple[
        str,
        str,
        tuple[dict[str, Any], ...],
        dict[str, str],
    ],
) -> tuple[str, dict[str, Any]]:
    """Process-safe robust receptor-medoid calculation for one target."""

    round_id, target_id, serialized_choices, expected_chain_sequences = payload
    try:
        import gemmi
    except (ImportError, ModuleNotFoundError) as exc:
        raise WeeklyQuizAssemblyError(
            "weekly quiz receptor precomputation requires Gemmi"
        ) from exc
    choices: list[dict[str, Any]] = []
    for serialized in serialized_choices:
        path = Path(serialized["artifact_path"])
        structure, model = _load_model(path, gemmi)
        choices.append({**serialized, "structure": structure, "model": model})

    def align_receptors(reference: Any, predicted: Any) -> Mapping[str, Any]:
        return _weekly_receptor_superposition(
            reference,
            predicted,
            expected_chain_sequences=expected_chain_sequences,
        )

    _choice, anchor = _select_receptor_medoid(
        choices,
        round_id=round_id,
        target_id=target_id,
        aligner=align_receptors,
    )
    return target_id, anchor


def _precompute_receptor_medoids(
    grouped: Mapping[str, list[dict[str, Any]]],
    artifact_paths: Mapping[str, Path],
    *,
    round_id: str,
    workers: int,
) -> dict[str, dict[str, Any]]:
    """Calculate independent target medoids concurrently and retain input order."""

    jobs: list[
        tuple[str, str, tuple[dict[str, Any], ...], dict[str, str]]
    ] = []
    for target_id, target_runs in sorted(grouped.items()):
        ordered_runs = sorted(
            target_runs,
            key=lambda row: (0 if row["method"] == "openfold3" else 1, row["run_id"]),
        )
        target = ordered_runs[0]["task_payload"]["target"]
        _task_receptor_complex, expected_chain_sequences = _receptor_complex(target)
        choices: list[dict[str, Any]] = []
        for row in ordered_runs:
            for sample in sorted(
                row["samples"],
                key=lambda value: (value.get("sample_index", 0), value["sample_id"]),
            ):
                artifact = sample["predicted_complex"]
                digest = artifact["sha256"]
                path = artifact_paths.get(digest)
                if path is None:
                    raise WeeklyQuizAssemblyError(
                        f"prediction artifact {digest} is absent from the verified cache"
                    )
                choices.append(
                    {
                        "run_id": row["run_id"],
                        "sample_id": sample["sample_id"],
                        "sample_index": sample.get("sample_index"),
                        "artifact_sha256": digest,
                        "method": row["method"],
                        "method_version": row["method_version"],
                        "artifact_path": str(path),
                    }
                )
        choices.sort(
            key=lambda choice: choice_order_digest(
                round_id,
                target_id,
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            )
        )
        jobs.append(
            (
                round_id,
                target_id,
                tuple(choices),
                expected_chain_sequences,
            )
        )
    if workers == 1 or len(jobs) <= 1:
        results = [_receptor_medoid_job(job) for job in jobs]
    else:
        ordered: list[tuple[str, dict[str, Any]] | None] = [None] * len(jobs)
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
            futures = {
                executor.submit(_receptor_medoid_job, job): index
                for index, job in enumerate(jobs)
            }
            try:
                for future in as_completed(futures):
                    ordered[futures[future]] = future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        if any(result is None for result in ordered):
            raise WeeklyQuizAssemblyError(
                "target receptor-medoid precomputation was incomplete"
            )
        results = [result for result in ordered if result is not None]
    return {target_id: anchor for target_id, anchor in results}


def _select_precomputed_receptor_medoid(
    choices: list[dict[str, Any]],
    *,
    round_id: str,
    target_id: str,
    anchor: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind a process-computed medoid back to the exact in-process choices."""

    ordered = [
        (
            choice_order_digest(
                round_id,
                target_id,
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            ),
            choice,
        )
        for choice in choices
    ]
    ordered.sort(key=lambda row: row[0])
    digests = [digest for digest, _choice in ordered]
    choices = [choice for _digest, choice in ordered]
    totals = anchor.get("total_pairwise_receptor_distances")
    if (
        anchor.get("policy") != RECEPTOR_ANCHOR_POLICY
        or anchor.get("pair_orientation_policy") != RECEPTOR_PAIR_ORIENTATION_POLICY
        or anchor.get("distance_policy") != RECEPTOR_DISTANCE_POLICY
        or anchor.get("choice_order") != digests
        or not isinstance(totals, list)
        or len(totals) != len(digests)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in totals
        )
    ):
        raise WeeklyQuizAssemblyError(
            f"precomputed receptor medoid for {target_id} is not input-bound"
        )
    selected_index = min(
        range(len(choices)), key=lambda index: (float(totals[index]), digests[index])
    )
    if (
        anchor.get("choice_digest") != digests[selected_index]
        or anchor.get("total_pairwise_receptor_distance") != totals[selected_index]
        or not isinstance(anchor.get("distance_matrix_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", anchor["distance_matrix_sha256"])
    ):
        raise WeeklyQuizAssemblyError(
            f"precomputed receptor medoid for {target_id} changed identity"
        )
    return choices[selected_index], deepcopy(dict(anchor))


def select_complete_method_pairs(
    runs: Iterable[Mapping[str, Any]],
    required_methods: frozenset[str] = REQUIRED_METHODS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Select one newest successful run per required method and target.

    ``campaign_prediction_outputs`` returns newest runs first within each
    target/method. Replacement successes must not cause an otherwise complete
    target to disappear from the quiz; only the selected pair is staged.
    """

    by_target: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for raw in runs:
        if not isinstance(raw, Mapping):
            raise WeeklyQuizAssemblyError("campaign outputs must be objects")
        row = deepcopy(dict(raw))
        target_id = row.get("target_id")
        method = row.get("method")
        if not isinstance(target_id, str) or not target_id:
            raise WeeklyQuizAssemblyError("campaign output target_id is invalid")
        if not isinstance(method, str) or not method:
            raise WeeklyQuizAssemblyError("campaign output method is invalid")
        by_target[target_id][method].append(row)

    complete: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    for target_id, method_rows in sorted(by_target.items()):
        methods = set(method_rows)
        if not required_methods.issubset(methods):
            omitted.append({"target_id": target_id, "succeeded_methods": sorted(methods)})
            continue
        for method in sorted(required_methods):
            rows = method_rows[method]
            complete.append(rows[0])
            if len(rows) > 1:
                replacements.append(
                    {
                        "target_id": target_id,
                        "method": method,
                        "selected_run_id": rows[0].get("run_id"),
                        "ignored_run_ids": [row.get("run_id") for row in rows[1:]],
                    }
                )
    return complete, omitted, replacements


def stage_weekly_quiz(
    runs: Iterable[Mapping[str, Any]],
    destination: str | Path,
    *,
    round_id: str,
    campaign_id: str,
    downloader: Callable[..., bytes],
    required_methods: frozenset[str] = REQUIRED_METHODS,
    choice_scorer: Callable[..., Mapping[str, Any]] | None = None,
    choice_batch_scorer: (
        Callable[[tuple[Mapping[str, Any], ...]], Iterable[Mapping[str, Any]]] | None
    ) = None,
    target_workers: int = DEFAULT_TARGET_ALIGNMENT_WORKERS,
    artifact_download_workers: int = DEFAULT_ARTIFACT_DOWNLOAD_WORKERS,
    artifact_cache_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Download private complexes and write a common-frame weekly local stage.

    ``choice_scorer`` retains the original synchronous, per-choice contract.
    ``choice_batch_scorer`` receives an immutable, deterministically ordered
    tuple only after every exact scoring input has been written.  Batch results
    must use that same order; no score is attached to the stage until the whole
    batch has returned and validated successfully.
    """

    if not isinstance(round_id, str) or not round_id or not isinstance(campaign_id, str) or not campaign_id:
        raise WeeklyQuizAssemblyError("round_id and campaign_id are required")
    if choice_scorer is not None and choice_batch_scorer is not None:
        raise WeeklyQuizAssemblyError(
            "choice_scorer and choice_batch_scorer are mutually exclusive"
        )
    if (
        isinstance(target_workers, bool)
        or not isinstance(target_workers, int)
        or not 1 <= target_workers <= MAX_TARGET_ALIGNMENT_WORKERS
    ):
        raise WeeklyQuizAssemblyError(
            f"target_workers must be between 1 and {MAX_TARGET_ALIGNMENT_WORKERS}"
        )
    if (
        isinstance(artifact_download_workers, bool)
        or not isinstance(artifact_download_workers, int)
        or artifact_download_workers < 1
    ):
        raise WeeklyQuizAssemblyError(
            "artifact_download_workers must be a positive integer"
        )
    root = Path(destination).resolve()
    if (root / "stage.json").exists():
        raise WeeklyQuizAssemblyError("stage destination already contains stage.json")
    root.mkdir(parents=True, exist_ok=True)
    gemmi, numpy, Chem = _dependencies()
    grouped = _normalized_runs(runs, required_methods)
    artifact_paths: dict[str, Path] = {}
    precomputed_medoids: dict[str, dict[str, Any]] = {}
    if target_workers > 1 or artifact_cache_directory is not None:
        cache_directory = (
            Path(artifact_cache_directory).resolve()
            if artifact_cache_directory is not None
            else root.with_name(f".{root.name}.input-cache")
        )
        artifact_paths = _materialize_artifact_cache(
            _prediction_artifact_records(grouped),
            cache_directory,
            downloader=downloader,
            workers=artifact_download_workers,
        )

        def cached_downloader(_uri: Any, *, expected_sha256: str) -> bytes:
            path = artifact_paths.get(expected_sha256)
            if path is None:
                raise WeeklyQuizAssemblyError(
                    f"prediction artifact {expected_sha256} is absent from the verified cache"
                )
            try:
                return path.read_bytes()
            except OSError as exc:
                raise WeeklyQuizAssemblyError(
                    f"could not read cached prediction artifact {expected_sha256}"
                ) from exc

        downloader = cached_downloader
    if target_workers > 1:
        precomputed_medoids = _precompute_receptor_medoids(
            grouped,
            artifact_paths,
            round_id=round_id,
            workers=target_workers,
        )
    staged_items: list[dict[str, Any]] = []
    ligand_eligibility_rejections: list[dict[str, Any]] = []
    alignment_warnings: list[dict[str, Any]] = []
    pending_choice_scores: list[
        tuple[dict[str, Any], dict[str, Any]]
    ] = []

    for target_id, target_runs in sorted(grouped.items()):
        ordered_runs = sorted(
            target_runs,
            key=lambda row: (0 if row["method"] == "openfold3" else 1, row["run_id"]),
        )
        target = ordered_runs[0]["task_payload"]["target"]
        if any(row["task_payload"]["target"] != target for row in ordered_runs[1:]):
            raise WeeklyQuizAssemblyError(f"target {target_id} differs across method tasks")
        task_receptor_complex, expected_chain_sequences = _receptor_complex(target)
        component_id, heavy_atom_count, ligand_chains, ligand_smiles = _selected_ligand(target)
        ligand_eligibility = _weekly_ligand_eligibility(
            component_id,
            heavy_atom_count,
            ligand_smiles,
        )
        if not ligand_eligibility["passed"]:
            ligand_eligibility_rejections.append(
                {
                    "target_id": target_id,
                    **ligand_eligibility,
                }
            )
            continue
        for row in ordered_runs:
            expected_version = SUPPORTED_LEGACY_LIGAND_ORDER.get(row["method"])
            if row.get("method_version") != expected_version:
                raise WeeklyQuizAssemblyError(
                    "weekly clustering has no verified ligand atom-order mapping for "
                    f"{row['method']} {row.get('method_version')}"
                )
        raw_choices: list[dict[str, Any]] = []
        for row in ordered_runs:
            for sample in sorted(
                row["samples"], key=lambda value: (value.get("sample_index", 0), value["sample_id"])
            ):
                artifact = sample.get("predicted_complex")
                if not isinstance(artifact, Mapping):
                    raise WeeklyQuizAssemblyError("prediction sample lacks predicted_complex metadata")
                digest = artifact.get("sha256")
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise WeeklyQuizAssemblyError("prediction artifact has no valid SHA-256")
                content = downloader(artifact.get("object_uri"), expected_sha256=digest)
                if not isinstance(content, bytes) or not content:
                    raise WeeklyQuizAssemblyError("prediction artifact download returned no bytes")
                if hashlib.sha256(content).hexdigest() != digest:
                    raise WeeklyQuizAssemblyError("prediction artifact content does not match SHA-256")
                suffix = _artifact_suffix(artifact)
                raw_name = hashlib.sha256(
                    f"{row['run_id']}:{sample['sample_id']}".encode("utf-8")
                ).hexdigest()[:20] + suffix
                raw_path = root / "raw" / target_id / raw_name
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(content)
                structure, model = _load_model(raw_path, gemmi)
                ligand = _prediction_ligand(model, heavy_atom_count, ligand_chains)
                raw_choices.append(
                    {
                        "run_id": row["run_id"],
                        "sample_id": sample["sample_id"],
                        "sample_index": sample.get("sample_index"),
                        "artifact_sha256": digest,
                        "method": row["method"],
                        "method_version": row["method_version"],
                        "structure": structure,
                        "model": model,
                        "ligand": ligand,
                    }
                )

        if len(raw_choices) < len(required_methods):
            raise WeeklyQuizAssemblyError(f"target {target_id} has too few blind choices")
        raw_choices.sort(
            key=lambda choice: choice_order_digest(
                round_id,
                target_id,
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            )
        )
        def align_receptors(reference: Any, predicted: Any) -> Mapping[str, Any]:
            return _weekly_receptor_superposition(
                reference,
                predicted,
                expected_chain_sequences=expected_chain_sequences,
            )

        if target_id in precomputed_medoids:
            reference_choice, receptor_anchor = _select_precomputed_receptor_medoid(
                raw_choices,
                round_id=round_id,
                target_id=target_id,
                anchor=precomputed_medoids[target_id],
            )
        else:
            reference_choice, receptor_anchor = _select_receptor_medoid(
                raw_choices,
                round_id=round_id,
                target_id=target_id,
                aligner=align_receptors,
            )
        reference_model = reference_choice["model"]
        receptor_anchor["task_receptor_complex"] = task_receptor_complex
        prepared_choices: list[tuple[dict[str, Any], dict[str, Any], Any]] = []
        failed_choices: list[dict[str, Any]] = []
        for choice in raw_choices:
            if choice is reference_choice:
                transform = _identity_transform(gemmi)
                try:
                    alignment = dict(
                        _weekly_receptor_superposition(
                            reference_model,
                            reference_model,
                            expected_chain_sequences=expected_chain_sequences,
                        )
                    )
                except EvaluationError as exc:
                    raise WeeklyQuizAssemblyError(
                        f"target {target_id} lacks its exact submitted protein complex in the reference pose"
                    ) from exc
                alignment["receptor_rmsd"] = 0.0
                alignment["receptor_tm_score"] = 1.0
                alignment["receptor_distance"] = 0.0
            else:
                try:
                    alignment = dict(
                        _weekly_receptor_superposition(
                            reference_model,
                            choice["model"],
                            expected_chain_sequences=expected_chain_sequences,
                        )
                    )
                except EvaluationError as exc:
                    raise WeeklyQuizAssemblyError(
                        f"could not align {target_id}/{choice['sample_id']} to the blind reference"
                    ) from exc
                transform = alignment["transform"]
            display_qa = _weekly_display_alignment_qa(
                alignment,
                contact_residue_counts=_ligand_contact_residue_counts(
                    choice["model"], choice["ligand"], numpy
                ),
            )
            alignment["display_qa"] = display_qa
            prepared_choices.append((choice, alignment, transform))
            if not display_qa["passed"]:
                failed_choices.append(
                    {
                        "run_id": choice["run_id"],
                        "sample_id": choice["sample_id"],
                        "sample_index": choice["sample_index"],
                        "artifact_sha256": choice["artifact_sha256"],
                        "method": choice["method"],
                        "method_version": choice["method_version"],
                        "display_qa": display_qa,
                    }
                )
        alignment_warning: dict[str, Any] | None = None
        if failed_choices:
            alignment_warning = {
                "code": DISPLAY_ALIGNMENT_WARNING_CODE,
                "message": DISPLAY_ALIGNMENT_WARNING_MESSAGE,
                "policy": DISPLAY_ALIGNMENT_QA_POLICY,
                "failed_choice_count": len(failed_choices),
            }
            alignment_warnings.append(
                {
                    "target_id": target_id,
                    "reason": "display_alignment_qa_warning",
                    **alignment_warning,
                    "failed_choices": failed_choices,
                }
            )

        reference_choice_index: int | None = None
        choice_rows: list[dict[str, Any]] = []
        pose_coordinates: list[list[list[float]]] = []
        ligands: list[Any] = []
        for index, (choice, alignment, transform) in enumerate(prepared_choices, start=1):
            if choice is reference_choice:
                reference_choice_index = index - 1
            ligand = choice["ligand"]
            ligands.append(ligand)
            ligand_confidence = _ligand_confidence(ligand)
            pose_relative = f"assets/{target_id}/pose-{index}.pdb"
            coordinates = _write_ligand(root / pose_relative, ligand, transform, gemmi)
            pose_coordinates.append(coordinates)
            choice_protein_relative = f"assets/{target_id}/protein-{index}.pdb"
            choice_pocket_relative = f"assets/{target_id}/pocket-{index}.pdb"
            _write_polymer(
                root / choice_protein_relative,
                choice["model"],
                near=None,
                transform=transform,
                gemmi=gemmi,
                numpy=numpy,
            )
            _write_polymer(
                root / choice_pocket_relative,
                choice["model"],
                near=numpy.array(coordinates, dtype=float),
                transform=transform,
                gemmi=gemmi,
                numpy=numpy,
            )
            row = {
                "run_id": choice["run_id"],
                "sample_id": choice["sample_id"],
                "sample_index": choice["sample_index"],
                "artifact_sha256": choice["artifact_sha256"],
                "method": choice["method"],
                "method_version": choice["method_version"],
                "confidence": ligand_confidence,
                "pose_path": pose_relative,
                "protein_path": choice_protein_relative,
                "pocket_path": choice_pocket_relative,
                "alignment": {
                    key: value for key, value in alignment.items() if key != "transform"
                },
            }
            if choice_scorer is not None:
                pose_id = choice_order_digest(
                    round_id,
                    target_id,
                    {
                        "run_id": choice["run_id"],
                        "sample_id": choice["sample_id"],
                        "artifact_sha256": choice["artifact_sha256"],
                    },
                )
                scoring = choice_scorer(
                    protein_path=root / choice_protein_relative,
                    ligand_path=root / pose_relative,
                    ligand_smiles=ligand_smiles,
                    pose_id=pose_id,
                )
                row.update(_choice_scoring_fields(scoring, expected_pose_id=pose_id))
            elif choice_batch_scorer is not None:
                pose_id = choice_order_digest(
                    round_id,
                    target_id,
                    {
                        "run_id": choice["run_id"],
                        "sample_id": choice["sample_id"],
                        "artifact_sha256": choice["artifact_sha256"],
                    },
                )
                pending_choice_scores.append(
                    (
                        row,
                        {
                            "protein_path": root / choice_protein_relative,
                            "ligand_path": root / pose_relative,
                            "ligand_smiles": ligand_smiles,
                            "pose_id": pose_id,
                        },
                    )
                )
            choice_rows.append(row)
        atom_counts = {len(coordinates) for coordinates in pose_coordinates}
        if atom_counts != {heavy_atom_count}:
            raise WeeklyQuizAssemblyError(f"target {target_id} ligand atom counts are inconsistent")
        try:
            distance_matrix, mapping_audit = _pairwise_pose_distances(
                ligands,
                pose_coordinates,
                ligand_smiles=ligand_smiles,
                numpy=numpy,
                Chem=Chem,
            )
        except WeeklyQuizAssemblyError as exc:
            raise WeeklyQuizAssemblyError(
                f"could not cluster target {target_id}: {exc}"
            ) from exc
        identities = [
            {
                "run_id": choice["run_id"],
                "sample_id": choice["sample_id"],
                "artifact_sha256": choice["artifact_sha256"],
            }
            for choice in choice_rows
        ]
        try:
            assignments, clustering = cluster_distance_matrix(
                round_id,
                target_id,
                identities,
                distance_matrix,
            )
        except PoseClusteringError as exc:
            raise WeeklyQuizAssemblyError(
                f"could not cluster blind poses for {target_id}"
            ) from exc
        mapping_audit["choices"] = [
            {
                "choice_digest": choice_order_digest(
                    round_id,
                    target_id,
                    {
                        "run_id": choice["run_id"],
                        "sample_id": choice["sample_id"],
                        "artifact_sha256": choice["artifact_sha256"],
                    },
                ),
                "method": choice["method"],
                "method_version": choice["method_version"],
                "mapping_mode": "source-heavy-atom-index-order",
            }
            for choice in choice_rows
        ]
        clustering["ligand_atom_mapping"] = mapping_audit
        clustering["receptor_anchor"] = receptor_anchor
        for choice, assignment in zip(choice_rows, assignments):
            choice["cluster_id"] = assignment["cluster_id"]
            choice["is_rep"] = assignment["is_rep"]
            if assignment["choice_digest"] != choice_order_digest(
                round_id,
                target_id,
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "artifact_sha256": choice["artifact_sha256"],
                },
            ):
                raise WeeklyQuizAssemblyError("clustering choice identity changed during assembly")
        pose_cloud = numpy.array(
            [coordinate for pose in pose_coordinates for coordinate in pose], dtype=float
        )
        if reference_choice_index is None:
            raise WeeklyQuizAssemblyError("selected receptor medoid disappeared during assembly")
        # The shared receptor is the prediction-set medoid. It is only
        # an all-overlay comparison frame and is never an experimental answer.
        protein_relative = choice_rows[reference_choice_index]["protein_path"]
        pocket_relative = f"assets/{target_id}/overlay-pocket.pdb"
        _write_polymer(
            root / pocket_relative,
            reference_model,
            near=pose_cloud,
            transform=_identity_transform(gemmi),
            gemmi=gemmi,
            numpy=numpy,
        )
        source = target.get("source") if isinstance(target.get("source"), Mapping) else {}
        staged_item = {
            "id": target_id,
            "target_id": target_id,
            "week": source.get("week"),
            "ligand": {
                "component_id": component_id,
                "heavy_atoms": heavy_atom_count,
            },
            "ligand_eligibility": ligand_eligibility,
            "selector_target": _sanitize_selector_target(target),
            "protein_path": protein_relative,
            "pocket_path": pocket_relative,
            "clustering": clustering,
            "choices": choice_rows,
        }
        staged_item["presentation_group"] = _weekly_presentation_group(
            int(clustering["cluster_count"])
        )
        if alignment_warning is not None:
            staged_item["alignment_warning"] = alignment_warning
        staged_items.append(staged_item)

    if not staged_items:
        raise WeeklyQuizAssemblyError(
            "the current ligand eligibility policy rejected every complete target"
        )

    staged_items.sort(key=_weekly_presentation_key)

    if choice_batch_scorer is not None:
        requests = tuple(request for _, request in pending_choice_scores)
        for request in requests:
            if not Path(request["protein_path"]).is_file() or not Path(
                request["ligand_path"]
            ).is_file():
                raise WeeklyQuizAssemblyError(
                    "batch pose scoring input disappeared before dispatch"
                )
        try:
            results = list(choice_batch_scorer(requests))
        except Exception as exc:
            raise WeeklyQuizAssemblyError("batch pose scoring failed") from exc
        if len(results) != len(pending_choice_scores):
            raise WeeklyQuizAssemblyError(
                "batch pose scorer returned the wrong number of results"
            )
        validated_updates = [
            _choice_scoring_fields(result, expected_pose_id=request["pose_id"])
            for (_, request), result in zip(pending_choice_scores, results)
        ]
        for (row, _), updates in zip(pending_choice_scores, validated_updates):
            row.update(updates)

    stage = {
        "schema_version": WEEKLY_QUIZ_STAGE_VERSION,
        "round_id": round_id,
        "campaign_id": campaign_id,
        "required_methods": sorted(required_methods),
        "ligand_eligibility_policy": SELECTION_POLICY_VERSION,
        "ligand_eligibility_rejections": ligand_eligibility_rejections,
        "presentation_policy": WEEKLY_PRESENTATION_POLICY,
        "items": staged_items,
        "alignment_warnings": alignment_warnings,
    }
    stage["stage_sha256"] = hashlib.sha256(canonical_json(stage).encode("utf-8")).hexdigest()
    (root / "stage.json").write_text(
        json.dumps(stage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return stage


def _aware_timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WeeklyQuizAssemblyError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise WeeklyQuizAssemblyError(f"{field} must include a timezone")
    return value


def _validate_staged_ligand_eligibility(
    stage: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Recompute current ligand eligibility before publication touches storage."""

    if stage.get("ligand_eligibility_policy") != SELECTION_POLICY_VERSION:
        raise WeeklyQuizAssemblyError("stage ligand eligibility policy is not current")
    if stage.get("presentation_policy") != WEEKLY_PRESENTATION_POLICY:
        raise WeeklyQuizAssemblyError("stage weekly presentation policy is not current")
    items = stage.get("items")
    if not isinstance(items, list) or not items:
        raise WeeklyQuizAssemblyError("stage items must be a non-empty list")
    staged_ids: set[str] = set()
    normalized_items: list[Mapping[str, Any]] = []
    for item in items:
        target_id = item.get("target_id") if isinstance(item, Mapping) else None
        eligibility = item.get("ligand_eligibility") if isinstance(item, Mapping) else None
        clustering = item.get("clustering") if isinstance(item, Mapping) else None
        cluster_count = clustering.get("cluster_count") if isinstance(clustering, Mapping) else None
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id in staged_ids
            or not isinstance(eligibility, Mapping)
        ):
            raise WeeklyQuizAssemblyError("staged ligand eligibility provenance is invalid")
        recomputed = _weekly_ligand_eligibility(
            eligibility.get("component_id"),
            eligibility.get("heavy_atoms"),
            eligibility.get("smiles"),
        )
        if dict(eligibility) != recomputed or recomputed["passed"] is not True:
            raise WeeklyQuizAssemblyError(
                "staged item does not pass the current ligand eligibility policy"
            )
        expected_group = _weekly_presentation_group(cluster_count)
        if item.get("presentation_group") != expected_group:
            raise WeeklyQuizAssemblyError(
                "staged item presentation group does not match its cluster count"
            )
        staged_ids.add(target_id)
        normalized_items.append(item)
    if normalized_items != sorted(normalized_items, key=_weekly_presentation_key):
        raise WeeklyQuizAssemblyError(
            "staged items must place multi-cluster systems before single-cluster systems"
        )

    rejections = stage.get("ligand_eligibility_rejections")
    if not isinstance(rejections, list):
        raise WeeklyQuizAssemblyError("stage ligand_eligibility_rejections must be a list")
    rejected_ids: set[str] = set()
    normalized_rejections: list[Mapping[str, Any]] = []
    for rejection in rejections:
        target_id = rejection.get("target_id") if isinstance(rejection, Mapping) else None
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id in staged_ids
            or target_id in rejected_ids
        ):
            raise WeeklyQuizAssemblyError("stage ligand eligibility rejection is invalid")
        recomputed = _weekly_ligand_eligibility(
            rejection.get("component_id"),
            rejection.get("heavy_atoms"),
            rejection.get("smiles"),
        )
        expected = {"target_id": target_id, **recomputed}
        if dict(rejection) != expected or recomputed["passed"] is not False:
            raise WeeklyQuizAssemblyError(
                "stage ligand eligibility rejection does not match the current policy"
            )
        rejected_ids.add(target_id)
        normalized_rejections.append(rejection)
    return normalized_items, normalized_rejections


def _validate_staged_display_alignment_qa(
    stage: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Validate every alignment classification before publication touches storage."""

    items = stage.get("items")
    if not isinstance(items, list) or not items:
        raise WeeklyQuizAssemblyError("stage items must be a non-empty list")
    staged_target_ids: set[str] = set()
    observed_warning_choices: dict[str, list[dict[str, Any]]] = {}
    normalized_items: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise WeeklyQuizAssemblyError("stage items must be objects")
        target_id = item.get("target_id")
        if not isinstance(target_id, str) or not target_id or target_id in staged_target_ids:
            raise WeeklyQuizAssemblyError("stage target IDs must be non-empty and unique")
        staged_target_ids.add(target_id)
        choices = item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise WeeklyQuizAssemblyError("stage items must contain blind choices")
        failed_choices: list[dict[str, Any]] = []
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise WeeklyQuizAssemblyError("stage choices must be objects")
            alignment = choice.get("alignment")
            display_qa = alignment.get("display_qa") if isinstance(alignment, Mapping) else None
            contact_chains = (
                display_qa.get("contact_chains")
                if isinstance(display_qa, Mapping)
                else None
            )
            if (
                not isinstance(display_qa, Mapping)
                or display_qa.get("policy") != DISPLAY_ALIGNMENT_QA_POLICY
                or not isinstance(contact_chains, list)
            ):
                raise WeeklyQuizAssemblyError(
                    "staged choice lacks a valid display alignment QA result"
                )
            contact_residue_counts: dict[str, int] = {}
            for row in contact_chains:
                chain_id = row.get("chain_id") if isinstance(row, Mapping) else None
                count = row.get("contact_residue_count") if isinstance(row, Mapping) else None
                if (
                    not isinstance(chain_id, str)
                    or not chain_id
                    or chain_id in contact_residue_counts
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                ):
                    raise WeeklyQuizAssemblyError(
                        "staged choice display alignment contact provenance is invalid"
                    )
                contact_residue_counts[chain_id] = count
            recomputed_qa = _weekly_display_alignment_qa(
                alignment,
                contact_residue_counts=contact_residue_counts,
            )
            if dict(display_qa) != recomputed_qa:
                raise WeeklyQuizAssemblyError(
                    "staged choice display alignment QA does not match its provenance"
                )
            if recomputed_qa.get("passed") is not True:
                failed_choices.append(
                    {
                        "run_id": choice.get("run_id"),
                        "sample_id": choice.get("sample_id"),
                        "sample_index": choice.get("sample_index"),
                        "artifact_sha256": choice.get("artifact_sha256"),
                        "method": choice.get("method"),
                        "method_version": choice.get("method_version"),
                        "display_qa": recomputed_qa,
                    }
                )
        warning = item.get("alignment_warning")
        expected_warning = (
            {
                "code": DISPLAY_ALIGNMENT_WARNING_CODE,
                "message": DISPLAY_ALIGNMENT_WARNING_MESSAGE,
                "policy": DISPLAY_ALIGNMENT_QA_POLICY,
                "failed_choice_count": len(failed_choices),
            }
            if failed_choices
            else None
        )
        if warning != expected_warning:
            raise WeeklyQuizAssemblyError(
                "staged item alignment warning does not match its choice QA results"
            )
        if failed_choices:
            observed_warning_choices[target_id] = failed_choices
        normalized_items.append(item)

    alignment_warnings = stage.get("alignment_warnings")
    if not isinstance(alignment_warnings, list):
        raise WeeklyQuizAssemblyError("stage alignment_warnings must be a list")
    warned_target_ids: set[str] = set()
    normalized_warnings: list[Mapping[str, Any]] = []
    for warning in alignment_warnings:
        if not isinstance(warning, Mapping):
            raise WeeklyQuizAssemblyError("stage alignment warnings must be objects")
        target_id = warning.get("target_id")
        failed_choices = warning.get("failed_choices")
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id in warned_target_ids
            or target_id not in staged_target_ids
            or warning.get("policy") != DISPLAY_ALIGNMENT_QA_POLICY
            or warning.get("reason") != "display_alignment_qa_warning"
            or warning.get("code") != DISPLAY_ALIGNMENT_WARNING_CODE
            or warning.get("message") != DISPLAY_ALIGNMENT_WARNING_MESSAGE
            or not isinstance(failed_choices, list)
            or not failed_choices
            or warning.get("failed_choice_count") != len(failed_choices)
            or failed_choices != observed_warning_choices.get(target_id)
        ):
            raise WeeklyQuizAssemblyError("stage alignment warning provenance is invalid")
        for failed_choice in failed_choices:
            failed_qa = (
                failed_choice.get("display_qa")
                if isinstance(failed_choice, Mapping)
                else None
            )
            if (
                not isinstance(failed_qa, Mapping)
                or failed_qa.get("policy") != DISPLAY_ALIGNMENT_QA_POLICY
                or failed_qa.get("passed") is not False
                or not isinstance(failed_qa.get("failures"), list)
                or not failed_qa["failures"]
            ):
                raise WeeklyQuizAssemblyError(
                    "stage alignment warning lacks a failed QA result"
                )
        warned_target_ids.add(target_id)
        normalized_warnings.append(warning)
    if warned_target_ids != set(observed_warning_choices):
        raise WeeklyQuizAssemblyError(
            "stage alignment warnings do not cover every failed choice QA result"
        )
    return normalized_items, normalized_warnings


def _sanitize_selector_target(raw_target: Mapping[str, Any]) -> dict[str, Any]:
    """Return a leak-safe normalized target for selector kit publication."""

    try:
        normalized = validate_target(raw_target)
    except Exception as exc:
        raise WeeklyQuizAssemblyError("selector target is invalid") from exc
    try:
        assert_no_forbidden_content(normalized, path="selector_target")
    except WeeklySelectorError as exc:
        raise WeeklyQuizAssemblyError(str(exc)) from exc
    return normalized


def _selector_storage_path(bucket: str, digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WeeklyQuizAssemblyError("selector kit digest must be a SHA-256 hex string")
    return f"{bucket}/sha256/{digest[:2]}/{digest}"


def _selector_targets_from_stage_items(
    stage_items: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for item in stage_items:
        if not isinstance(item, Mapping):
            raise WeeklyQuizAssemblyError("stage items must be objects")
        item_id = item.get("id")
        selector_target = item.get("selector_target")
        if not isinstance(item_id, str) or not item_id:
            raise WeeklyQuizAssemblyError("stage item id is required for selector kits")
        if not isinstance(selector_target, Mapping):
            raise WeeklyQuizAssemblyError(
                f"stage item {item_id} lacks a normalized selector target"
            )
        targets[item_id] = _sanitize_selector_target(selector_target)
    if not targets:
        raise WeeklyQuizAssemblyError("selector targets must be non-empty")
    return targets


def _selector_assets_from_stage(
    root: Path,
    blind_manifest: Mapping[str, Any],
    stage_items: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, bytes]]:
    stage_by_id = {
        item["id"]: item
        for item in stage_items
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    assets: dict[tuple[str, str], dict[str, bytes]] = {}
    blind_items = blind_manifest.get("items")
    if not isinstance(blind_items, list) or not blind_items:
        raise WeeklyQuizAssemblyError("blind manifest has no selector items")
    for blind_item in blind_items:
        if not isinstance(blind_item, Mapping):
            raise WeeklyQuizAssemblyError("blind manifest item is invalid")
        item_id = blind_item.get("id")
        stage_item = stage_by_id.get(item_id)
        if not isinstance(item_id, str) or stage_item is None:
            raise WeeklyQuizAssemblyError(
                f"blind manifest item {item_id!r} is absent from the stage"
            )
        choices = blind_item.get("choices")
        stage_choices = stage_item.get("choices")
        if not isinstance(choices, list) or not isinstance(stage_choices, list):
            raise WeeklyQuizAssemblyError(f"stage item {item_id} has invalid choices")
        stage_choice_by_id: dict[str, Mapping[str, Any]] = {}
        for stage_choice in stage_choices:
            if not isinstance(stage_choice, Mapping):
                raise WeeklyQuizAssemblyError(f"stage item {item_id} has invalid choices")
            run_id = stage_choice.get("run_id")
            sample_id = stage_choice.get("sample_id")
            if not isinstance(run_id, str) or not isinstance(sample_id, str):
                raise WeeklyQuizAssemblyError(
                    f"stage item {item_id} choice lacks run/sample identity"
                )
            choice_id = stable_id(
                "choice",
                {
                    "round_id": blind_manifest["round_id"],
                    "item_id": item_id,
                    "run_id": run_id,
                    "sample_id": sample_id,
                },
                length=16,
            )
            if choice_id in stage_choice_by_id:
                raise WeeklyQuizAssemblyError(
                    f"stage item {item_id} contains duplicate blind choice identities"
                )
            stage_choice_by_id[choice_id] = stage_choice
        for blind_choice in choices:
            if not isinstance(blind_choice, Mapping):
                raise WeeklyQuizAssemblyError("blind manifest choice is invalid")
            choice_id = blind_choice.get("id")
            if not isinstance(choice_id, str) or not choice_id:
                raise WeeklyQuizAssemblyError("blind manifest choice id is invalid")
            stage_choice = stage_choice_by_id.get(choice_id)
            if stage_choice is None:
                raise WeeklyQuizAssemblyError(
                    f"blind choice {item_id}/{choice_id} is absent from the stage"
                )
            asset_bytes: dict[str, bytes] = {}
            for kind, field in (
                ("pose", "pose_path"),
                ("protein", "protein_path"),
                ("pocket", "pocket_path"),
            ):
                path = _safe_path(root, stage_choice.get(field), f"choice.{field}")
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise WeeklyQuizAssemblyError(
                        f"selector asset {item_id}/{choice_id}/{kind} could not be read"
                    ) from exc
                if not content:
                    raise WeeklyQuizAssemblyError(
                        f"selector asset {item_id}/{choice_id}/{kind} is empty"
                    )
                asset_bytes[kind] = content
            assets[(item_id, choice_id)] = asset_bytes
    return assets


def _selector_assets_from_blind_manifest(
    blind_manifest: Mapping[str, Any],
    *,
    downloader: Callable[[str], bytes],
) -> dict[tuple[str, str], dict[str, bytes]]:
    assets: dict[tuple[str, str], dict[str, bytes]] = {}
    blind_items = blind_manifest.get("items")
    if not isinstance(blind_items, list) or not blind_items:
        raise WeeklyQuizAssemblyError("blind manifest has no selector items")
    for blind_item in blind_items:
        if not isinstance(blind_item, Mapping):
            raise WeeklyQuizAssemblyError("blind manifest item is invalid")
        item_id = blind_item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise WeeklyQuizAssemblyError("blind manifest item id is invalid")
        choices = blind_item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise WeeklyQuizAssemblyError(f"blind item {item_id} has no choices")
        for blind_choice in choices:
            if not isinstance(blind_choice, Mapping):
                raise WeeklyQuizAssemblyError("blind manifest choice is invalid")
            choice_id = blind_choice.get("id")
            if not isinstance(choice_id, str) or not choice_id:
                raise WeeklyQuizAssemblyError("blind manifest choice id is invalid")
            asset_bytes: dict[str, bytes] = {}
            for kind, uri_key in (
                ("pose", "pose_uri"),
                ("protein", "protein_uri"),
                ("pocket", "pocket_uri"),
            ):
                uri = blind_choice.get(uri_key)
                if not isinstance(uri, str) or not uri.strip():
                    raise WeeklyQuizAssemblyError(
                        f"blind choice {item_id}/{choice_id} lacks {uri_key}"
                    )
                content = None
                last_error: Exception | None = None
                for attempt in range(SELECTOR_ASSET_DOWNLOAD_ATTEMPTS):
                    try:
                        content = downloader(uri.strip())
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt + 1 < SELECTOR_ASSET_DOWNLOAD_ATTEMPTS:
                            time.sleep(
                                SELECTOR_ASSET_DOWNLOAD_RETRY_SECONDS * (2**attempt)
                            )
                if content is None and last_error is not None:
                    raise WeeklyQuizAssemblyError(
                        "selector asset download failed after "
                        f"{SELECTOR_ASSET_DOWNLOAD_ATTEMPTS} attempts for "
                        f"{item_id}/{choice_id}/{kind}"
                    ) from last_error
                if not isinstance(content, bytes) or not content:
                    raise WeeklyQuizAssemblyError(
                        f"selector asset download for {item_id}/{choice_id}/{kind} is empty"
                    )
                asset_bytes[kind] = content
            assets[(item_id, choice_id)] = asset_bytes
    return assets


def _build_selector_kit_bundle(
    *,
    round_id: str,
    blind_manifest: Mapping[str, Any],
    targets_by_item_id: Mapping[str, Mapping[str, Any]],
    assets_by_choice: Mapping[tuple[str, str], Mapping[str, bytes]],
) -> tuple[bytes, dict[str, Any]]:
    try:
        return build_selector_kit(
            round_id=round_id,
            blind_manifest=blind_manifest,
            targets_by_item_id=targets_by_item_id,
            assets_by_choice=assets_by_choice,
        )
    except WeeklySelectorError as exc:
        raise WeeklyQuizAssemblyError(str(exc)) from exc


def build_staged_selector_kit(
    stage_directory: str | Path,
    blind_manifest: Mapping[str, Any],
    *,
    stage_items: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[bytes, dict[str, Any], dict[str, dict[str, Any]]]:
    """Build a deterministic selector kit from one local weekly quiz stage."""

    root = Path(stage_directory).resolve()
    if stage_items is None:
        try:
            stage = json.loads((root / "stage.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeeklyQuizAssemblyError("stage.json is missing or invalid") from exc
        stage_items = stage.get("items")
    if not isinstance(stage_items, list) or not stage_items:
        raise WeeklyQuizAssemblyError("stage items must be a non-empty list")
    round_id = blind_manifest.get("round_id")
    if not isinstance(round_id, str) or not round_id:
        raise WeeklyQuizAssemblyError("blind manifest round_id is required")
    targets_by_item_id = _selector_targets_from_stage_items(stage_items)
    assets_by_choice = _selector_assets_from_stage(root, blind_manifest, stage_items)
    zip_bytes, descriptor = _build_selector_kit_bundle(
        round_id=round_id,
        blind_manifest=blind_manifest,
        targets_by_item_id=targets_by_item_id,
        assets_by_choice=assets_by_choice,
    )
    return zip_bytes, descriptor, targets_by_item_id


def load_selector_targets_from_metadata(
    metadata: Mapping[str, Any],
    *,
    coordinator: Any,
) -> dict[str, dict[str, Any]]:
    pointer = metadata.get("selector_targets")
    if not isinstance(pointer, Mapping):
        raise WeeklyQuizAssemblyError("weekly round metadata lacks selector_targets")
    object_uri = pointer.get("object_uri")
    digest = pointer.get("sha256")
    if not isinstance(object_uri, str) or not isinstance(digest, str):
        raise WeeklyQuizAssemblyError("selector_targets pointer is invalid")
    content = coordinator.download_content_object(object_uri, expected_sha256=digest)
    try:
        raw = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise WeeklyQuizAssemblyError("selector_targets object is not valid JSON") from exc
    if not isinstance(raw, Mapping) or not raw:
        raise WeeklyQuizAssemblyError("selector_targets object must be a non-empty map")
    return {
        item_id: _sanitize_selector_target(raw_target)
        for item_id, raw_target in raw.items()
        if isinstance(item_id, str) and isinstance(raw_target, Mapping)
    }


def selector_targets_from_campaign_tasks(
    round_row: Mapping[str, Any],
    *,
    coordinator: Any,
) -> dict[str, dict[str, Any]]:
    """Recover leak-safe targets for a round published before selector metadata."""

    campaign_id = round_row.get("campaign_id")
    blind_manifest = round_row.get("blind_manifest")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise WeeklyQuizAssemblyError("weekly round campaign_id is required for target recovery")
    if not isinstance(blind_manifest, Mapping):
        raise WeeklyQuizAssemblyError("weekly round blind_manifest is required for target recovery")
    blind_items = blind_manifest.get("items")
    if not isinstance(blind_items, list) or not blind_items:
        raise WeeklyQuizAssemblyError("weekly round has no blind items for target recovery")

    recovered_by_target_id: dict[str, dict[str, Any]] = {}
    for row in coordinator.campaign_prediction_run_statuses(campaign_id):
        if not isinstance(row, Mapping):
            raise WeeklyQuizAssemblyError("campaign task recovery returned an invalid row")
        target_id = row.get("target_id")
        task_payload = row.get("task_payload")
        raw_target = task_payload.get("target") if isinstance(task_payload, Mapping) else None
        if not isinstance(target_id, str) or not isinstance(raw_target, Mapping):
            raise WeeklyQuizAssemblyError("campaign task recovery row lacks its target")
        normalized = _sanitize_selector_target(raw_target)
        if normalized.get("target_id") != target_id:
            raise WeeklyQuizAssemblyError("campaign task target identity is inconsistent")
        existing = recovered_by_target_id.get(target_id)
        if existing is not None and canonical_json(existing) != canonical_json(normalized):
            raise WeeklyQuizAssemblyError(
                f"campaign task targets disagree for {target_id}"
            )
        recovered_by_target_id[target_id] = normalized

    targets_by_item_id: dict[str, dict[str, Any]] = {}
    for blind_item in blind_items:
        if not isinstance(blind_item, Mapping):
            raise WeeklyQuizAssemblyError("weekly blind item is invalid during target recovery")
        item_id = blind_item.get("id")
        if not isinstance(item_id, str):
            raise WeeklyQuizAssemblyError("weekly blind item lacks target identity")
        target = recovered_by_target_id.get(item_id)
        if target is None:
            raise WeeklyQuizAssemblyError(
                f"campaign tasks contain no selector target for {item_id}"
            )
        targets_by_item_id[item_id] = target
    return targets_by_item_id


def _selector_targets_for_round(
    round_row: Mapping[str, Any],
    *,
    coordinator: Any,
) -> dict[str, dict[str, Any]]:
    metadata = round_row.get("metadata")
    if not isinstance(metadata, Mapping):
        raise WeeklyQuizAssemblyError("weekly round metadata is required")
    if isinstance(metadata.get("selector_targets"), Mapping):
        return load_selector_targets_from_metadata(metadata, coordinator=coordinator)
    return selector_targets_from_campaign_tasks(round_row, coordinator=coordinator)


def publish_selector_kit(
    *,
    round_id: str,
    blind_manifest_sha256: str,
    zip_bytes: bytes,
    descriptor: Mapping[str, Any],
    public_coordinator: Any,
    private_coordinator: Any,
    selector_targets: Mapping[str, Mapping[str, Any]] | None = None,
    register_catalog: bool = True,
) -> dict[str, Any]:
    """Upload one selector kit ZIP and optionally register its catalog row."""

    if not isinstance(round_id, str) or not round_id:
        raise WeeklyQuizAssemblyError("round_id is required for selector kit publication")
    if not re.fullmatch(r"[0-9a-f]{64}", blind_manifest_sha256):
        raise WeeklyQuizAssemblyError("blind_manifest_sha256 must be a SHA-256 hex string")
    kit_sha256 = descriptor.get("kit_sha256")
    if not isinstance(kit_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", kit_sha256):
        raise WeeklyQuizAssemblyError("selector kit descriptor lacks a valid kit_sha256")
    try:
        parsed = parse_selector_kit(zip_bytes)
    except WeeklySelectorError as exc:
        raise WeeklyQuizAssemblyError(str(exc)) from exc
    if parsed["kit_sha256"] != kit_sha256:
        raise WeeklyQuizAssemblyError("selector kit ZIP manifest does not match descriptor")
    stored = public_coordinator.store_bytes(
        zip_bytes,
        SELECTOR_KIT_ZIP_MEDIA_TYPE,
        cache_control=IMMUTABLE_PUBLIC_CACHE_CONTROL,
    )
    storage_path = _selector_storage_path(public_coordinator.storage_bucket, stored["sha256"])
    catalog_descriptor = {
        **dict(descriptor),
        "storage_path": storage_path,
        "blind_manifest_sha256": blind_manifest_sha256,
    }
    selector_targets_object = None
    if selector_targets is not None:
        selector_targets_object = private_coordinator.store_bytes(
            (canonical_json(dict(selector_targets)) + "\n").encode("utf-8"),
            SELECTOR_TARGETS_JSON_MEDIA_TYPE,
        )
    registration = None
    if register_catalog:
        registration = private_coordinator.register_weekly_selector_kit(
            round_id=round_id,
            kit_sha256=kit_sha256,
            item_count=int(descriptor["item_count"]),
            byte_size=len(zip_bytes),
            storage_path=storage_path,
            descriptor=catalog_descriptor,
            blind_manifest_sha256=blind_manifest_sha256,
        )
    return {
        "round_id": round_id,
        "kit_sha256": kit_sha256,
        "item_count": descriptor["item_count"],
        "choice_count": descriptor.get("choice_count"),
        "byte_size": len(zip_bytes),
        "storage_path": storage_path,
        "object_uri": stored["object_uri"],
        "descriptor": catalog_descriptor,
        "selector_targets": selector_targets_object,
        "registration": registration,
        "registered": register_catalog,
    }


def publish_staged_selector_kit(
    stage_directory: str | Path,
    blind_manifest: Mapping[str, Any],
    *,
    public_coordinator: Any,
    private_coordinator: Any,
    stage_items: Iterable[Mapping[str, Any]] | None = None,
    register_catalog: bool = True,
) -> dict[str, Any]:
    """Build and publish one selector kit from a local weekly quiz stage."""

    zip_bytes, descriptor, targets_by_item_id = build_staged_selector_kit(
        stage_directory,
        blind_manifest,
        stage_items=stage_items,
    )
    return publish_selector_kit(
        round_id=str(blind_manifest["round_id"]),
        blind_manifest_sha256=manifest_sha256(blind_manifest),
        zip_bytes=zip_bytes,
        descriptor=descriptor,
        public_coordinator=public_coordinator,
        private_coordinator=private_coordinator,
        selector_targets=targets_by_item_id,
        register_catalog=register_catalog,
    )


def publish_selector_kit_from_blind_manifest(
    blind_manifest: Mapping[str, Any],
    targets_by_item_id: Mapping[str, Mapping[str, Any]],
    *,
    asset_downloader: Callable[[str], bytes],
    public_coordinator: Any,
    private_coordinator: Any,
    register_catalog: bool = True,
) -> dict[str, Any]:
    """Rebuild and publish one round-bound selector kit from a blind manifest."""

    round_id = blind_manifest.get("round_id")
    if not isinstance(round_id, str) or not round_id:
        raise WeeklyQuizAssemblyError("blind manifest round_id is required")
    assets_by_choice = _selector_assets_from_blind_manifest(
        blind_manifest,
        downloader=asset_downloader,
    )
    zip_bytes, descriptor = _build_selector_kit_bundle(
        round_id=round_id,
        blind_manifest=blind_manifest,
        targets_by_item_id={
            item_id: _sanitize_selector_target(raw_target)
            for item_id, raw_target in targets_by_item_id.items()
        },
        assets_by_choice=assets_by_choice,
    )
    return publish_selector_kit(
        round_id=round_id,
        blind_manifest_sha256=manifest_sha256(blind_manifest),
        zip_bytes=zip_bytes,
        descriptor=descriptor,
        public_coordinator=public_coordinator,
        private_coordinator=private_coordinator,
        selector_targets=targets_by_item_id,
        register_catalog=register_catalog,
    )


def regenerate_promoted_selector_kit(
    *,
    source_round: Mapping[str, Any],
    source_metadata: Mapping[str, Any],
    promoted_blind_manifest: Mapping[str, Any],
    public_coordinator: Any,
    private_coordinator: Any,
    register_catalog: bool = True,
) -> dict[str, Any]:
    """Regenerate a promoted round's selector kit instead of reusing source ZIP bytes."""

    source_with_metadata = {**dict(source_round), "metadata": dict(source_metadata)}
    targets_by_item_id = _selector_targets_for_round(
        source_with_metadata, coordinator=private_coordinator
    )
    blind_item_ids = {
        item["id"]
        for item in promoted_blind_manifest.get("items", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if not blind_item_ids:
        raise WeeklyQuizAssemblyError("promoted blind manifest has no selector items")
    missing_targets = blind_item_ids.difference(targets_by_item_id)
    if missing_targets:
        raise WeeklyQuizAssemblyError(
            "promoted selector items lack normalized targets: "
            + ", ".join(sorted(missing_targets))
        )
    filtered_targets = {
        item_id: targets_by_item_id[item_id]
        for item_id in sorted(blind_item_ids)
    }
    return publish_selector_kit_from_blind_manifest(
        promoted_blind_manifest,
        filtered_targets,
        asset_downloader=public_coordinator.download_content_object,
        public_coordinator=public_coordinator,
        private_coordinator=private_coordinator,
        register_catalog=register_catalog,
    )


def backfill_selector_kit_for_round(
    round_row: Mapping[str, Any],
    *,
    public_coordinator: Any,
    private_coordinator: Any,
    register_catalog: bool = True,
) -> dict[str, Any]:
    """Publish a selector kit for one already-open weekly round."""

    metadata = round_row.get("metadata")
    if not isinstance(metadata, Mapping):
        raise WeeklyQuizAssemblyError("weekly round metadata is required for selector backfill")
    blind_manifest = round_row.get("blind_manifest")
    if not isinstance(blind_manifest, Mapping):
        raise WeeklyQuizAssemblyError("weekly round blind_manifest is required for backfill")
    targets_by_item_id = _selector_targets_for_round(
        round_row, coordinator=private_coordinator
    )
    return publish_selector_kit_from_blind_manifest(
        blind_manifest,
        targets_by_item_id,
        asset_downloader=public_coordinator.download_content_object,
        public_coordinator=public_coordinator,
        private_coordinator=private_coordinator,
        register_catalog=register_catalog,
    )


def clone_weekly_quiz_manifests(
    blind_manifest: Mapping[str, Any],
    private_index: Mapping[str, Any],
    *,
    round_id: str,
    include_item_ids: Iterable[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebind one verified blind/private manifest pair to a replacement round.

    Content-addressed pose assets and opaque item/choice IDs remain unchanged;
    only the round identity and its digest binding are replaced.
    """

    if not isinstance(round_id, str) or not round_id.strip():
        raise WeeklyQuizAssemblyError("replacement round_id is required")
    blind = deepcopy(dict(blind_manifest))
    private = deepcopy(dict(private_index))
    source_round_id = blind.get("round_id")
    if (
        blind.get("schema_version") != private.get("schema_version")
        or not isinstance(source_round_id, str)
        or private.get("round_id") != source_round_id
        or private.get("blind_manifest_sha256") != manifest_sha256(blind)
    ):
        raise WeeklyQuizAssemblyError("source weekly manifests are not digest-bound")

    def manifest_ids(value: Mapping[str, Any]) -> dict[str, set[str]]:
        items = value.get("items")
        if not isinstance(items, list) or not items:
            raise WeeklyQuizAssemblyError("source weekly manifest has no items")
        result: dict[str, set[str]] = {}
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise WeeklyQuizAssemblyError("source weekly manifest item is invalid")
            choices = item.get("choices")
            if not isinstance(choices, list) or not choices:
                raise WeeklyQuizAssemblyError("source weekly manifest item has no choices")
            ids = {
                choice.get("id")
                for choice in choices
                if isinstance(choice, Mapping) and isinstance(choice.get("id"), str)
            }
            if len(ids) != len(choices) or item["id"] in result:
                raise WeeklyQuizAssemblyError("source weekly manifest IDs are invalid")
            result[item["id"]] = ids
        return result

    blind_ids = manifest_ids(blind)
    if blind_ids != manifest_ids(private):
        raise WeeklyQuizAssemblyError("source blind and private manifest IDs differ")
    if include_item_ids is not None:
        if isinstance(include_item_ids, (str, bytes)):
            raise WeeklyQuizAssemblyError("included weekly item IDs must be an iterable")
        included = {
            item_id.strip()
            for item_id in include_item_ids
            if isinstance(item_id, str) and item_id.strip()
        }
        if not included:
            raise WeeklyQuizAssemblyError("replacement weekly round cannot be empty")
        unknown = included.difference(blind_ids)
        if unknown:
            raise WeeklyQuizAssemblyError(
                "replacement weekly item IDs are absent from the source: "
                + ", ".join(sorted(unknown))
            )
        blind["items"] = [item for item in blind["items"] if item["id"] in included]
        private["items"] = [item for item in private["items"] if item["id"] in included]
    blind["round_id"] = round_id.strip()
    private["round_id"] = round_id.strip()
    private["blind_manifest_sha256"] = manifest_sha256(blind)
    return blind, private


def _validated_public_object(
    result: Any,
    *,
    content: bytes,
    media_type: str,
    bucket: str,
) -> dict[str, Any]:
    """Bind a Storage result to the exact content-addressed upload request."""

    digest = hashlib.sha256(content).hexdigest()
    expected = {
        "object_uri": f"supabase://{bucket}/sha256/{digest[:2]}/{digest}",
        "sha256": digest,
        "size_bytes": len(content),
        "media_type": media_type,
    }
    if not isinstance(result, Mapping) or any(
        result.get(key) != value for key, value in expected.items()
    ):
        raise WeeklyQuizAssemblyError(
            "public Storage upload result does not match its content digest"
        )
    return expected


def _store_public_objects_concurrently(
    requests: list[dict[str, Any]],
    *,
    public_coordinator: Any,
    workers: int,
) -> dict[str, dict[str, Any]]:
    """Store ordered, unique public objects with bounded concurrency."""

    if not requests:
        return {}

    def store(request: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        content = request["content"]
        media_type = request["media_type"]
        result = public_coordinator.store_bytes(
            content,
            media_type,
            cache_control=IMMUTABLE_PUBLIC_CACHE_CONTROL,
        )
        return request["key"], _validated_public_object(
            result,
            content=content,
            media_type=media_type,
            bucket=public_coordinator.storage_bucket,
        )

    ordered: list[tuple[str, dict[str, Any]] | None] = [None] * len(requests)
    if workers == 1 or len(requests) == 1:
        results = [store(request) for request in requests]
    else:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(requests)),
            thread_name_prefix="foldarium-public-upload",
        ) as executor:
            futures = {
                executor.submit(store, request): index
                for index, request in enumerate(requests)
            }
            try:
                for future in as_completed(futures):
                    ordered[futures[future]] = future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        if any(result is None for result in ordered):
            raise WeeklyQuizAssemblyError(
                "public Storage upload batch returned incomplete results"
            )
        results = [result for result in ordered if result is not None]
    return {key: stored for key, stored in results}


def publish_staged_weekly_quiz(
    stage_directory: str | Path,
    *,
    private_coordinator: Any,
    public_coordinator: Any,
    opens_at: str,
    closes_at: str,
    open_round: bool = False,
    round_environment: str = "production",
    round_metadata: Mapping[str, Any] | None = None,
    public_upload_workers: int = DEFAULT_PUBLIC_UPLOAD_WORKERS,
) -> dict[str, Any]:
    """Upload sanitized assets; optionally atomically open the blind voting round."""

    root = Path(stage_directory).resolve()
    try:
        stage = json.loads((root / "stage.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeeklyQuizAssemblyError("stage.json is missing or invalid") from exc
    if stage.get("schema_version") != WEEKLY_QUIZ_STAGE_VERSION:
        raise WeeklyQuizAssemblyError("unsupported weekly quiz stage version")
    declared_digest = stage.get("stage_sha256")
    unhashed = {key: value for key, value in stage.items() if key != "stage_sha256"}
    actual_digest = hashlib.sha256(canonical_json(unhashed).encode("utf-8")).hexdigest()
    if declared_digest != actual_digest:
        raise WeeklyQuizAssemblyError("stage_sha256 does not match stage.json")
    opens_at = _aware_timestamp(opens_at, "opens_at")
    closes_at = _aware_timestamp(closes_at, "closes_at")
    if datetime.fromisoformat(closes_at.replace("Z", "+00:00")) <= datetime.fromisoformat(
        opens_at.replace("Z", "+00:00")
    ):
        raise WeeklyQuizAssemblyError("closes_at must be after opens_at")
    if round_environment not in WEEKLY_QUIZ_ENVIRONMENTS:
        raise WeeklyQuizAssemblyError(
            "round_environment must be production, preview, or development"
        )
    if (
        isinstance(public_upload_workers, bool)
        or not isinstance(public_upload_workers, int)
        or not 1 <= public_upload_workers <= MAX_PUBLIC_UPLOAD_WORKERS
    ):
        raise WeeklyQuizAssemblyError(
            f"public_upload_workers must be between 1 and {MAX_PUBLIC_UPLOAD_WORKERS}"
        )

    # Validate the full stage before the first bucket check or object upload.
    # This keeps a tampered or legacy stage strictly fail-closed.
    eligible_items, ligand_eligibility_rejections = (
        _validate_staged_ligand_eligibility(stage)
    )
    stage_items, alignment_warnings = _validate_staged_display_alignment_qa(stage)
    if stage_items != eligible_items:
        raise WeeklyQuizAssemblyError(
            "stage ligand eligibility and display alignment items differ"
        )
    supplied_metadata = dict(round_metadata or {})
    reserved_metadata = {
        "stage_sha256",
        "private_index",
        "public_quiz_bucket",
        "selector_targets",
        "selector_kit",
        "display_alignment_qa_policy",
        "display_alignment_warnings",
        "display_alignment_warned_target_ids",
        "ligand_eligibility_policy",
        "ligand_eligibility_rejections",
        "ligand_eligibility_rejected_target_ids",
        "weekly_presentation_policy",
    }
    overlap = reserved_metadata.intersection(supplied_metadata)
    if overlap:
        raise WeeklyQuizAssemblyError(
            "round_metadata cannot override publication metadata: "
            + ", ".join(sorted(overlap))
        )

    # Service-role uploads also succeed for private buckets, but the browser
    # resolves every URI below through Supabase's unauthenticated public object
    # endpoint. Verify visibility before the first upload.
    public_coordinator.require_public_bucket()

    upload_requests: list[dict[str, Any]] = []
    upload_request_keys: set[str] = set()
    prepared_items: list[dict[str, Any]] = []

    def prepare_asset(path: Path, field: str) -> str:
        if not path.is_file():
            raise WeeklyQuizAssemblyError(f"staged {field} asset is missing")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise WeeklyQuizAssemblyError(f"staged {field} asset could not be read") from exc
        if not content:
            raise WeeklyQuizAssemblyError(f"staged {field} asset is empty")
        media_type = "chemical/x-pdb"
        digest = hashlib.sha256(content).hexdigest()
        key = f"{media_type}:{digest}"
        if key not in upload_request_keys:
            upload_request_keys.add(key)
            upload_requests.append(
                {
                    "key": key,
                    "content": content,
                    "media_type": media_type,
                }
            )
        return key

    # Resolve and read every declared public asset before the first upload.
    # This prevents a late missing file from leaving a partially published batch.
    for item in stage_items:
        protein = _safe_path(root, item.get("protein_path"), "item.protein_path")
        pocket = _safe_path(root, item.get("pocket_path"), "item.pocket_path")
        protein_key = prepare_asset(protein, "protein")
        pocket_key = prepare_asset(pocket, "pocket")
        prepared_choices: list[dict[str, Any]] = []
        for choice in item.get("choices", []):
            if not isinstance(choice, Mapping):
                raise WeeklyQuizAssemblyError("stage choices must be objects")
            pose = _safe_path(root, choice.get("pose_path"), "choice.pose_path")
            choice_protein = _safe_path(
                root, choice.get("protein_path"), "choice.protein_path"
            )
            choice_pocket = _safe_path(
                root, choice.get("pocket_path"), "choice.pocket_path"
            )
            prepared_choices.append(
                {
                    "choice": choice,
                    "pose_key": prepare_asset(pose, "choice pose"),
                    "protein_key": prepare_asset(
                        choice_protein, "choice protein"
                    ),
                    "pocket_key": prepare_asset(choice_pocket, "choice pocket"),
                }
            )
        prepared_items.append(
            {
                "item": item,
                "protein_key": protein_key,
                "pocket_key": pocket_key,
                "choices": prepared_choices,
            }
        )

    try:
        public_objects = _store_public_objects_concurrently(
            upload_requests,
            public_coordinator=public_coordinator,
            workers=public_upload_workers,
        )
    except WeeklyQuizAssemblyError:
        raise
    except Exception as exc:
        raise WeeklyQuizAssemblyError("public Storage upload batch failed") from exc

    manifest_items: list[dict[str, Any]] = []
    for prepared_item in prepared_items:
        item = prepared_item["item"]
        protein_object = public_objects[prepared_item["protein_key"]]
        pocket_object = public_objects[prepared_item["pocket_key"]]
        choices: list[dict[str, Any]] = []
        for prepared_choice in prepared_item["choices"]:
            choice = prepared_choice["choice"]
            pose_object = public_objects[prepared_choice["pose_key"]]
            choice_protein_object = public_objects[prepared_choice["protein_key"]]
            choice_pocket_object = public_objects[prepared_choice["pocket_key"]]
            published_choice = {
                    "run_id": choice.get("run_id"),
                    "sample_id": choice.get("sample_id"),
                    "sample_index": choice.get("sample_index"),
                    "artifact_sha256": choice.get("artifact_sha256"),
                    "method": choice.get("method"),
                    "method_version": choice.get("method_version"),
                    "confidence": choice.get("confidence"),
                    "pose_uri": pose_object["object_uri"],
                    "protein_uri": choice_protein_object["object_uri"],
                    "pocket_uri": choice_pocket_object["object_uri"],
                    "media_type": "chemical/x-pdb",
                    "cluster_id": choice.get("cluster_id"),
                    "is_rep": choice.get("is_rep"),
                    "alignment": choice.get("alignment"),
                }
            for metric_field in ("smina_score", "interaction_count", "scoring"):
                if choice.get(metric_field) is not None:
                    published_choice[metric_field] = choice[metric_field]
            choices.append(published_choice)
        manifest_item = {
            "id": item.get("id"),
            "target_id": item.get("target_id"),
            "week": item.get("week"),
            "ligand": item.get("ligand"),
            "ligand_eligibility": item.get("ligand_eligibility"),
            "protein_uri": protein_object["object_uri"],
            "pocket_uri": pocket_object["object_uri"],
            "clustering": item.get("clustering"),
            "choices": choices,
        }
        item_metadata = {
            "presentation": {
                "policy": WEEKLY_PRESENTATION_POLICY,
                "group": item["presentation_group"],
                "cluster_count": item["clustering"]["cluster_count"],
            }
        }
        warning = item.get("alignment_warning")
        if isinstance(warning, Mapping):
            item_metadata["display_alignment"] = {
                "code": warning["code"],
                "message": warning["message"],
            }
        manifest_item["metadata"] = item_metadata
        manifest_items.append(manifest_item)

    blind, private_index = build_blind_manifest(stage["round_id"], manifest_items)
    _order_weekly_manifests(
        blind,
        private_index,
        (item["id"] for item in manifest_items),
    )
    selector_kit = publish_staged_selector_kit(
        root,
        blind,
        public_coordinator=public_coordinator,
        private_coordinator=private_coordinator,
        stage_items=stage_items,
        register_catalog=False,
    )
    private_object = private_coordinator.store_bytes(
        canonical_json(private_index).encode("utf-8"), "application/json"
    )
    warning_object = private_coordinator.store_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "round_id": stage["round_id"],
                "policy": DISPLAY_ALIGNMENT_QA_POLICY,
                "warnings": alignment_warnings,
            }
        ).encode("utf-8"),
        "application/json",
    )
    eligibility_rejection_object = private_coordinator.store_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "round_id": stage["round_id"],
                "policy": SELECTION_POLICY_VERSION,
                "rejections": ligand_eligibility_rejections,
            }
        ).encode("utf-8"),
        "application/json",
    )
    ligand_eligibility_rejected_target_ids = sorted(
        row["target_id"] for row in ligand_eligibility_rejections
    )
    metadata = {
        **supplied_metadata,
        "stage_sha256": declared_digest,
        "private_index": private_object,
        "public_quiz_bucket": public_coordinator.storage_bucket,
        "selector_targets": selector_kit["selector_targets"],
        "selector_kit": {
            "kit_sha256": selector_kit["kit_sha256"],
            "item_count": selector_kit["item_count"],
            "byte_size": selector_kit["byte_size"],
            "storage_path": selector_kit["storage_path"],
            "object_uri": selector_kit["object_uri"],
            "registered": selector_kit["registered"],
        },
        "display_alignment_qa_policy": DISPLAY_ALIGNMENT_QA_POLICY,
        "display_alignment_warnings": warning_object,
        "display_alignment_warned_target_ids": sorted(
            row.get("target_id")
            for row in alignment_warnings
            if isinstance(row, Mapping) and isinstance(row.get("target_id"), str)
        ),
        "ligand_eligibility_policy": SELECTION_POLICY_VERSION,
        "ligand_eligibility_rejections": eligibility_rejection_object,
        "ligand_eligibility_rejected_target_ids": (
            ligand_eligibility_rejected_target_ids
        ),
        "weekly_presentation_policy": WEEKLY_PRESENTATION_POLICY,
    }
    response: Any = {"status": "uploaded-not-opened"}
    if open_round:
        response = private_coordinator.open_weekly_quiz_round(
            round_id=stage["round_id"],
            campaign_id=stage["campaign_id"],
            opens_at=opens_at,
            closes_at=closes_at,
            blind_manifest=blind,
            metadata=metadata,
            environment=round_environment,
        )
        selector_kit["registration"] = private_coordinator.register_weekly_selector_kit(
            round_id=stage["round_id"],
            kit_sha256=selector_kit["kit_sha256"],
            item_count=int(selector_kit["item_count"]),
            byte_size=int(selector_kit["byte_size"]),
            storage_path=selector_kit["storage_path"],
            descriptor=selector_kit["descriptor"],
            blind_manifest_sha256=manifest_sha256(blind),
        )
        selector_kit["registered"] = True
        metadata["selector_kit"]["registered"] = True
    (root / "blind-manifest.json").write_text(
        json.dumps(blind, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "private-index.json").write_text(
        json.dumps(private_index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "opened" if open_round else "uploaded-not-opened",
        "round_id": stage["round_id"],
        "environment": round_environment,
        "item_count": len(blind["items"]),
        "choice_count": sum(len(item["choices"]) for item in blind["items"]),
        "display_alignment_warned_target_count": len(alignment_warnings),
        "display_alignment_warned_target_ids": metadata[
            "display_alignment_warned_target_ids"
        ],
        "ligand_eligibility_rejected_target_count": len(
            ligand_eligibility_rejections
        ),
        "ligand_eligibility_rejected_target_ids": (
            ligand_eligibility_rejected_target_ids
        ),
        "multi_cluster_item_count": sum(
            item["presentation_group"] == WEEKLY_PRESENTATION_MULTI_CLUSTER
            for item in stage_items
        ),
        "single_cluster_item_count": sum(
            item["presentation_group"] == WEEKLY_PRESENTATION_SINGLE_CLUSTER
            for item in stage_items
        ),
        "blind_manifest_sha256": manifest_sha256(blind),
        "private_index": private_object,
        "selector_kit": selector_kit,
        "open_response": response,
    }


__all__ = [
    "DISPLAY_ALIGNMENT_QA_POLICY",
    "DISPLAY_ALIGNMENT_WARNING_CODE",
    "DISPLAY_ALIGNMENT_WARNING_MESSAGE",
    "POCKET_RADIUS_ANGSTROM",
    "LIGAND_CONFIDENCE_AGGREGATION",
    "LIGAND_CONFIDENCE_METRIC",
    "PROLIF_COUNT_METRIC",
    "REQUIRED_METHODS",
    "SMINA_SCORE_METRIC",
    "WEEKLY_QUIZ_STAGE_VERSION",
    "WEEKLY_QUIZ_ENVIRONMENTS",
    "WeeklyQuizAssemblyError",
    "backfill_selector_kit_for_round",
    "build_staged_selector_kit",
    "publish_selector_kit",
    "publish_selector_kit_from_blind_manifest",
    "publish_staged_selector_kit",
    "publish_staged_weekly_quiz",
    "regenerate_promoted_selector_kit",
    "select_complete_method_pairs",
    "stage_weekly_quiz",
]
