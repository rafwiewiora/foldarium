#!/usr/bin/env python3
"""Reproduce and validate Runs & Poses BiSyRMSD after crystal alignment.

Runs & Poses used OpenStructure 2.8's 4 A binding-site superposition and
chemical-symmetry-corrected ligand RMSD.  This independent implementation
enumerates the same chain mappings and ligand graph isomorphisms, resolves the
official RMSD model-chain provenance from the released score tables, and then
checks the coordinates actually written to each browser PDB.

Use ``--rewrite`` to replace a legacy heuristic alignment with the reproduced
R&P transform before performing the post-write check.  Rewriting always moves
the complete prediction with one receptor-derived rigid transform; it never
fits a ligand independently.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import gemmi
import networkx as nx
import numpy as np
from Bio import Align
from Bio.Align import substitution_matrices
from rdkit import Chem
from rdkit.Chem import rdDetermineBonds

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_crystal_aligned as export


AA = export.THREE_TO_ONE
STANDARD = set(AA)


@dataclass
class Residue:
    name: str
    code: str
    atoms: dict[str, np.ndarray]
    heavy: np.ndarray


@dataclass
class Chain:
    name: str
    residues: list[Residue]

    @property
    def sequence(self) -> str:
        return "".join(r.code for r in self.residues)

    @property
    def heavy(self) -> np.ndarray:
        return np.concatenate([r.heavy for r in self.residues], axis=0)


def pos(atom: gemmi.Atom) -> np.ndarray:
    return np.array([atom.pos.x, atom.pos.y, atom.pos.z], dtype=float)


def protein_chains(structure: gemmi.Structure) -> dict[str, Chain]:
    result = {}
    for chain in structure[0]:
        residues = []
        for residue in chain:
            name = residue.name.upper()
            if name not in STANDARD:
                continue
            atoms = {}
            for atom in residue:
                if atom.element.name in ("H", "D") or atom.name in atoms:
                    continue
                atoms[atom.name] = pos(atom)
            required = {"N", "CA", "C"} | (set() if name == "GLY" else {"CB"})
            if not required.issubset(atoms):
                continue
            residues.append(
                Residue(
                    name=name,
                    code=AA.get(name, "X"),
                    atoms=atoms,
                    heavy=np.array(list(atoms.values()), dtype=float),
                )
            )
        if len(residues) >= 6:
            result[chain.name] = Chain(chain.name, residues)
    return result


def aligner() -> Align.PairwiseAligner:
    result = Align.PairwiseAligner(mode="global")
    result.substitution_matrix = substitution_matrices.load("BLOSUM62")
    result.open_gap_score = -11
    result.extend_gap_score = -1
    return result


ALIGNER = aligner()


def alignment_pairs(left: str, right: str) -> tuple[list[tuple[int, int]], float, float, float]:
    aln = ALIGNER.align(left, right)[0]
    pairs = []
    for (l0, l1), (r0, r1) in zip(aln.aligned[0], aln.aligned[1]):
        pairs.extend(zip(range(l0, l1), range(r0, r1)))
    matches = sum(left[i] == right[j] for i, j in pairs)
    identity = 100.0 * matches / max(len(pairs), 1)
    coverage = len(pairs) / max(len(left), 1)
    # OST's group gap threshold is 1.0 here, so this is only diagnostic.
    return pairs, identity, coverage, identity * coverage


def target_groups(chains: dict[str, Chain]) -> list[list[str]]:
    groups: list[list[str]] = []
    for name, chain in chains.items():
        found = None
        for group_index, group in enumerate(groups):
            for other_name in group:
                _, identity, _, _ = alignment_pairs(chain.sequence, chains[other_name].sequence)
                if identity >= 95.0:
                    found = group_index
                    break
            if found is not None:
                break
        if found is None:
            groups.append([name])
        else:
            groups[found].append(name)
    return [sorted(group, key=lambda name: len(chains[name].sequence), reverse=True) for group in groups]


def model_group_mapping(
    target: dict[str, Chain], model: dict[str, Chain], groups: list[list[str]]
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    for model_name, model_chain in model.items():
        scores = []
        for group_index, group in enumerate(groups):
            ref = target[group[0]]
            _, _, _, score = alignment_pairs(ref.sequence, model_chain.sequence)
            scores.append((score, group_index))
        if scores:
            result[max(scores)[1]].append(model_name)
    return result


def read_structure(path: Path) -> gemmi.Structure:
    result = gemmi.read_structure(str(path))
    result.setup_entities()
    return result


def chain_heavy_atoms(chain: gemmi.Chain) -> list[gemmi.Atom]:
    return [
        atom
        for residue in chain
        for atom in residue
        if atom.element.name not in ("H", "D")
    ]


def selected_model_ligand(
    raw_path: Path, chain_name: str, method: str
) -> tuple[np.ndarray, Chem.Mol, str]:
    structure = read_structure(raw_path)
    matches = []
    for chain in structure[0]:
        normalized = export.strip_protenix_suffix(chain.name) if method == "protenix" else chain.name
        if normalized == chain_name:
            matches.append(chain)
    if len(matches) != 1:
        raise ValueError(f"expected one model ligand chain {chain_name}; got {[c.name for c in matches]}")
    chain = matches[0]
    residues = list(chain)
    if len(residues) != 1:
        raise ValueError(f"model ligand {chain.name} has {len(residues)} residues")
    residue = residues[0]
    atoms = chain_heavy_atoms(chain)
    coordinates = np.array([pos(atom) for atom in atoms])
    block = gemmi.cif.read_file(str(raw_path)).sole_block()
    smiles_by_comp = dict(zip(block.find_values("_chem_comp.id"), block.find_values("_chem_comp.pdbx_smiles")))
    expected = [atom.element.name.upper() for atom in atoms]
    smiles = smiles_by_comp.get(residue.name)
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None:
        editable = Chem.RWMol()
        for element in expected:
            editable.AddAtom(Chem.Atom(element.title()))
        mol = editable.GetMol()
        conformer = Chem.Conformer(len(atoms))
        for index, xyz in enumerate(coordinates):
            conformer.SetAtomPosition(index, xyz.tolist())
        mol.AddConformer(conformer)
        rdDetermineBonds.DetermineConnectivity(mol)
    observed = [atom.GetSymbol().upper() for atom in mol.GetAtoms()]
    if expected != observed:
        raise ValueError(f"model SMILES atom order differs from coordinates: {expected} != {observed}")
    return coordinates, mol, chain.name


def sdf_mol(path: Path) -> Chem.Mol:
    mol = Chem.MolFromMolFile(str(path), sanitize=False, removeHs=True)
    if mol is None:
        raise ValueError(f"RDKit could not parse {path}")
    Chem.SanitizeMol(mol)
    return mol


def reference_ligand_copies(
    system_path: Path, sdf_path: Path, selected_chain: str
) -> tuple[Chem.Mol, dict[str, np.ndarray]]:
    mol = sdf_mol(sdf_path)
    sdf_coordinates = np.array(mol.GetConformer().GetPositions(), dtype=float)
    sdf_elements = [atom.GetSymbol().upper() for atom in mol.GetAtoms()]
    structure = read_structure(system_path)
    selected = next((chain for chain in structure[0] if chain.name == selected_chain), None)
    if selected is None:
        raise ValueError(f"selected crystal ligand {selected_chain} not in {system_path}")
    selected_atoms = chain_heavy_atoms(selected)
    selected_coordinates = np.array([pos(atom) for atom in selected_atoms])
    selected_elements = [atom.element.name.upper() for atom in selected_atoms]
    ordered_names = []
    available = set(range(len(selected_atoms)))
    for element, coordinate in zip(sdf_elements, sdf_coordinates):
        choices = [i for i in available if selected_elements[i] == element]
        if not choices:
            raise ValueError("could not map SDF atom elements to selected system ligand")
        index = min(choices, key=lambda i: np.linalg.norm(selected_coordinates[i] - coordinate))
        distance = np.linalg.norm(selected_coordinates[index] - coordinate)
        if distance > 0.05:
            raise ValueError(f"SDF/system atom coordinate mismatch: {distance:.3f} A")
        available.remove(index)
        ordered_names.append(selected_atoms[index].name)

    copies = {}
    wanted_names = set(ordered_names)
    for chain in structure[0]:
        atoms = chain_heavy_atoms(chain)
        atoms_by_name = {atom.name: atom for atom in atoms}
        if len(atoms_by_name) != len(atoms) or set(atoms_by_name) != wanted_names:
            continue
        ordered = [atoms_by_name[name] for name in ordered_names]
        if [atom.element.name.upper() for atom in ordered] != sdf_elements:
            continue
        copies[chain.name] = np.array([pos(atom) for atom in ordered])
    if selected_chain not in copies:
        copies[selected_chain] = sdf_coordinates
    return mol, copies


def graph(mol: Chem.Mol) -> nx.Graph:
    result = nx.Graph()
    for atom in mol.GetAtoms():
        result.add_node(atom.GetIdx(), element=atom.GetSymbol().upper())
    for bond in mol.GetBonds():
        result.add_edge(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
    return result


def symmetries(reference: Chem.Mol, model: Chem.Mol) -> list[np.ndarray]:
    matcher = nx.algorithms.isomorphism.GraphMatcher(
        graph(reference), graph(model),
        node_match=lambda left, right: left["element"] == right["element"],
    )
    result = []
    for mapping in matcher.isomorphisms_iter():
        # Mapping keys are reference atom indices, values are model indices.
        result.append(np.array([mapping[i] for i in range(reference.GetNumAtoms())], dtype=int))
        if len(result) > 100000:
            raise ValueError("more than 100000 ligand symmetries")
    if not result:
        raise ValueError("reference and model ligand graphs are not isomorphic")
    return result


def rmsd_for_symmetries(
    model_coordinates: np.ndarray,
    target_coordinates: np.ndarray,
    mappings: list[np.ndarray],
    rotation: np.ndarray,
    translation: np.ndarray,
) -> float:
    aligned = export.transform_coordinates(model_coordinates, rotation, translation)
    return min(
        float(np.sqrt(np.mean(np.sum((aligned[mapping] - target_coordinates) ** 2, axis=1))))
        for mapping in mappings
    )


def binding_site(chains: dict[str, Chain], ligand: np.ndarray) -> dict[str, set[int]]:
    result = {}
    for name, chain in chains.items():
        selected = {
            i for i, residue in enumerate(chain.residues)
            if export.minimum_distance(residue.heavy, ligand) <= 4.0
        }
        if selected:
            result[name] = selected
    return result


def mapping_options(
    target: dict[str, Chain],
    model: dict[str, Chain],
    groups: list[list[str]],
    mapped_model_groups: dict[int, list[str]],
    site: dict[str, set[int]],
    model_ligand: np.ndarray,
) -> list[dict[str, str]]:
    close_model = {
        name for name, chain in model.items()
        if export.minimum_distance(chain.heavy, model_ligand) <= 25.0
    }
    per_group = []
    for group_index, group in enumerate(groups):
        reference_names = [name for name in group if name in site]
        if not reference_names:
            continue
        model_names = [name for name in mapped_model_groups[group_index] if name in close_model]
        if len(model_names) >= len(reference_names):
            choices = [
                dict(zip(reference_names, permutation))
                for permutation in itertools.permutations(model_names, len(reference_names))
            ]
        else:
            choices = []
            for selected_refs in itertools.combinations(reference_names, len(model_names)):
                for permutation in itertools.permutations(model_names):
                    choices.append(dict(zip(selected_refs, permutation)))
        per_group.append(choices)
    if not per_group or any(not choices for choices in per_group):
        return []
    result = []
    for parts in itertools.product(*per_group):
        merged = {}
        for part in parts:
            merged.update(part)
        result.append(merged)
    return result


def transform_for_mapping(
    target: dict[str, Chain], model: dict[str, Chain], site: dict[str, set[int]],
    mapping: dict[str, str], group_representative: dict[str, str]
) -> tuple[np.ndarray, np.ndarray, int, float]:
    pairs = []
    for target_name, model_name in mapping.items():
        representative_name = group_representative[target_name]
        representative = target[representative_name]
        representative_model_pairs, _, _, _ = alignment_pairs(
            representative.sequence, model[model_name].sequence
        )
        if target_name == representative_name:
            seq_pairs = representative_model_pairs
        else:
            representative_target_pairs, _, _, _ = alignment_pairs(
                representative.sequence, target[target_name].sequence
            )
            target_by_representative = {
                representative_index: target_index
                for representative_index, target_index in representative_target_pairs
            }
            model_by_representative = {
                representative_index: model_index
                for representative_index, model_index in representative_model_pairs
            }
            seq_pairs = [
                (target_by_representative[index], model_by_representative[index])
                for index in sorted(target_by_representative.keys() & model_by_representative.keys())
            ]
        pocket = site[target_name]
        pairs.extend((target_name, ti, model_name, mi) for ti, mi in seq_pairs if ti in pocket)
    if not pairs:
        raise ValueError("mapping has no aligned binding-site residues")
    target_ca = np.array([
        target[tn].residues[ti].atoms["CA"] for tn, ti, mn, mi in pairs
    ])
    model_ca = np.array([
        model[mn].residues[mi].atoms["CA"] for tn, ti, mn, mi in pairs
    ])
    if len(pairs) < 3:
        target_xyz = np.array([
            target[tn].residues[ti].atoms[atom]
            for tn, ti, mn, mi in pairs for atom in ("N", "CA", "C")
        ])
        model_xyz = np.array([
            model[mn].residues[mi].atoms[atom]
            for tn, ti, mn, mi in pairs for atom in ("N", "CA", "C")
        ])
    else:
        target_xyz = np.array([target[tn].residues[ti].atoms["CA"] for tn, ti, mn, mi in pairs])
        model_xyz = np.array([model[mn].residues[mi].atoms["CA"] for tn, ti, mn, mi in pairs])
    rotation, translation = export.kabsch(model_xyz, target_xyz)
    aligned_ca = export.transform_coordinates(model_ca, rotation, translation)
    bb_rmsd = float(np.sqrt(np.mean(np.sum((aligned_ca - target_ca) ** 2, axis=1))))
    return rotation, translation, len(pairs), bb_rmsd


def raw_path_for(manifest_record: dict, members: dict[str, str], raw_dir: Path) -> Path:
    member = manifest_record["source"]["archiveMember"]
    return raw_dir / members[member]


def score_record(
    manifest: dict,
    record: dict,
    raw_dir: Path,
    members: dict[str, str],
    ground_dir: Path,
    score_rows: dict[tuple[str, str, str, str], list[dict[str, str]]],
    system_output_dir: Path,
) -> dict:
    target_name = manifest["target"]
    target_dir = ground_dir / target_name
    sdf_path = target_dir / f'{manifest["ligandInstance"]}.sdf'
    raw_path = raw_path_for(record, members, raw_dir)
    member_match = re.search(r"seed-([^/]+)_sample-(\d+)\.cif$", record["source"]["archiveMember"])
    if member_match is None:
        raise ValueError("cannot extract source seed/sample from archive member")
    key = (record["method"], target_name, member_match.group(1), member_match.group(2))
    official_candidates = [
        row for row in score_rows.get(key, [])
        if row.get("rmsd") not in (None, "", "nan")
        and abs(float(row["rmsd"]) - float(record["publishedRmsdAngstrom"])) <= 0.0006
    ]
    official_by_chain = {
        row["model_ligand_chain_rmsd"]: row for row in official_candidates
    }
    if len(official_by_chain) != 1:
        raise ValueError(
            f"expected one official RMSD model chain for {key}; got "
            f"{sorted(official_by_chain)}"
        )
    official_row = next(iter(official_by_chain.values()))
    official_model_chain = official_row["model_ligand_chain_rmsd"]
    model_ligand_xyz, model_mol, resolved_model_ligand = selected_model_ligand(
        raw_path, official_model_chain, record["method"]
    )
    ref_mol, target_copies = reference_ligand_copies(
        target_dir / "system.cif", sdf_path, manifest["ligandInstance"]
    )
    ligand_symmetries = symmetries(ref_mol, model_mol)
    target_structure = read_structure(target_dir / "receptor.cif")
    model_structure = read_structure(raw_path)
    target_protein = protein_chains(target_structure)
    model_protein = protein_chains(model_structure)
    groups = target_groups(target_protein)
    group_representative = {
        target_chain: group[0] for group in groups for target_chain in group
    }
    model_groups = model_group_mapping(target_protein, model_protein, groups)
    candidates = []
    for target_copy, target_ligand_xyz in target_copies.items():
        site = binding_site(target_protein, target_ligand_xyz)
        options = mapping_options(
            target_protein, model_protein, groups, model_groups, site, model_ligand_xyz
        )
        for mapping in options:
            rotation, translation, residue_count, bb_rmsd = transform_for_mapping(
                target_protein, model_protein, site, mapping, group_representative
            )
            rmsd = rmsd_for_symmetries(
                model_ligand_xyz, target_ligand_xyz, ligand_symmetries, rotation, translation
            )
            candidates.append({
                "rmsd": rmsd,
                "target_copy": target_copy,
                "chain_mapping": mapping,
                "rotation": rotation,
                "translation": translation,
                "binding_site_residue_count": residue_count,
                "bb_rmsd": bb_rmsd,
            })
    if not candidates:
        raise ValueError("no binding-site representations")
    published = float(official_row["rmsd"])
    closest = min(candidates, key=lambda item: abs(item["rmsd"] - published))
    lowest = min(candidates, key=lambda item: item["rmsd"])
    pose_structure = read_structure(system_output_dir / record["poseFile"])
    pose_atoms = [
        atom for chain in pose_structure[0] for residue in chain for atom in residue
        if atom.element.name not in ("H", "D")
    ]
    pose_coordinates = np.array([pos(atom) for atom in pose_atoms])
    if len(pose_coordinates) != len(model_ligand_xyz):
        raise ValueError(
            f"exported pose has {len(pose_coordinates)} heavy atoms; expected "
            f"{len(model_ligand_xyz)}"
        )
    post_alignment_rmsd = rmsd_for_symmetries(
        pose_coordinates,
        target_copies[closest["target_copy"]],
        ligand_symmetries,
        np.eye(3),
        np.zeros(3),
    )
    return {
        "system_id": manifest["systemId"],
        "sample": record["sample"],
        "method": record["method"],
        "model_ligand_chain": resolved_model_ligand,
        "official_model_ligand_chain": official_model_chain,
        "indexed_model_ligand_chain": record["source"]["predictedLigandChain"],
        "published_rmsd": published,
        "reproduced_rmsd": closest["rmsd"],
        "absolute_delta": abs(closest["rmsd"] - published),
        "post_alignment_rmsd": post_alignment_rmsd,
        "post_alignment_absolute_delta": abs(post_alignment_rmsd - published),
        "lowest_candidate_rmsd": lowest["rmsd"],
        "target_copy": closest["target_copy"],
        "chain_mapping": closest["chain_mapping"],
        "rotation": closest["rotation"].tolist(),
        "translation": closest["translation"].tolist(),
        "binding_site_residue_count": closest["binding_site_residue_count"],
        "binding_site_bb_rmsd": closest["bb_rmsd"],
        "reported_binding_site_bb_rmsd": float(official_row["bb_rmsd"]),
        "binding_site_bb_rmsd_delta": abs(
            closest["bb_rmsd"] - float(official_row["bb_rmsd"])
        ),
        "symmetry_count": len(ligand_symmetries),
        "candidate_count": len(candidates),
    }


def rewrite_exported_pose(
    system_output_dir: Path,
    manifest: dict,
    record: dict,
    result: dict,
    raw_dir: Path,
    members: dict[str, str],
) -> None:
    raw_path = raw_path_for(record, members, raw_dir)
    structure = read_structure(raw_path)
    chain = next(
        (chain for chain in structure[0] if chain.name == result["model_ligand_chain"]),
        None,
    )
    if chain is None:
        raise ValueError(
            f'raw ligand chain {result["model_ligand_chain"]} was not found'
        )
    atoms = chain_heavy_atoms(chain)
    raw_coordinates = np.array([pos(atom) for atom in atoms])
    rotation = np.array(result["rotation"])
    translation = np.array(result["translation"])
    aligned_coordinates = export.transform_coordinates(
        raw_coordinates, rotation, translation
    )
    export.write_ligand_pdb(
        system_output_dir / record["poseFile"],
        [atom.element.name.upper() for atom in atoms],
        [atom.name for atom in atoms],
        aligned_coordinates,
    )
    if record.get("fullPredictionFile"):
        export.transform_structure(structure, rotation, translation)
        structure.make_mmcif_document().write_file(
            str(system_output_dir / record["fullPredictionFile"])
        )

    indexed_chain = record["source"].get(
        "indexMatchedLigandChain",
        record["source"].get("predictedLigandChain"),
    )
    record["publishedRmsdAngstrom"] = result["published_rmsd"]
    record["source"].update(
        {
            "indexMatchedLigandChain": indexed_chain,
            "predictedLigandChain": result["official_model_ligand_chain"],
            "resolvedPredictedLigandChain": result["model_ligand_chain"],
            "sameChemistryIndexCorrected": (
                indexed_chain != result["official_model_ligand_chain"]
            ),
            "resolvedCrystalLigandChain": result["target_copy"],
            "equivalentCrystalCopyUsed": (
                result["target_copy"] != manifest["ligandInstance"]
            ),
        }
    )
    record["source"].pop("symmetryCopyReassigned", None)
    record["alignment"] = {
        "scorer": "OpenStructure 2.8 BiSyRMSD-compatible",
        "scope": "4A target binding site",
        "chainMapping": result["chain_mapping"],
        "matchedBindingSiteResidueCount": result["binding_site_residue_count"],
        "bindingSiteBackboneRmsdAngstrom": round(
            result["binding_site_bb_rmsd"], 6
        ),
        "reportedBindingSiteBackboneRmsdAngstrom": result[
            "reported_binding_site_bb_rmsd"
        ],
        "ligandSymmetryCount": result["symmetry_count"],
        "recomputedRmsdAngstrom": round(result["reproduced_rmsd"], 6),
        "reportedRmsdDeltaAngstrom": round(result["absolute_delta"], 6),
        "rotation": result["rotation"],
        "translation": result["translation"],
    }
    record["validation"] = {
        "status": "pending-post-write-check",
        "metric": "binding-site-superposed symmetry-corrected ligand RMSD",
        "toleranceAngstrom": 0.005,
    }


def update_post_write_validation(
    system_output_dir: Path,
    manifest: dict,
    record: dict,
    result: dict,
    raw_dir: Path,
    members: dict[str, str],
    ground_dir: Path,
) -> None:
    raw_path = raw_path_for(record, members, raw_dir)
    _, model_mol, _ = selected_model_ligand(
        raw_path,
        result["official_model_ligand_chain"],
        record["method"],
    )
    target_dir = ground_dir / manifest["target"]
    ref_mol, target_copies = reference_ligand_copies(
        target_dir / "system.cif",
        target_dir / f'{manifest["ligandInstance"]}.sdf',
        manifest["ligandInstance"],
    )
    mappings = symmetries(ref_mol, model_mol)
    pose = read_structure(system_output_dir / record["poseFile"])
    coordinates = np.array(
        [
            pos(atom)
            for chain in pose[0]
            for residue in chain
            for atom in residue
            if atom.element.name not in ("H", "D")
        ]
    )
    post_rmsd = rmsd_for_symmetries(
        coordinates,
        target_copies[result["target_copy"]],
        mappings,
        np.eye(3),
        np.zeros(3),
    )
    delta = abs(post_rmsd - result["published_rmsd"])
    record["alignment"]["postAlignmentRmsdAngstrom"] = round(post_rmsd, 6)
    record["alignment"]["postAlignmentRmsdDeltaAngstrom"] = round(delta, 6)
    record["validation"]["status"] = "validated" if delta <= 0.005 else "failed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aligned-dir", type=Path, default=Path("data_rnp_aligned"))
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--ground-dir", type=Path, required=True)
    parser.add_argument("--score-tables-dir", type=Path, required=True)
    parser.add_argument("--system", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rewrite", action="store_true")
    args = parser.parse_args()
    collection = json.loads((args.aligned_dir / "collection.json").read_text())
    members = json.loads((args.raw_dir / "members.json").read_text())
    score_rows: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    for csv_path in args.score_tables_dir.glob("*.csv"):
        method = csv_path.stem
        with csv_path.open() as handle:
            reader = csv.DictReader(handle)
            if not {"target", "seed", "sample", "rmsd"}.issubset(reader.fieldnames or []):
                continue
            for row in reader:
                score_rows[(method, row["target"], row["seed"], row["sample"])].append(row)
    wanted = set(args.system or [])
    rows = []
    failures = []
    for system_index in collection["systems"]:
        if wanted and system_index["systemId"] not in wanted:
            continue
        manifest = json.loads((args.aligned_dir / system_index["manifest"]).read_text())
        for record in manifest["predictions"]:
            if args.limit is not None and len(rows) >= args.limit:
                break
            try:
                system_output_dir = args.aligned_dir / manifest["systemId"]
                row = score_record(
                    manifest,
                    record,
                    args.raw_dir,
                    members,
                    args.ground_dir,
                    score_rows,
                    system_output_dir,
                )
                if args.rewrite:
                    rewrite_exported_pose(
                        system_output_dir,
                        manifest,
                        record,
                        row,
                        args.raw_dir,
                        members,
                    )
                    update_post_write_validation(
                        system_output_dir,
                        manifest,
                        record,
                        row,
                        args.raw_dir,
                        members,
                        args.ground_dir,
                    )
                    row["post_alignment_rmsd"] = record["alignment"][
                        "postAlignmentRmsdAngstrom"
                    ]
                    row["post_alignment_absolute_delta"] = record["alignment"][
                        "postAlignmentRmsdDeltaAngstrom"
                    ]
                rows.append(row)
                print(
                    f'{row["system_id"]} {row["sample"]:2d} {row["method"]:8s} '
                    f'{row["published_rmsd"]:7.3f} {row["reproduced_rmsd"]:9.4f} '
                    f'delta={row["absolute_delta"]:.4f}', flush=True
                )
            except Exception as error:
                failures.append({"system_id": manifest["systemId"], "sample": record["sample"], "error": str(error)})
                print(f'ERROR {manifest["systemId"]} {record["sample"]}: {error}', flush=True)
        if args.limit is not None and len(rows) >= args.limit:
            break
        if args.rewrite:
            scored_copies = {
                row["target_copy"]
                for row in rows
                if row["system_id"] == manifest["systemId"]
            }
            manifest["schemaVersion"] = "foldarium-crystal-aligned/v2"
            manifest["crystal"]["benchmarkLigandInstance"] = manifest[
                "ligandInstance"
            ]
            manifest["crystal"]["rmsdScoredLigandInstances"] = sorted(
                scored_copies
            )
            if len(scored_copies) == 1:
                scored_copy = next(iter(scored_copies))
                if scored_copy != manifest["ligandInstance"]:
                    target_dir = args.ground_dir / manifest["target"]
                    _, copies = reference_ligand_copies(
                        target_dir / "system.cif",
                        target_dir / f'{manifest["ligandInstance"]}.sdf',
                        manifest["ligandInstance"],
                    )
                    sdf_elements, sdf_names, _ = export.parse_sdf(
                        target_dir / f'{manifest["ligandInstance"]}.sdf'
                    )
                    scored_name = "rmsd_scored_xtal_lig.pdb"
                    export.write_ligand_pdb(
                        args.aligned_dir / manifest["systemId"] / scored_name,
                        sdf_elements,
                        sdf_names,
                        copies[scored_copy],
                    )
                    manifest["crystal"].setdefault(
                        "benchmarkLigandFile",
                        manifest["crystal"]["ligandFile"],
                    )
                    manifest["crystal"]["ligandFile"] = scored_name
            system_failures = [
                f'pose {record["sample"]}: post-alignment RMSD mismatch'
                for record in manifest["predictions"]
                if record["validation"]["status"] != "validated"
            ]
            manifest["validation"] = {
                "status": "validated" if not system_failures else "failed",
                "failures": system_failures,
            }
            (args.aligned_dir / system_index["manifest"]).write_text(
                json.dumps(manifest, indent=2) + "\n"
            )
    if args.rewrite:
        collection["schemaVersion"] = "foldarium-crystal-aligned/v2"
        collection["validation"] = {
            "status": "validated" if not failures else "failed",
            "systemCount": len(collection["systems"]),
            "poseCount": sum(item["poseCount"] for item in collection["systems"]),
            "metric": "OpenStructure 2.8-compatible BiSyRMSD",
            "postAlignmentToleranceAngstrom": 0.005,
            "failures": failures,
        }
        (args.aligned_dir / "collection.json").write_text(
            json.dumps(collection, indent=2) + "\n"
        )
    deltas = np.array([row["absolute_delta"] for row in rows])
    summary = {
        "pose_count": len(rows),
        "failure_count": len(failures),
        "within_0_002": int(np.sum(deltas <= 0.002)) if len(deltas) else 0,
        "within_0_01": int(np.sum(deltas <= 0.01)) if len(deltas) else 0,
        "median_absolute_delta": float(np.median(deltas)) if len(deltas) else None,
        "maximum_absolute_delta": float(np.max(deltas)) if len(deltas) else None,
        "post_alignment_within_0_005": int(
            sum(row["post_alignment_absolute_delta"] <= 0.005 for row in rows)
        ),
        "maximum_post_alignment_absolute_delta": max(
            (row["post_alignment_absolute_delta"] for row in rows), default=None
        ),
    }
    report = {
        "schemaVersion": "foldarium-rnp-bisyrmsd-audit/v1",
        "referenceScorer": "OpenStructure 2.8 SCRMSDScorer",
        "toleranceAngstrom": 0.005,
        "summary": summary,
        "poses": rows,
        "failures": failures,
    }
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if failures or any(
        row["post_alignment_absolute_delta"] > 0.005 for row in rows
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
