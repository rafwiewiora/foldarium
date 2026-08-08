"""Coordinator-side Saturday assembly of aligned, method-blind quiz assets."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from .clustering import (
    PoseClusteringError,
    choice_order_digest,
    cluster_distance_matrix,
)
from .contracts import canonical_json, validate_prediction_task
from .evaluation import (
    EvaluationError,
    _mapped_rmsd,
    best_receptor_superposition,
)
from .quiz import build_blind_manifest, manifest_sha256

WEEKLY_QUIZ_STAGE_VERSION = 2
POCKET_RADIUS_ANGSTROM = 5.0
REQUIRED_METHODS = frozenset({"openfold3", "boltz2"})
LEGACY_LIGAND_ORDER_POLICY = "adapter-preserved-task-smiles-heavy-atom-order/legacy-v1"
SUPPORTED_LEGACY_LIGAND_ORDER = {
    "openfold3": "0.4.4",
    "boltz2": "2.2.1",
}
LIGAND_AUTOMORPHISM_CAP = 100_000
RECEPTOR_ANCHOR_POLICY = "minimum-total-pairwise-receptor-rmsd-medoid/v1"


class WeeklyQuizAssemblyError(RuntimeError):
    """Raised when completed predictions cannot form a safe blind round."""


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
    source_molecule = Chem.MolFromSmiles(ligand_smiles)
    if source_molecule is None:
        raise WeeklyQuizAssemblyError(
            "selected ligand task SMILES could not be parsed for clustering"
        )
    source_molecule = Chem.RemoveHs(source_molecule)
    expected_elements = [atom.GetAtomicNum() for atom in source_molecule.GetAtoms()]
    if not expected_elements:
        raise WeeklyQuizAssemblyError("selected ligand task SMILES has no heavy atoms")
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

    # Construct an element-labelled connectivity graph from the task SMILES.
    # Bond order is deliberately collapsed so aromatic/resonance annotation
    # cannot differ across method writers, while adjacency remains exact.
    topology_builder = Chem.RWMol()
    for atomic_number in expected_elements:
        atom = Chem.Atom(int(atomic_number))
        atom.SetNoImplicit(True)
        topology_builder.AddAtom(atom)
    topology_edges: list[list[int]] = []
    for bond in source_molecule.GetBonds():
        left, right = sorted((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))
        topology_builder.AddBond(left, right, Chem.BondType.SINGLE)
        topology_edges.append([int(left), int(right)])
    topology_edges.sort()
    topology = topology_builder.GetMol()
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

    topology_payload = {
        "atomic_numbers": expected_elements,
        "edges": topology_edges,
    }
    mapping_audit = {
        "policy": LEGACY_LIGAND_ORDER_POLICY,
        "source_smiles_sha256": hashlib.sha256(
            ligand_smiles.encode("utf-8")
        ).hexdigest(),
        "source_topology_sha256": hashlib.sha256(
            canonical_json(topology_payload).encode("utf-8")
        ).hexdigest(),
        "heavy_atom_count": len(expected_elements),
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
    """Choose the method-blind prediction closest to all other receptors."""

    if not choices:
        raise WeeklyQuizAssemblyError("cannot select a receptor medoid without choices")
    digests = [
        choice_order_digest(
            round_id,
            target_id,
            {
                "run_id": choice["run_id"],
                "sample_id": choice["sample_id"],
                "artifact_sha256": choice["artifact_sha256"],
            },
        )
        for choice in choices
    ]
    matrix = [[0.0 for _ in choices] for _ in choices]
    for reference_index, reference in enumerate(choices):
        for predicted_index, predicted in enumerate(choices):
            if reference_index == predicted_index:
                continue
            try:
                alignment = aligner(reference["model"], predicted["model"])
                rmsd = float(alignment["receptor_rmsd"])
            except (EvaluationError, KeyError, TypeError, ValueError) as exc:
                raise WeeklyQuizAssemblyError(
                    "could not compare receptors while selecting the shared medoid for "
                    f"{target_id}"
                ) from exc
            if not math.isfinite(rmsd) or rmsd < 0:
                raise WeeklyQuizAssemblyError(
                    "receptor-medoid RMSD must be finite and non-negative"
                )
            matrix[reference_index][predicted_index] = rmsd
    totals = [sum(row) for row in matrix]
    medoid_index = min(range(len(choices)), key=lambda index: (totals[index], digests[index]))
    distance_payload = {
        "choice_order": digests,
        "distances_angstrom": [
            [f"{value:.6f}" for value in row]
            for row in matrix
        ],
    }
    return choices[medoid_index], {
        "policy": RECEPTOR_ANCHOR_POLICY,
        "choice_digest": digests[medoid_index],
        "total_pairwise_receptor_rmsd": totals[medoid_index],
        "choice_order": digests,
        "total_pairwise_receptor_rmsds": totals,
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
) -> dict[str, Any]:
    """Download private complexes and write a common-frame, method-blind local stage."""

    if not isinstance(round_id, str) or not round_id or not isinstance(campaign_id, str) or not campaign_id:
        raise WeeklyQuizAssemblyError("round_id and campaign_id are required")
    root = Path(destination).resolve()
    if (root / "stage.json").exists():
        raise WeeklyQuizAssemblyError("stage destination already contains stage.json")
    root.mkdir(parents=True, exist_ok=True)
    gemmi, numpy, Chem = _dependencies()
    grouped = _normalized_runs(runs, required_methods)
    staged_items: list[dict[str, Any]] = []

    for target_id, target_runs in sorted(grouped.items()):
        ordered_runs = sorted(
            target_runs,
            key=lambda row: (0 if row["method"] == "openfold3" else 1, row["run_id"]),
        )
        target = ordered_runs[0]["task_payload"]["target"]
        if any(row["task_payload"]["target"] != target for row in ordered_runs[1:]):
            raise WeeklyQuizAssemblyError(f"target {target_id} differs across method tasks")
        component_id, heavy_atom_count, ligand_chains, ligand_smiles = _selected_ligand(target)
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
                media_type = str(artifact.get("media_type") or "chemical/x-mmcif")
                suffix = ".pdb" if "pdb" in media_type.lower() else ".cif"
                raw_name = hashlib.sha256(
                    f"{row['run_id']}:{sample['sample_id']}".encode("utf-8")
                ).hexdigest()[:20] + suffix
                raw_path = root / "raw" / target_id / raw_name
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(content)
                structure, model = _load_model(raw_path, gemmi)
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
        reference_choice, receptor_anchor = _select_receptor_medoid(
            raw_choices,
            round_id=round_id,
            target_id=target_id,
        )
        reference_model = reference_choice["model"]
        reference_choice_index: int | None = None
        choice_rows: list[dict[str, Any]] = []
        pose_coordinates: list[list[list[float]]] = []
        ligands: list[Any] = []
        for index, choice in enumerate(raw_choices, start=1):
            if choice is reference_choice:
                reference_choice_index = index - 1
                transform = _identity_transform(gemmi)
                alignment = {
                    "reference_chain": None,
                    "predicted_chain": None,
                    "sequence_similarity": 1.0,
                    "receptor_rmsd": 0.0,
                }
            else:
                try:
                    alignment = best_receptor_superposition(reference_model, choice["model"])
                except EvaluationError as exc:
                    raise WeeklyQuizAssemblyError(
                        f"could not align {target_id}/{choice['sample_id']} to the blind reference"
                    ) from exc
                transform = alignment["transform"]
            ligand = _prediction_ligand(choice["model"], heavy_atom_count, ligand_chains)
            ligands.append(ligand)
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
            choice_rows.append(
                {
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "sample_index": choice["sample_index"],
                    "artifact_sha256": choice["artifact_sha256"],
                    "method": choice["method"],
                    "method_version": choice["method_version"],
                    "pose_path": pose_relative,
                    "protein_path": choice_protein_relative,
                    "pocket_path": choice_pocket_relative,
                    "alignment": {
                        key: value for key, value in alignment.items() if key != "transform"
                    },
                }
            )
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
        # The shared receptor is the method-blind prediction medoid. It is only
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
        staged_items.append(
            {
                "id": target_id,
                "target_id": target_id,
                "week": source.get("week"),
                "ligand": {
                    "component_id": component_id,
                    "heavy_atoms": heavy_atom_count,
                },
                "protein_path": protein_relative,
                "pocket_path": pocket_relative,
                "clustering": clustering,
                "choices": choice_rows,
            }
        )

    stage = {
        "schema_version": WEEKLY_QUIZ_STAGE_VERSION,
        "round_id": round_id,
        "campaign_id": campaign_id,
        "required_methods": sorted(required_methods),
        "items": staged_items,
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


def publish_staged_weekly_quiz(
    stage_directory: str | Path,
    *,
    private_coordinator: Any,
    public_coordinator: Any,
    opens_at: str,
    closes_at: str,
    open_round: bool = False,
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

    # Service-role uploads also succeed for private buckets, but the browser
    # resolves every URI below through Supabase's unauthenticated public object
    # endpoint. Verify visibility before the first upload.
    public_coordinator.require_public_bucket()

    manifest_items: list[dict[str, Any]] = []
    for item in stage.get("items", []):
        if not isinstance(item, Mapping):
            raise WeeklyQuizAssemblyError("stage items must be objects")
        protein = _safe_path(root, item.get("protein_path"), "item.protein_path")
        pocket = _safe_path(root, item.get("pocket_path"), "item.pocket_path")
        if not protein.is_file() or not pocket.is_file():
            raise WeeklyQuizAssemblyError("staged protein/pocket asset is missing")
        protein_object = public_coordinator.store_bytes(protein.read_bytes(), "chemical/x-pdb")
        pocket_object = public_coordinator.store_bytes(pocket.read_bytes(), "chemical/x-pdb")
        choices: list[dict[str, Any]] = []
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
            if not pose.is_file() or not choice_protein.is_file() or not choice_pocket.is_file():
                raise WeeklyQuizAssemblyError("staged choice pose/protein/pocket asset is missing")
            pose_object = public_coordinator.store_bytes(pose.read_bytes(), "chemical/x-pdb")
            choice_protein_object = public_coordinator.store_bytes(
                choice_protein.read_bytes(), "chemical/x-pdb"
            )
            choice_pocket_object = public_coordinator.store_bytes(
                choice_pocket.read_bytes(), "chemical/x-pdb"
            )
            choices.append(
                {
                    "run_id": choice.get("run_id"),
                    "sample_id": choice.get("sample_id"),
                    "sample_index": choice.get("sample_index"),
                    "artifact_sha256": choice.get("artifact_sha256"),
                    "method": choice.get("method"),
                    "method_version": choice.get("method_version"),
                    "pose_uri": pose_object["object_uri"],
                    "protein_uri": choice_protein_object["object_uri"],
                    "pocket_uri": choice_pocket_object["object_uri"],
                    "media_type": "chemical/x-pdb",
                    "cluster_id": choice.get("cluster_id"),
                    "is_rep": choice.get("is_rep"),
                    "alignment": choice.get("alignment"),
                }
            )
        manifest_items.append(
            {
                "id": item.get("id"),
                "target_id": item.get("target_id"),
                "week": item.get("week"),
                "ligand": item.get("ligand"),
                "protein_uri": protein_object["object_uri"],
                "pocket_uri": pocket_object["object_uri"],
                "clustering": item.get("clustering"),
                "choices": choices,
            }
        )

    blind, private_index = build_blind_manifest(stage["round_id"], manifest_items)
    private_object = private_coordinator.store_bytes(
        canonical_json(private_index).encode("utf-8"), "application/json"
    )
    metadata = {
        "stage_sha256": declared_digest,
        "private_index": private_object,
        "public_quiz_bucket": public_coordinator.storage_bucket,
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
        )
    (root / "blind-manifest.json").write_text(
        json.dumps(blind, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / "private-index.json").write_text(
        json.dumps(private_index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "opened" if open_round else "uploaded-not-opened",
        "round_id": stage["round_id"],
        "item_count": len(blind["items"]),
        "choice_count": sum(len(item["choices"]) for item in blind["items"]),
        "blind_manifest_sha256": manifest_sha256(blind),
        "private_index": private_object,
        "open_response": response,
    }


__all__ = [
    "POCKET_RADIUS_ANGSTROM",
    "REQUIRED_METHODS",
    "WEEKLY_QUIZ_STAGE_VERSION",
    "WeeklyQuizAssemblyError",
    "publish_staged_weekly_quiz",
    "select_complete_method_pairs",
    "stage_weekly_quiz",
]
