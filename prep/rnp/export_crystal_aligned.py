#!/usr/bin/env python3
"""Export Runs & Poses predictions in a canonical crystal coordinate frame.

The Runs & Poses prediction archive stores every method/sample in its own
Cartesian frame.  The scored RMSDs are authoritative, but the archive does not
contain a directly overlayable ensemble.  This exporter resolves the selected
Foldarium poses back to their raw archive members, aligns each full prediction
to the model-qualified ground-truth receptor, and writes:

  data_rnp_aligned/<system>/
    protein.pdb                 full experimental receptor (all chains)
    pocket.pdb                  experimental residues within 5 A of the ligand
    xtal_lig.pdb                experimental ligand, unchanged
    xtal_lig_equivalents.pdb    same-chemistry crystal copies, when present
    crystal.cif                 original experimental system
    pose-N.pdb                  aligned predicted ligand used by the viewer
    predictions/pose-N-METHOD.cif aligned full model prediction (optional)
    alignment.json              source, chain mapping, transform, validation

No browser-side fitting is required.  A system is published only when every
pose passes explicit sequence, receptor-fit, ligand-centroid, and geometry
checks against the published Runs & Poses RMSD.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import re
import tarfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import gemmi
import numpy as np
from scipy.optimize import linear_sum_assignment


SCHEMA_VERSION = "foldarium-crystal-aligned/v1"
MIN_MATCHED_CA = 20
MIN_MATCHED_POCKET_CA = 6
MIN_SEQUENCE_IDENTITY = 0.5
MAX_RECEPTOR_RMSD_ANGSTROM = 10.0
RMSD_GEOMETRY_TOLERANCE_ANGSTROM = 2.0
POCKET_RADIUS_ANGSTROM = 5.0
ALIGNMENT_POCKET_RADIUS_ANGSTROM = 12.0


def export_readme(include_full_predictions: bool) -> str:
    full_note = (
        "Each `predictions/` directory contains the complete aligned model CIFs."
        if include_full_predictions
        else "Complete aligned model CIFs were omitted from this lightweight browser build."
    )
    return f"""# Runs & Poses crystal-aligned preview

This is a derived, 38-system Foldarium/Portal preview of the Runs & Poses
prediction archive. Source predictions use independent model coordinate frames;
these files make the selected systems directly overlayable with experiment.

The experimental system is fixed. Every complete prediction is moved with one
receptor-derived rigid transform; ligand coordinates are never fitted or moved
independently. In each `alignment.json`, the convention is:

`aligned_xyz = rotation @ raw_xyz + translation`

The manifest also records the exact raw archive member and SHA-256, receptor
chain mapping, sequence/fit diagnostics, published scorer RMSD, and validation
result. Chemically identical ligand copies are scorer-equivalent and retain both
their indexed and resolved chain identities. {full_note}

`collection.json` indexes all systems. A build is published only when every
selected pose validates; this export contains 38 systems and 452 poses.
"""


@dataclass(frozen=True)
class Selection:
    system_id: str
    target: str
    ligand_instance: str
    quiz_sample: int
    method: str
    published_rmsd: float
    ranking: float | None
    confidence: float | None
    predicted_ligand_chain: str
    archive_member: str


@dataclass
class ChainRecord:
    name: str
    residues: list[tuple[str, np.ndarray]]
    heavy_atoms: np.ndarray

    @property
    def sequence(self) -> str:
        return "".join(code for code, _ in self.residues)


@dataclass
class AlignmentCandidate:
    predicted_ligand_chain: str
    crystal_ligand_chain: str
    predicted_chain: str
    crystal_chain: str
    alignment_scope: str
    rotation: np.ndarray
    translation: np.ndarray
    matched_ca: int
    sequence_identity: float
    receptor_rmsd: float
    predicted_chain_ligand_distance: float
    crystal_chain_ligand_distance: float
    ligand_centroid_distance: float
    ligand_assignment_rmsd: float | None
    geometry_excess: float

    @property
    def valid(self) -> bool:
        minimum_ca = (
            MIN_MATCHED_POCKET_CA
            if self.alignment_scope == "crystal_pocket"
            else MIN_MATCHED_CA
        )
        return (
            self.matched_ca >= minimum_ca
            and self.sequence_identity >= MIN_SEQUENCE_IDENTITY
            and self.receptor_rmsd <= MAX_RECEPTOR_RMSD_ANGSTROM
            and self.geometry_excess <= RMSD_GEOMETRY_TOLERANCE_ANGSTROM
        )


THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C",
    "GLN": "Q", "GLU": "E", "GLY": "G", "HIS": "H", "ILE": "I",
    "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F", "PRO": "P",
    "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M", "SEC": "U", "PYL": "O",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_name(member: str) -> str:
    return hashlib.sha256(member.encode()).hexdigest()[:24] + ".cif"


def strip_protenix_suffix(chain: str) -> str:
    match = re.match(r"^([A-Za-z]+)\d*$", chain)
    return match.group(1) if match else chain


def system_id(target: str, ligand_instance: str) -> str:
    return f"{target}__{ligand_instance}".replace(".", "_")


def optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def resolve_selections(
    plan_path: Path,
    quiz_items_path: Path,
    index_path: Path,
    wanted_system_ids: set[str],
) -> list[Selection]:
    plan = pickle.loads(plan_path.read_bytes())
    plan_by_id = {
        system_id(item["target"], item["instchain"]): item for item in plan
    }
    quiz_by_id = {
        item["id"]: item for item in json.loads(quiz_items_path.read_text())
    }
    index = json.loads(index_path.read_text())
    entries_by_key: dict[tuple[str, str, str], list[tuple[str, dict[str, Any]]]] = (
        defaultdict(list)
    )
    for member, entries in index.items():
        for entry in entries:
            entries_by_key[
                (entry["target"], entry["instchain"], entry["method"])
            ].append((member, entry))

    selections: list[Selection] = []
    for item_id in sorted(wanted_system_ids):
        if item_id not in plan_by_id or item_id not in quiz_by_id:
            raise ValueError(f"missing plan or quiz record for {item_id}")
        plan_item = plan_by_id[item_id]
        quiz_item = quiz_by_id[item_id]
        for choice in quiz_item["choices"]:
            quiz_sample = int(choice["af3_sample"])
            plan_choice = plan_item["choices"][quiz_sample]
            ranking = optional_float(plan_choice.get("ranking"))
            candidates = []
            for member, entry in entries_by_key[
                (
                    plan_item["target"],
                    plan_item["instchain"],
                    plan_choice["_method"],
                )
            ]:
                if abs(float(entry["rmsd"]) - float(plan_choice["rmsd"])) > 0.0006:
                    continue
                entry_ranking = optional_float(entry.get("ranking_score"))
                if ranking is None or entry_ranking is None:
                    if ranking != entry_ranking:
                        continue
                elif abs(entry_ranking - ranking) > 1e-7:
                    continue
                candidates.append((member, entry))
            if len(candidates) != 1:
                raise ValueError(
                    f"expected one raw prediction for {item_id} pose {quiz_sample}; "
                    f"found {len(candidates)}"
                )
            member, entry = candidates[0]
            selections.append(
                Selection(
                    system_id=item_id,
                    target=plan_item["target"],
                    ligand_instance=plan_item["instchain"],
                    quiz_sample=quiz_sample,
                    method=plan_choice["_method"],
                    published_rmsd=float(plan_choice["rmsd"]),
                    ranking=ranking,
                    confidence=optional_float(plan_choice.get("plddt")),
                    predicted_ligand_chain=entry["pred_chain"],
                    archive_member=member,
                )
            )
    return selections


def cache_prediction_members(
    archive_path: Path, selections: list[Selection], raw_cache_dir: Path
) -> dict[str, Path]:
    raw_cache_dir.mkdir(parents=True, exist_ok=True)
    wanted = {selection.archive_member for selection in selections}
    cached = {
        member: raw_cache_dir / cache_name(member)
        for member in wanted
        if (raw_cache_dir / cache_name(member)).exists()
    }
    missing = wanted - cached.keys()
    if missing:
        started = time.time()
        scanned = 0
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                scanned += 1
                if scanned % 50_000 == 0:
                    print(
                        f"prediction archive: scanned {scanned:,}; "
                        f"cached {len(cached):,}/{len(wanted):,} "
                        f"({time.time() - started:.0f}s)",
                        flush=True,
                    )
                if member.name not in missing:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                destination = raw_cache_dir / cache_name(member.name)
                destination.write_bytes(source.read())
                cached[member.name] = destination
                missing.remove(member.name)
                if not missing:
                    break
    if missing:
        preview = ", ".join(sorted(missing)[:3])
        raise ValueError(f"prediction archive is missing {len(missing)} members: {preview}")
    (raw_cache_dir / "members.json").write_text(
        json.dumps(
            {member: cached[member].name for member in sorted(cached)}, indent=2
        )
        + "\n"
    )
    return cached


def cache_ground_truth(
    archive_path: Path, selections: list[Selection], ground_cache_dir: Path
) -> dict[str, dict[str, Path]]:
    ground_cache_dir.mkdir(parents=True, exist_ok=True)
    target_instances = {(item.target, item.ligand_instance) for item in selections}
    wanted: dict[str, tuple[str, str]] = {}
    for target, ligand_instance in target_instances:
        wanted[f"ground_truth/{target}/receptor.cif"] = (target, "receptor")
        wanted[f"ground_truth/{target}/system.cif"] = (target, "system")
        wanted[f"ground_truth/{target}/ligand_files/{ligand_instance}.sdf"] = (
            target,
            "ligand",
        )
    result: dict[str, dict[str, Path]] = defaultdict(dict)
    missing: set[str] = set()
    for member, (target, kind) in wanted.items():
        path = ground_cache_dir / target / Path(member).name
        if path.exists():
            result[target][kind] = path
        else:
            missing.add(member)
    if missing:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                if member.name not in missing:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target, kind = wanted[member.name]
                destination = ground_cache_dir / target / Path(member.name).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read())
                result[target][kind] = destination
                missing.remove(member.name)
                if not missing:
                    break
    if missing:
        preview = ", ".join(sorted(missing)[:3])
        raise ValueError(f"ground-truth archive is missing {len(missing)} files: {preview}")
    return result


def read_structure(path: Path) -> gemmi.Structure:
    structure = gemmi.read_structure(str(path))
    structure.setup_entities()
    if not structure:
        raise ValueError(f"no model in {path}")
    return structure


def heavy_atom_coordinates(atoms: Iterable[gemmi.Atom]) -> np.ndarray:
    return np.array(
        [
            [atom.pos.x, atom.pos.y, atom.pos.z]
            for atom in atoms
            if atom.element.name not in ("H", "D")
        ],
        dtype=float,
    )


def structure_chains(structure: gemmi.Structure) -> list[ChainRecord]:
    records: list[ChainRecord] = []
    for chain in structure[0]:
        residues: list[tuple[str, np.ndarray]] = []
        atoms: list[gemmi.Atom] = []
        for residue in chain:
            ca = residue.find_atom("CA", "*")
            if ca:
                residues.append(
                    (
                        THREE_TO_ONE.get(residue.name.upper(), "X"),
                        np.array([ca.pos.x, ca.pos.y, ca.pos.z], dtype=float),
                    )
                )
            atoms.extend(residue)
        if len(residues) >= 4:
            records.append(
                ChainRecord(
                    name=chain.name,
                    residues=residues,
                    heavy_atoms=heavy_atom_coordinates(atoms),
                )
            )
    return records


def find_ligand_candidates(
    structure: gemmi.Structure,
    chain_name: str,
    method: str,
    protein_chain_names: set[str],
) -> dict[str, list[gemmi.Atom]]:
    """Return the indexed ligand and any chemically identical chain copies.

    The Runs & Poses evaluator treats identical ligand copies as symmetry
    equivalents, but its index records only one predicted chain.  Retaining all
    same-composition non-polymer chains lets the alignment reproduce that
    scoring choice without confusing an ion or a protein chain for the ligand.
    """
    indexed_matches = []
    for chain in structure[0]:
        normalized = (
            strip_protenix_suffix(chain.name) if method == "protenix" else chain.name
        )
        if normalized == chain_name:
            indexed_matches.append(chain)
    if not indexed_matches:
        raise ValueError(
            f"predicted ligand chain {chain_name} was not found; chains are "
            f"{[chain.name for chain in structure[0]]}"
        )

    def chain_heavy_atoms(chain: gemmi.Chain) -> list[gemmi.Atom]:
        return [
            atom
            for residue in chain
            for atom in residue
            if atom.element.name not in ("H", "D")
        ]

    indexed_compositions = {
        tuple(sorted(Counter(atom.element.name.upper() for atom in atoms).items()))
        for chain in indexed_matches
        if (atoms := chain_heavy_atoms(chain))
    }
    if not indexed_compositions:
        raise ValueError(f"predicted ligand chain {chain_name} has no heavy atoms")

    candidates: dict[str, list[gemmi.Atom]] = {}
    indexed_names = {chain.name for chain in indexed_matches}
    for chain in structure[0]:
        if chain.name in protein_chain_names and chain.name not in indexed_names:
            continue
        atoms = chain_heavy_atoms(chain)
        composition = tuple(
            sorted(Counter(atom.element.name.upper() for atom in atoms).items())
        )
        if atoms and composition in indexed_compositions:
            candidates[chain.name] = atoms
    if not candidates:
        raise ValueError(f"predicted ligand chain {chain_name} has no usable copy")
    return candidates


def find_crystal_ligand_candidates(
    system: gemmi.Structure,
    ligand_instance: str,
    selected_elements: list[str],
    selected_coordinates: np.ndarray,
) -> dict[str, tuple[list[str], np.ndarray]]:
    """Return the selected crystal ligand and chemically identical copies."""
    selected_composition = Counter(selected_elements)
    protein_chain_names = {chain.name for chain in structure_chains(system)}
    candidates = {
        ligand_instance: (selected_elements, selected_coordinates),
    }
    for chain in system[0]:
        if chain.name in protein_chain_names or chain.name == ligand_instance:
            continue
        atoms = [
            atom
            for residue in chain
            for atom in residue
            if atom.element.name not in ("H", "D")
        ]
        elements = [atom.element.name.upper() for atom in atoms]
        if atoms and Counter(elements) == selected_composition:
            candidates[chain.name] = (elements, heavy_atom_coordinates(atoms))
    return candidates


def parse_sdf(path: Path) -> tuple[list[str], list[str], np.ndarray]:
    lines = path.read_text(errors="replace").splitlines()
    if len(lines) < 4:
        raise ValueError(f"invalid SDF {path}")
    atom_count = int(lines[3][0:3])
    elements: list[str] = []
    names: list[str] = []
    coordinates = []
    counts: Counter[str] = Counter()
    for line in lines[4 : 4 + atom_count]:
        element = line[31:34].strip().upper()
        if element in ("H", "D"):
            continue
        counts[element] += 1
        elements.append(element)
        names.append(f"{element}{counts[element]}")
        coordinates.append(
            [float(line[0:10]), float(line[10:20]), float(line[20:30])]
        )
    if not coordinates:
        raise ValueError(f"no heavy ligand atoms in {path}")
    return elements, names, np.array(coordinates, dtype=float)


def sequence_pairs(
    predicted: ChainRecord, crystal: ChainRecord
) -> tuple[list[tuple[int, int]], float]:
    matcher = SequenceMatcher(
        None, predicted.sequence, crystal.sequence, autojunk=False
    )
    pairs = [
        (block.a + offset, block.b + offset)
        for block in matcher.get_matching_blocks()
        for offset in range(block.size)
    ]
    identity = len(pairs) / max(len(predicted.sequence), len(crystal.sequence), 1)
    return pairs, identity


def kabsch(predicted: np.ndarray, crystal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    predicted_center = predicted.mean(axis=0)
    crystal_center = crystal.mean(axis=0)
    covariance = (predicted - predicted_center).T @ (crystal - crystal_center)
    left, _, right_t = np.linalg.svd(covariance)
    determinant = np.sign(np.linalg.det(right_t.T @ left.T))
    rotation = right_t.T @ np.diag([1.0, 1.0, determinant]) @ left.T
    translation = crystal_center - rotation @ predicted_center
    return rotation, translation


def transform_coordinates(
    coordinates: np.ndarray, rotation: np.ndarray, translation: np.ndarray
) -> np.ndarray:
    return (rotation @ coordinates.T).T + translation


def minimum_distance(left: np.ndarray, right: np.ndarray) -> float:
    if not len(left) or not len(right):
        return math.inf
    return float(np.linalg.norm(left[:, None, :] - right[None, :, :], axis=2).min())


def assignment_rmsd(
    predicted_elements: list[str],
    predicted_coordinates: np.ndarray,
    crystal_elements: list[str],
    crystal_coordinates: np.ndarray,
) -> float | None:
    if Counter(predicted_elements) != Counter(crystal_elements):
        return None
    squared_distances: list[float] = []
    for element in sorted(set(predicted_elements)):
        predicted_indices = [
            index for index, value in enumerate(predicted_elements) if value == element
        ]
        crystal_indices = [
            index for index, value in enumerate(crystal_elements) if value == element
        ]
        cost = np.sum(
            (
                predicted_coordinates[predicted_indices, None, :]
                - crystal_coordinates[None, crystal_indices, :]
            )
            ** 2,
            axis=2,
        )
        rows, columns = linear_sum_assignment(cost)
        squared_distances.extend(cost[rows, columns].tolist())
    return float(math.sqrt(sum(squared_distances) / len(squared_distances)))


def model_prefix(instance: str) -> str:
    return instance.split(".", 1)[0] + "."


def alignment_candidates(
    selection: Selection,
    prediction: gemmi.Structure,
    crystal_receptor: gemmi.Structure,
    crystal_ligands: dict[str, tuple[list[str], np.ndarray]],
) -> tuple[dict[str, list[gemmi.Atom]], list[AlignmentCandidate]]:
    predicted_chains = structure_chains(prediction)
    predicted_ligands = find_ligand_candidates(
        prediction,
        selection.predicted_ligand_chain,
        selection.method,
        {chain.name for chain in predicted_chains},
    )
    all_crystal_chains = structure_chains(crystal_receptor)
    qualified = [
        chain
        for chain in all_crystal_chains
        if chain.name.startswith(model_prefix(selection.ligand_instance))
    ]
    crystal_chains = qualified or all_crystal_chains
    candidates: list[AlignmentCandidate] = []
    for crystal_ligand_chain, crystal_ligand in crystal_ligands.items():
        crystal_elements, crystal_coordinates = crystal_ligand
        for predicted_ligand_chain, predicted_ligand_atoms in predicted_ligands.items():
            predicted_ligand_coordinates = heavy_atom_coordinates(
                predicted_ligand_atoms
            )
            predicted_elements = [
                atom.element.name.upper() for atom in predicted_ligand_atoms
            ]
            for predicted_chain in predicted_chains:
                predicted_contact = minimum_distance(
                    predicted_chain.heavy_atoms, predicted_ligand_coordinates
                )
                for crystal_chain in crystal_chains:
                    pairs, identity = sequence_pairs(predicted_chain, crystal_chain)
                    if len(pairs) < MIN_MATCHED_CA or identity < MIN_SEQUENCE_IDENTITY:
                        continue
                    fit_groups = [("full_chain", pairs)]
                    pocket_pairs = [
                        (left, right)
                        for left, right in pairs
                        if minimum_distance(
                            crystal_chain.residues[right][1][None, :],
                            crystal_coordinates,
                        )
                        < ALIGNMENT_POCKET_RADIUS_ANGSTROM
                    ]
                    if len(pocket_pairs) >= MIN_MATCHED_POCKET_CA:
                        fit_groups.insert(0, ("crystal_pocket", pocket_pairs))

                    for alignment_scope, fit_pairs in fit_groups:
                        predicted_ca = np.array(
                            [
                                predicted_chain.residues[left][1]
                                for left, _ in fit_pairs
                            ]
                        )
                        crystal_ca = np.array(
                            [
                                crystal_chain.residues[right][1]
                                for _, right in fit_pairs
                            ]
                        )
                        rotation, translation = kabsch(predicted_ca, crystal_ca)
                        aligned_ca = transform_coordinates(
                            predicted_ca, rotation, translation
                        )
                        receptor_rmsd = float(
                            np.sqrt(
                                np.mean(
                                    np.sum((aligned_ca - crystal_ca) ** 2, axis=1)
                                )
                            )
                        )
                        aligned_ligand = transform_coordinates(
                            predicted_ligand_coordinates, rotation, translation
                        )
                        centroid_distance = float(
                            np.linalg.norm(
                                aligned_ligand.mean(0)
                                - crystal_coordinates.mean(0)
                            )
                        )
                        assigned_rmsd = assignment_rmsd(
                            predicted_elements,
                            aligned_ligand,
                            crystal_elements,
                            crystal_coordinates,
                        )
                        geometry_measure = (
                            assigned_rmsd
                            if assigned_rmsd is not None
                            else centroid_distance
                        )
                        candidates.append(
                            AlignmentCandidate(
                                predicted_ligand_chain=predicted_ligand_chain,
                                crystal_ligand_chain=crystal_ligand_chain,
                                predicted_chain=predicted_chain.name,
                                crystal_chain=crystal_chain.name,
                                alignment_scope=alignment_scope,
                                rotation=rotation,
                                translation=translation,
                                matched_ca=len(fit_pairs),
                                sequence_identity=identity,
                                receptor_rmsd=receptor_rmsd,
                                predicted_chain_ligand_distance=predicted_contact,
                                crystal_chain_ligand_distance=minimum_distance(
                                    crystal_chain.heavy_atoms,
                                    crystal_coordinates,
                                ),
                                ligand_centroid_distance=centroid_distance,
                                ligand_assignment_rmsd=assigned_rmsd,
                                geometry_excess=(
                                    geometry_measure - selection.published_rmsd
                                ),
                            ),
                        )
    return predicted_ligands, candidates


def choose_candidate(candidates: list[AlignmentCandidate]) -> AlignmentCandidate:
    if not candidates:
        raise ValueError("no sequence-compatible receptor-chain alignment")
    return min(
        candidates,
        key=lambda candidate: (
            not candidate.valid,
            max(0.0, candidate.geometry_excess),
            abs(candidate.geometry_excess),
            candidate.predicted_chain_ligand_distance
            + candidate.crystal_chain_ligand_distance,
            candidate.receptor_rmsd,
            -candidate.sequence_identity,
        ),
    )


def transform_structure(
    structure: gemmi.Structure, rotation: np.ndarray, translation: np.ndarray
) -> None:
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    transformed = rotation @ np.array(
                        [atom.pos.x, atom.pos.y, atom.pos.z]
                    ) + translation
                    atom.pos = gemmi.Position(*transformed)


def pdb_atom_line(
    record: str,
    serial: int,
    atom_name: str,
    residue_name: str,
    chain: str,
    sequence: int,
    coordinates: np.ndarray,
    element: str,
) -> str:
    return (
        f"{record:<6}{serial:>5d} {atom_name[:4]:<4s} {residue_name[:3]:>3s} "
        f"{chain[:1]}{sequence:>4d}    "
        f"{coordinates[0]:8.3f}{coordinates[1]:8.3f}{coordinates[2]:8.3f}"
        f"  1.00  0.00          {element[:2]:>2s}"
    )


def write_ligand_pdb(
    path: Path, elements: list[str], names: list[str], coordinates: np.ndarray
) -> None:
    lines = [
        pdb_atom_line("HETATM", index, name, "LIG", "X", 1, xyz, element)
        for index, (element, name, xyz) in enumerate(
            zip(elements, names, coordinates), 1
        )
    ]
    path.write_text("\n".join([*lines, "END", ""]))


def write_ligand_copies_pdb(
    path: Path, copies: dict[str, tuple[list[str], np.ndarray]]
) -> dict[str, str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    if len(copies) > len(alphabet):
        raise ValueError("too many equivalent ligand copies for PDB export")
    lines: list[str] = []
    serial = 0
    chain_map: dict[str, str] = {}
    for chain_index, (instance, (elements, coordinates)) in enumerate(
        sorted(copies.items())
    ):
        viewer_chain = alphabet[chain_index]
        chain_map[instance] = viewer_chain
        counts: Counter[str] = Counter()
        for element, xyz in zip(elements, coordinates):
            serial += 1
            counts[element] += 1
            lines.append(
                pdb_atom_line(
                    "HETATM",
                    serial,
                    f"{element}{counts[element]}",
                    "LIG",
                    viewer_chain,
                    1,
                    xyz,
                    element,
                )
            )
        lines.append("TER")
    path.write_text("\n".join([*lines, "END", ""]))
    return chain_map


def write_receptor_pdb(
    path: Path,
    structure: gemmi.Structure,
    crystal_coordinates: np.ndarray | None = None,
    pocket_only: bool = False,
) -> dict[str, str]:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    chains = structure_chains(structure)
    if len(chains) > len(alphabet):
        raise ValueError("too many receptor chains for PDB viewer export")
    chain_map = {record.name: alphabet[index] for index, record in enumerate(chains)}
    lines: list[str] = []
    serial = 0
    for chain in structure[0]:
        if chain.name not in chain_map:
            continue
        for residue in chain:
            residue_atoms = [
                atom for atom in residue if atom.element.name not in ("H", "D")
            ]
            if not residue_atoms:
                continue
            if pocket_only:
                if crystal_coordinates is None:
                    raise ValueError("pocket export requires crystal coordinates")
                residue_coordinates = heavy_atom_coordinates(residue_atoms)
                if (
                    minimum_distance(residue_coordinates, crystal_coordinates)
                    > POCKET_RADIUS_ANGSTROM
                ):
                    continue
            for atom in residue_atoms:
                serial += 1
                lines.append(
                    pdb_atom_line(
                        "ATOM",
                        serial,
                        atom.name,
                        residue.name,
                        chain_map[chain.name],
                        residue.seqid.num,
                        np.array([atom.pos.x, atom.pos.y, atom.pos.z]),
                        atom.element.name,
                    )
                )
    path.write_text("\n".join([*lines, "END", ""]))
    return chain_map


def export_system(
    system_selections: list[Selection],
    cached_predictions: dict[str, Path],
    ground_truth: dict[str, Path],
    output_dir: Path,
    include_full_predictions: bool,
) -> dict[str, Any]:
    first = system_selections[0]
    receptor = read_structure(ground_truth["receptor"])
    system = read_structure(ground_truth["system"])
    crystal_elements, crystal_names, crystal_coordinates = parse_sdf(
        ground_truth["ligand"]
    )
    crystal_ligands = find_crystal_ligand_candidates(
        system,
        first.ligand_instance,
        crystal_elements,
        crystal_coordinates,
    )
    system_output = output_dir / first.system_id
    system_output.mkdir(parents=True, exist_ok=False)
    chain_map = write_receptor_pdb(system_output / "protein.pdb", receptor)
    write_receptor_pdb(
        system_output / "pocket.pdb",
        receptor,
        crystal_coordinates=crystal_coordinates,
        pocket_only=True,
    )
    write_ligand_pdb(
        system_output / "xtal_lig.pdb",
        crystal_elements,
        crystal_names,
        crystal_coordinates,
    )
    equivalent_ligand_file = None
    equivalent_ligand_chain_map: dict[str, str] = {}
    if len(crystal_ligands) > 1:
        equivalent_ligand_file = "xtal_lig_equivalents.pdb"
        equivalent_ligand_chain_map = write_ligand_copies_pdb(
            system_output / equivalent_ligand_file,
            crystal_ligands,
        )
    (system_output / "crystal.cif").write_bytes(ground_truth["system"].read_bytes())
    prediction_output = system_output / "predictions"
    if include_full_predictions:
        prediction_output.mkdir()

    records = []
    failures = []
    for selection in sorted(system_selections, key=lambda item: item.quiz_sample):
        raw_path = cached_predictions[selection.archive_member]
        raw_bytes = raw_path.read_bytes()
        prediction = read_structure(raw_path)
        predicted_ligands, candidates = alignment_candidates(
            selection,
            prediction,
            receptor,
            crystal_ligands,
        )
        chosen = choose_candidate(candidates)
        predicted_ligand_atoms = predicted_ligands[chosen.predicted_ligand_chain]
        raw_ligand_coordinates = heavy_atom_coordinates(predicted_ligand_atoms)
        aligned_ligand_coordinates = transform_coordinates(
            raw_ligand_coordinates, chosen.rotation, chosen.translation
        )
        predicted_elements = [
            atom.element.name.upper() for atom in predicted_ligand_atoms
        ]
        predicted_names = [atom.name for atom in predicted_ligand_atoms]
        pose_name = f"pose-{selection.quiz_sample}.pdb"
        write_ligand_pdb(
            system_output / pose_name,
            predicted_elements,
            predicted_names,
            aligned_ligand_coordinates,
        )
        full_prediction_name = None
        if include_full_predictions:
            transform_structure(prediction, chosen.rotation, chosen.translation)
            full_prediction_name = f"pose-{selection.quiz_sample}-{selection.method}.cif"
            prediction.make_mmcif_document().write_file(
                str(prediction_output / full_prediction_name)
            )
        record = {
            "sample": selection.quiz_sample,
            "method": selection.method,
            "publishedRmsdAngstrom": selection.published_rmsd,
            "rankingScore": selection.ranking,
            "confidence": selection.confidence,
            "source": {
                "archiveMember": selection.archive_member,
                "sha256": sha256_bytes(raw_bytes),
                "predictedLigandChain": selection.predicted_ligand_chain,
                "resolvedPredictedLigandChain": chosen.predicted_ligand_chain,
                "symmetryCopyReassigned": (
                    strip_protenix_suffix(chosen.predicted_ligand_chain)
                    if selection.method == "protenix"
                    else chosen.predicted_ligand_chain
                )
                != selection.predicted_ligand_chain,
                "resolvedCrystalLigandChain": chosen.crystal_ligand_chain,
                "equivalentCrystalCopyUsed": (
                    chosen.crystal_ligand_chain != selection.ligand_instance
                ),
            },
            "alignment": {
                "predictedAnchorChain": chosen.predicted_chain,
                "crystalAnchorChain": chosen.crystal_chain,
                "scope": chosen.alignment_scope,
                "matchedCaCount": chosen.matched_ca,
                "sequenceIdentity": round(chosen.sequence_identity, 6),
                "receptorRmsdAngstrom": round(chosen.receptor_rmsd, 4),
                "ligandCentroidDistanceAngstrom": round(
                    chosen.ligand_centroid_distance, 4
                ),
                "ligandAssignmentRmsdAngstrom": (
                    round(chosen.ligand_assignment_rmsd, 4)
                    if chosen.ligand_assignment_rmsd is not None
                    else None
                ),
                "geometryExcessAngstrom": round(chosen.geometry_excess, 4),
                "rotation": chosen.rotation.round(10).tolist(),
                "translation": chosen.translation.round(10).tolist(),
            },
            "validation": {
                "status": "validated" if chosen.valid else "failed",
                "rmsdGeometryToleranceAngstrom": RMSD_GEOMETRY_TOLERANCE_ANGSTROM,
            },
            "poseFile": pose_name,
            "fullPredictionFile": (
                f"predictions/{full_prediction_name}"
                if full_prediction_name
                else None
            ),
        }
        records.append(record)
        if not chosen.valid:
            failures.append(
                f"pose {selection.quiz_sample} {selection.method}: "
                f"fit={chosen.receptor_rmsd:.2f} A, "
                f"geometry excess={chosen.geometry_excess:.2f} A"
            )

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "systemId": first.system_id,
        "target": first.target,
        "ligandInstance": first.ligand_instance,
        "coordinateFrame": "experimental-system",
        "crystal": {
            "systemFile": "crystal.cif",
            "proteinFile": "protein.pdb",
            "pocketFile": "pocket.pdb",
            "ligandFile": "xtal_lig.pdb",
            "equivalentLigandInstances": sorted(crystal_ligands),
            "equivalentLigandFile": equivalent_ligand_file,
            "equivalentLigandViewerChainMap": equivalent_ligand_chain_map,
            "viewerChainMap": chain_map,
            "receptorSourceSha256": sha256_bytes(
                ground_truth["receptor"].read_bytes()
            ),
            "ligandSourceSha256": sha256_bytes(ground_truth["ligand"].read_bytes()),
        },
        "predictions": records,
        "validation": {
            "status": "validated" if not failures else "failed",
            "failures": failures,
        },
    }
    (system_output / "alignment.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    if failures:
        raise ValueError(f"{first.system_id}: " + "; ".join(failures[:3]))
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prediction-archive", required=True, type=Path)
    parser.add_argument("--ground-truth-archive", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--quiz-items", required=True, type=Path)
    parser.add_argument("--system-ids", required=True, type=Path)
    parser.add_argument("--raw-cache-dir", required=True, type=Path)
    parser.add_argument("--ground-cache-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--include-full-predictions",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.output_dir.exists():
        raise ValueError(f"refusing to overwrite existing output {args.output_dir}")
    wanted_system_ids = {
        line.strip()
        for line in args.system_ids.read_text().splitlines()
        if line.strip()
    }
    selections = resolve_selections(
        args.plan, args.quiz_items, args.index, wanted_system_ids
    )
    print(
        f"resolved {len(selections)} selected poses across "
        f"{len(wanted_system_ids)} systems"
    )
    cached_predictions = cache_prediction_members(
        args.prediction_archive, selections, args.raw_cache_dir
    )
    ground_truth_by_target = cache_ground_truth(
        args.ground_truth_archive, selections, args.ground_cache_dir
    )
    args.output_dir.mkdir(parents=True)
    by_system: dict[str, list[Selection]] = defaultdict(list)
    for selection in selections:
        by_system[selection.system_id].append(selection)

    manifests = []
    failures = []
    for index, item_id in enumerate(sorted(by_system), 1):
        system_selections = by_system[item_id]
        target = system_selections[0].target
        try:
            manifests.append(
                export_system(
                    system_selections,
                    cached_predictions,
                    ground_truth_by_target[target],
                    args.output_dir,
                    args.include_full_predictions,
                )
            )
            print(f"[{index}/{len(by_system)}] validated {item_id}")
        except Exception as error:
            failures.append({"systemId": item_id, "error": str(error)})
            print(f"[{index}/{len(by_system)}] FAILED {item_id}: {error}")

    collection = {
        "schemaVersion": SCHEMA_VERSION,
        "coordinateFrame": "experimental-system",
        "systems": [
            {
                "systemId": manifest["systemId"],
                "manifest": f"{manifest['systemId']}/alignment.json",
                "poseCount": len(manifest["predictions"]),
            }
            for manifest in manifests
        ],
        "validation": {
            "status": "validated" if not failures else "failed",
            "systemCount": len(manifests),
            "poseCount": sum(len(manifest["predictions"]) for manifest in manifests),
            "failures": failures,
        },
    }
    (args.output_dir / "collection.json").write_text(
        json.dumps(collection, indent=2) + "\n"
    )
    (args.output_dir / "README.md").write_text(
        export_readme(args.include_full_predictions)
    )
    if failures:
        raise SystemExit(f"alignment failed for {len(failures)} systems")
    print(
        f"wrote {len(manifests)} validated systems / "
        f"{collection['validation']['poseCount']} poses to {args.output_dir}"
    )


if __name__ == "__main__":
    main()
