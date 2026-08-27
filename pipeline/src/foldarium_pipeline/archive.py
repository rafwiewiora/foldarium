"""Released-CAMEO catch-up policy and static quiz asset exporter.

The archive path deliberately shares target/ligand selection and the Wednesday
evaluator with the weekly workflow.  Network discovery stays outside this
module, which keeps classification and generated quiz data replayable in unit
tests and from a retained coordinate cache.
"""

from __future__ import annotations

import math
import re
from statistics import median
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .cameo import af3_availability, af3_import_manifest
from .evaluation import EVALUATOR_VERSION, EvaluationError
from .intake import WeeklyPolicy, target_from_cameo

CLUSTER_RMSD_ANGSTROM = 2.0
SINGLE_POCKET_ANGSTROM = 8.0
POCKET_RADIUS_ANGSTROM = 5.0
MINIMUM_SCORED_MODELS = 3
ARCHIVE_POLICY_VERSION = "cameo-static-quiz/v2"
OFFICIAL_CAMEO_EVALUATOR_VERSION = "cameo-ligand-pose-bisyrmsd/public-raw-v1"


class ArchiveError(ValueError):
    """Raised when released CAMEO data cannot form a reproducible quiz item."""


def _rmsd(left: Sequence[Sequence[float]], right: Sequence[Sequence[float]]) -> float:
    if not left or len(left) != len(right):
        raise ArchiveError("pose coordinate arrays must have the same non-zero length")
    total = 0.0
    for left_atom, right_atom in zip(left, right):
        if len(left_atom) != 3 or len(right_atom) != 3:
            raise ArchiveError("pose coordinates must be XYZ triples")
        total += sum((float(a) - float(b)) ** 2 for a, b in zip(left_atom, right_atom))
    return math.sqrt(total / len(left))


def cluster_poses(
    coordinates: Sequence[Sequence[Sequence[float]]],
    threshold: float = CLUSTER_RMSD_ANGSTROM,
) -> tuple[list[int], dict[int, int]]:
    """Match the historical greedy clustering and medoid rule."""

    if not coordinates:
        raise ArchiveError("at least one pose is required")
    if not math.isfinite(threshold) or threshold <= 0:
        raise ArchiveError("cluster threshold must be positive")
    atom_count = len(coordinates[0])
    if atom_count < 1 or any(len(pose) != atom_count for pose in coordinates):
        raise ArchiveError("every pose must contain the same non-zero atom count")
    labels = [-1] * len(coordinates)
    cluster_id = 0
    for index in range(len(coordinates)):
        if labels[index] >= 0:
            continue
        labels[index] = cluster_id
        for other in range(index + 1, len(coordinates)):
            if labels[other] < 0 and _rmsd(coordinates[index], coordinates[other]) < threshold:
                labels[other] = cluster_id
        cluster_id += 1
    medoids: dict[int, int] = {}
    for current in range(cluster_id):
        members = [index for index, label in enumerate(labels) if label == current]
        medoids[current] = min(
            members,
            key=lambda index: sum(_rmsd(coordinates[index], coordinates[other]) for other in members),
        )
    return labels, medoids


def _centroid(coordinates: Sequence[Sequence[float]]) -> tuple[float, float, float]:
    return tuple(
        sum(float(atom[axis]) for atom in coordinates) / len(coordinates) for axis in range(3)
    )


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left, right)))


def classify_pose_ensemble(scored_models: Mapping[int, Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the historical loose buckets and single-pocket filter."""

    if not isinstance(scored_models, Mapping):
        raise ArchiveError("scored_models must be a mapping")
    samples = sorted(scored_models)
    if len(samples) < MINIMUM_SCORED_MODELS:
        return {"eligible": False, "reason": "fewer-than-three-scored-models"}
    coordinates: list[Sequence[Sequence[float]]] = []
    rmsds: list[float] = []
    for sample in samples:
        score = scored_models[sample]
        rmsd = score.get("rmsd")
        coords = score.get("predicted_ligand_coordinates_reference_order")
        if (
            isinstance(rmsd, bool)
            or not isinstance(rmsd, (int, float))
            or not math.isfinite(float(rmsd))
            or float(rmsd) < 0
            or not isinstance(coords, list)
        ):
            raise ArchiveError(f"model {sample} has an invalid evaluator result")
        rmsds.append(float(rmsd))
        coordinates.append(coords)
    labels, medoids = cluster_poses(coordinates)
    representative_indices = [medoids[index] for index in sorted(medoids)]
    centroids = [_centroid(coordinates[index]) for index in representative_indices]
    spread = max(
        (
            _distance(centroids[left], centroids[right])
            for left in range(len(centroids))
            for right in range(left + 1, len(centroids))
        ),
        default=0.0,
    )
    if spread >= SINGLE_POCKET_ANGSTROM:
        return {
            "eligible": False,
            "reason": "multi-pocket",
            "centroid_spread": spread,
            "labels": labels,
            "medoids": medoids,
        }

    correct = [value < CLUSTER_RMSD_ANGSTROM for value in rmsds]
    representative_correct = [correct[index] for index in representative_indices]
    if all(correct):
        bucket = "all-correct"
    elif not any(correct):
        bucket = "all-wrong"
    elif len(medoids) >= 2 and any(representative_correct) and not all(representative_correct):
        bucket = "game-able"
    else:
        return {
            "eligible": False,
            "reason": "trivial-mixed-ensemble",
            "centroid_spread": spread,
            "labels": labels,
            "medoids": medoids,
        }
    return {
        "eligible": True,
        "bucket": bucket,
        "samples": samples,
        "labels": labels,
        "medoids": medoids,
        "centroid_spread": spread,
        "correct": correct,
        "n_clusters": len(medoids),
    }


def build_archive_candidate(
    payload: Mapping[str, Any], *, heavy_atom_minimum: int = 15
) -> dict[str, Any] | None:
    """Normalize one released, protein-only public AF3 target for evaluation."""

    try:
        target = target_from_cameo(
            payload,
            WeeklyPolicy(heavy_atom_minimum=heavy_atom_minimum, protein_only=True),
        )
    except ValueError:
        return None
    if target is None:
        return None
    if {entity["type"] for entity in target["entities"] if entity["type"] != "ligand"} != {
        "protein"
    }:
        return None
    source = target["source"]
    pdb_id = source.get("pdb_id")
    if not isinstance(pdb_id, str) or not pdb_id.strip() or not pdb_id.strip().isalnum():
        return None
    try:
        availability = af3_availability(payload)
        manifest = af3_import_manifest(payload)
    except ValueError:
        return None
    if not availability["advertised_models"] or not manifest["references"]:
        return None
    selected_ligand = target["metadata"]["selected_ligand"]
    return {
        "policy_version": ARCHIVE_POLICY_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "target_id": target["target_id"],
        "pdb_id": pdb_id.strip().upper(),
        "week": source["week"],
        "component_id": selected_ligand["component_id"],
        "heavy_atoms": selected_ligand["heavy_atoms"],
        "prediction_target": target,
        "coordinate_manifest": manifest,
    }


def build_static_quiz_item(
    candidate: Mapping[str, Any],
    scored_models: Mapping[int, Mapping[str, Any]],
    classification: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the established static JSON schema after assets have been exported."""

    evaluator_versions = {
        str(score.get("evaluator_version") or candidate["evaluator_version"])
        for score in scored_models.values()
    }
    if len(evaluator_versions) != 1:
        raise ArchiveError("scored models must use one evaluator version")
    evaluator_version = evaluator_versions.pop()
    if not classification.get("eligible"):
        raise ArchiveError("cannot build a quiz item from an ineligible ensemble")
    pdb_id = str(candidate["pdb_id"])
    samples = list(classification["samples"])
    labels = list(classification["labels"])
    medoids = dict(classification["medoids"])
    choices: list[dict[str, Any]] = []
    for index, sample in enumerate(samples):
        score = scored_models[sample]
        choices.append(
            {
                "af3_sample": sample,
                "pose_file": f"data/{pdb_id}/pose-{sample}.pdb",
                "afprotein_file": f"data/{pdb_id}/afprotein-{sample}.pdb",
                "afpocket_file": f"data/{pdb_id}/afpocket-{sample}.pdb",
                "rmsd": round(float(score["rmsd"]), 2),
                "correct": bool(float(score["rmsd"]) < CLUSTER_RMSD_ANGSTROM),
                "plddt": round(float(score.get("ligand_plddt") or 0.0), 2),
                "cluster": int(labels[index]),
                "is_rep": bool(medoids[labels[index]] == index),
            }
        )
    reference_sample = min(samples)
    plddt_pick = max(choices, key=lambda choice: choice["plddt"])["af3_sample"]
    bucket = str(classification["bucket"])
    source = "cameo-af3" if bucket == "game-able" else "cameo"
    return {
        "id": pdb_id,
        "ligand": candidate["component_id"],
        "week": str(candidate["week"]).replace("-", "."),
        "protein_file": f"data/{pdb_id}/protein.pdb",
        "pocket_file": f"data/{pdb_id}/pocket.pdb",
        "xtal_lig_file": f"data/{pdb_id}/xtal_lig.pdb",
        "afprotein_ref": f"data/{pdb_id}/afprotein-{reference_sample}.pdb",
        "afpocket_union": f"data/{pdb_id}/afpocket-union.pdb",
        "choices": choices,
        "n_clusters": int(classification["n_clusters"]),
        "af3_top_sample": 1,
        "plddt_pick_sample": plddt_pick,
        "n_correct": sum(choice["correct"] for choice in choices),
        "source": source,
        "single_pocket": True,
        "bucket": bucket,
        "has_correct": any(choice["correct"] for choice in choices),
        "novel": None,
        "n_heavy": int(candidate["heavy_atoms"]),
        "provenance": {
            "provider": "CAMEO",
            "provider_target_id": candidate["target_id"],
            "provider_server_id": "993",
            "license": candidate["coordinate_manifest"]["license"],
            "selection_policy_version": ARCHIVE_POLICY_VERSION,
            "evaluator_version": evaluator_version,
        },
    }


def parse_official_ligand_score(
    payload: Mapping[str, Any], component_id: str
) -> dict[str, Any] | None:
    """Extract one model's official per-component CAMEO BiSyRMSD record."""

    try:
        ligands = payload["results"]["details"]["ligand_pose"]["ligands"]
    except (KeyError, TypeError):
        return None
    if not isinstance(ligands, Mapping):
        return None
    component = component_id.upper()
    rows = [
        (str(key), value)
        for key, value in ligands.items()
        if isinstance(value, Mapping) and str(key).split(".")[-1].upper() == component
    ]
    scored = [
        (key, value)
        for key, value in rows
        if isinstance(value.get("rmsd"), (int, float))
        and not isinstance(value.get("rmsd"), bool)
        and math.isfinite(float(value["rmsd"]))
    ]
    mapped = [
        (key, value)
        for key, value in scored
        if isinstance(value.get("model_ligand_rmsd"), str)
        and isinstance(value.get("transform"), str)
    ]
    if not scored or not mapped:
        return None
    reference_key, selected = mapped[0]
    model_ligand = selected["model_ligand_rmsd"]
    if "." not in model_ligand:
        return None
    predicted_chain, predicted_residue_id = model_ligand.split(".", 1)
    predicted_residue = re.sub(r"\d+$", "", predicted_residue_id)
    transform_values = [
        float(value)
        for value in re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", selected["transform"])
    ]
    if len(transform_values) != 16:
        return None
    mapping = selected.get("chain_mapping_rmsd")
    if not isinstance(mapping, Mapping):
        mapping = {}
    atom_counts = [
        int(value["atom_count"])
        for _, value in rows
        if isinstance(value.get("atom_count"), int) and not isinstance(value.get("atom_count"), bool)
    ]
    target_biounit = str(selected.get("trg_bu", "0"))
    filename_match = re.fullmatch(r"bu_target(?:_hetero)?_(\d+)\.cif(?:\.gz)?", target_biounit)
    if filename_match:
        assembly_id = int(filename_match.group(1))
    elif re.fullmatch(r"\d+", target_biounit):
        assembly_id = int(target_biounit)
    else:
        return None
    return {
        "evaluator_version": OFFICIAL_CAMEO_EVALUATOR_VERSION,
        "rmsd": float(median(float(value["rmsd"]) for _, value in scored)),
        "copy_rmsds": [float(value["rmsd"]) for _, value in scored],
        "component_id": component,
        "atom_count": max(set(atom_counts), key=atom_counts.count) if atom_counts else None,
        "reference_ligand_chain": ".".join(reference_key.split(".")[:-1]),
        "reference_ligand_residue": component,
        "predicted_ligand_chain": predicted_chain,
        "predicted_ligand_residue": predicted_residue,
        "chain_mapping": {str(key): str(value) for key, value in mapping.items()},
        "assembly_id": assembly_id,
        "transform": {
            "rotation": [
                transform_values[0:3],
                transform_values[4:7],
                transform_values[8:11],
            ],
            "translation": [
                transform_values[3],
                transform_values[7],
                transform_values[11],
            ],
        },
    }


def _dependencies():
    try:
        import gemmi
        import numpy
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError("static asset export requires Gemmi and NumPy") from exc
    return gemmi, numpy


def _position(atom: Any, transform: Any | None, gemmi: Any, numpy: Any) -> Any:
    if transform is None:
        return numpy.array([atom.pos.x, atom.pos.y, atom.pos.z], dtype=float)
    value = transform.apply(gemmi.Position(atom.pos.x, atom.pos.y, atom.pos.z))
    return numpy.array([value.x, value.y, value.z], dtype=float)


def _transform_from_score(score: Mapping[str, Any], gemmi: Any) -> Any:
    payload = score.get("transform")
    if not isinstance(payload, Mapping):
        raise ArchiveError("evaluator result is missing its receptor transform")
    transform = gemmi.Transform()
    transform.mat.fromlist(payload["rotation"])
    transform.vec.fromlist(payload["translation"])
    return transform


def _heavy_atoms(residue: Any) -> list[Any]:
    return [atom for atom in residue if atom.element.name != "H"]


def _nearest_polymer_chain(model: Any, ligand: Any, allowed: set[str], numpy: Any) -> str:
    ligand_coordinates = numpy.array(
        [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in _heavy_atoms(ligand)]
    )
    best: tuple[float, str] | None = None
    for chain in model:
        if allowed and chain.name not in allowed:
            continue
        polymer = chain.get_polymer()
        coordinates = numpy.array(
            [
                [atom.pos.x, atom.pos.y, atom.pos.z]
                for residue in polymer
                for atom in residue
                if atom.element.name != "H"
            ]
        )
        if not len(polymer) or not len(coordinates):
            continue
        distance = float(
            numpy.min(numpy.linalg.norm(coordinates[:, None] - ligand_coordinates[None], axis=2))
        )
        if best is None or distance < best[0]:
            best = distance, chain.name
    if best is None:
        raise ArchiveError("no mapped receptor polymer is available for the scored ligand")
    return best[1]


def build_official_pose_result(
    official_score: Mapping[str, Any],
    reference_path: str | Path,
    prediction_path: str | Path,
) -> dict[str, Any]:
    """Materialize CAMEO's scored ligand mapping/transform for quiz asset export."""

    gemmi, numpy = _dependencies()
    reference = gemmi.read_structure(str(reference_path))
    prediction = gemmi.read_structure(str(prediction_path))
    reference.setup_entities()
    prediction.setup_entities()
    reference_model = reference[0]
    prediction_model = prediction[0]
    heavy_atoms = official_score.get("atom_count")
    if not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise ArchiveError("official ligand score is missing atom_count")
    predicted_ligand_chain = _chain(
        prediction_model, str(official_score["predicted_ligand_chain"])
    )
    predicted_ligand = _residue(
        predicted_ligand_chain,
        str(official_score["predicted_ligand_residue"]),
        heavy_atoms,
    )
    reference_ligand_chain = _chain(
        reference_model, str(official_score["reference_ligand_chain"])
    )
    reference_ligand = _residue(
        reference_ligand_chain,
        str(official_score["reference_ligand_residue"]),
        heavy_atoms,
    )
    chain_mapping = dict(official_score.get("chain_mapping") or {})
    predicted_receptor_chain = _nearest_polymer_chain(
        prediction_model, predicted_ligand, set(chain_mapping.values()), numpy
    )
    reverse_mapping = {predicted: reference for reference, predicted in chain_mapping.items()}
    reference_receptor_chain = reverse_mapping.get(predicted_receptor_chain)
    if reference_receptor_chain is None:
        reference_receptor_chain = _nearest_polymer_chain(
            reference_model, reference_ligand, set(chain_mapping), numpy
        )
    transform = _transform_from_score(official_score, gemmi)
    predicted_coordinates = numpy.array(
        [
            _position(atom, transform, gemmi, numpy)
            for atom in _heavy_atoms(predicted_ligand)
        ]
    )
    reference_coordinates = numpy.array(
        [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in _heavy_atoms(reference_ligand)]
    )
    plddt_values = [float(atom.b_iso) for atom in _heavy_atoms(predicted_ligand)]
    return {
        **dict(official_score),
        "reference_receptor_chain": reference_receptor_chain,
        "predicted_receptor_chain": predicted_receptor_chain,
        "predicted_ligand_coordinates": predicted_coordinates.tolist(),
        # AF3 writes a stable ligand atom order across its five ranked models;
        # this matches the historical quiz's clustering input.
        "predicted_ligand_coordinates_reference_order": predicted_coordinates.tolist(),
        "reference_ligand_coordinates": reference_coordinates.tolist(),
        "ligand_plddt": sum(plddt_values) / len(plddt_values) if plddt_values else None,
    }


def _chain(model: Any, chain_name: str) -> Any:
    for chain in model:
        if chain.name == chain_name:
            return chain
    raise ArchiveError(f"coordinate model is missing chain {chain_name}")


def _residue(chain: Any, residue_name: str, heavy_atoms: int) -> Any:
    matches = [
        residue
        for residue in chain
        if residue.name == residue_name
        and sum(atom.element.name != "H" for atom in residue) == heavy_atoms
    ]
    if not matches:
        raise ArchiveError(f"coordinate model is missing ligand {residue_name}")
    return matches[0]


def _write_ligand(path: Path, atoms: Iterable[tuple[str, str, Sequence[float]]]) -> None:
    lines = []
    for index, (element, name, xyz) in enumerate(atoms, start=1):
        lines.append(
            f"HETATM{index:5d} {name[:4]:<4s} LIG X   1    "
            f"{float(xyz[0]):8.3f}{float(xyz[1]):8.3f}{float(xyz[2]):8.3f}"
            f"  1.00  0.00          {element:>2s}"
        )
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")


def _write_polymer(
    path: Path,
    polymer: Any,
    *,
    transform: Any | None,
    near: Any | None,
    gemmi: Any,
    numpy: Any,
) -> None:
    lines: list[str] = []
    serial = 0
    for residue in polymer:
        atoms = [
            (atom, _position(atom, transform, gemmi, numpy))
            for atom in residue
            if atom.element.name != "H"
        ]
        if near is not None:
            positions = numpy.array([position for _, position in atoms])
            if not len(positions) or float(
                numpy.min(numpy.linalg.norm(positions[:, None] - near[None], axis=2))
            ) >= POCKET_RADIUS_ANGSTROM:
                continue
        for atom, xyz in atoms:
            serial += 1
            lines.append(
                f"ATOM  {serial:5d} {atom.name[:4]:<4s} {residue.name[:3]:>3s} A"
                f"{residue.seqid.num:4d}    {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}"
                f"  1.00{float(atom.b_iso):6.2f}          {atom.element.name:>2s}"
            )
    path.write_text("\n".join(lines) + "\nEND\n", encoding="utf-8")


def export_static_assets(
    destination: str | Path,
    reference_path: str | Path,
    prediction_paths: Mapping[int, str | Path],
    scored_models: Mapping[int, Mapping[str, Any]],
) -> None:
    """Write crystal-frame PDB assets consumed by the existing quiz viewer."""

    gemmi, numpy = _dependencies()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    reference = gemmi.read_structure(str(reference_path))
    reference.setup_entities()
    reference_model = reference[0]
    first_score = scored_models[min(scored_models)]
    reference_chain = _chain(reference_model, first_score["reference_receptor_chain"])
    reference_ligand_chain = _chain(reference_model, first_score["reference_ligand_chain"])
    reference_ligand = _residue(
        reference_ligand_chain,
        first_score["reference_ligand_residue"],
        len(first_score["reference_ligand_coordinates"]),
    )
    all_pose_coordinates = numpy.vstack(
        [score["predicted_ligand_coordinates"] for score in scored_models.values()]
    )
    _write_polymer(
        destination / "protein.pdb",
        reference_chain.get_polymer(),
        transform=None,
        near=None,
        gemmi=gemmi,
        numpy=numpy,
    )
    _write_polymer(
        destination / "pocket.pdb",
        reference_chain.get_polymer(),
        transform=None,
        near=all_pose_coordinates,
        gemmi=gemmi,
        numpy=numpy,
    )
    _write_ligand(
        destination / "xtal_lig.pdb",
        (
            (atom.element.name, atom.name, [atom.pos.x, atom.pos.y, atom.pos.z])
            for atom in reference_ligand
            if atom.element.name != "H"
        ),
    )

    first_prediction_polymer = None
    first_transform = None
    for sample in sorted(scored_models):
        score = scored_models[sample]
        prediction = gemmi.read_structure(str(prediction_paths[sample]))
        prediction.setup_entities()
        model = prediction[0]
        receptor = _chain(model, score["predicted_receptor_chain"]).get_polymer()
        ligand_chain = _chain(model, score["predicted_ligand_chain"])
        ligand = _residue(
            ligand_chain,
            score["predicted_ligand_residue"],
            len(score["predicted_ligand_coordinates"]),
        )
        transform = _transform_from_score(score, gemmi)
        if first_prediction_polymer is None:
            first_prediction_polymer = receptor
            first_transform = transform
        coordinates = numpy.array(score["predicted_ligand_coordinates"], dtype=float)
        _write_ligand(
            destination / f"pose-{sample}.pdb",
            (
                (atom.element.name, atom.name, coordinates[index])
                for index, atom in enumerate(
                    atom for atom in ligand if atom.element.name != "H"
                )
            ),
        )
        _write_polymer(
            destination / f"afprotein-{sample}.pdb",
            receptor,
            transform=transform,
            near=None,
            gemmi=gemmi,
            numpy=numpy,
        )
        _write_polymer(
            destination / f"afpocket-{sample}.pdb",
            receptor,
            transform=transform,
            near=coordinates,
            gemmi=gemmi,
            numpy=numpy,
        )
    if first_prediction_polymer is None or first_transform is None:
        raise ArchiveError("no scored predictions were available for asset export")
    _write_polymer(
        destination / "afpocket-union.pdb",
        first_prediction_polymer,
        transform=first_transform,
        near=all_pose_coordinates,
        gemmi=gemmi,
        numpy=numpy,
    )


__all__ = [
    "ARCHIVE_POLICY_VERSION",
    "ArchiveError",
    "CLUSTER_RMSD_ANGSTROM",
    "MINIMUM_SCORED_MODELS",
    "POCKET_RADIUS_ANGSTROM",
    "SINGLE_POCKET_ANGSTROM",
    "build_archive_candidate",
    "build_official_pose_result",
    "build_static_quiz_item",
    "classify_pose_ensemble",
    "cluster_poses",
    "export_static_assets",
    "parse_official_ligand_score",
]
