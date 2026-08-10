"""Reference-coordinate ligand-pose evaluation for Wednesday reveal.

Heavy scientific dependencies are imported lazily so Saturday intake and GPU
workers remain dependency-free.  The scorer aligns compatible receptor chains,
tries reference/predicted ligand copies, and computes the lowest graph-symmetry
aware heavy-atom RMSD without subsequently fitting the ligand itself.
"""

from __future__ import annotations

import difflib
import math
import re
from pathlib import Path
from collections.abc import Collection
from typing import Any

EVALUATOR_VERSION = "foldarium-receptor-aligned-symmetry-rmsd/v1"


class EvaluationError(RuntimeError):
    """Raised when a pose cannot be evaluated unambiguously."""


def _dependencies():
    try:
        import gemmi
        import numpy
        from rdkit import Chem
        from rdkit.Chem import rdDetermineBonds
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError(
            "evaluation requires Gemmi, NumPy, and RDKit; install the evaluation runtime"
        ) from exc
    return gemmi, numpy, Chem, rdDetermineBonds


def _heavy_atoms(residue: Any) -> list[Any]:
    return [atom for atom in residue if atom.element.name != "H"]


def _coordinates(residue: Any, numpy: Any) -> Any:
    return numpy.array(
        [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in _heavy_atoms(residue)],
        dtype=float,
    )


def _polymer_chains(model: Any) -> list[tuple[str, Any]]:
    return [(chain.name, chain.get_polymer()) for chain in model if len(chain.get_polymer()) >= 5]


def _sequence(polymer: Any, gemmi: Any) -> str:
    return gemmi.one_letter_code([residue.name for residue in polymer])


def _atom_position(residue: Any, name: str) -> Any | None:
    for atom in residue:
        if atom.name.strip() == name:
            return atom.pos
    return None


def _sequence_aligned_positions(
    reference: Any, predicted: Any, gemmi: Any
) -> tuple[list[Any], list[Any]]:
    reference_sequence = _sequence(reference, gemmi)
    predicted_sequence = _sequence(predicted, gemmi)
    alignment = gemmi.align_string_sequences(
        list(reference_sequence), list(predicted_sequence), []
    )
    reference_index = 0
    predicted_index = 0
    reference_positions: list[Any] = []
    predicted_positions: list[Any] = []
    for count_text, operation in re.findall(r"(\d+)([MID])", alignment.cigar_str()):
        count = int(count_text)
        if operation == "I":
            reference_index += count
            continue
        if operation == "D":
            predicted_index += count
            continue
        for offset in range(count):
            reference_position = _atom_position(reference[reference_index + offset], "CA")
            predicted_position = _atom_position(predicted[predicted_index + offset], "CA")
            if reference_position is not None and predicted_position is not None:
                reference_positions.append(reference_position)
                predicted_positions.append(predicted_position)
        reference_index += count
        predicted_index += count
    if len(reference_positions) < 5:
        raise EvaluationError("fewer than five sequence-aligned receptor C-alpha atoms")
    return reference_positions, predicted_positions


def _sequence_superposition(reference: Any, predicted: Any, gemmi: Any) -> Any:
    """Superpose predicted onto reference using sequence-aligned C-alpha pairs."""

    reference_positions, predicted_positions = _sequence_aligned_positions(
        reference, predicted, gemmi
    )
    return gemmi.superpose_positions(reference_positions, predicted_positions)


def _robust_sequence_superposition(
    reference: Any,
    predicted: Any,
    gemmi: Any,
    *,
    cutoff_angstrom: float = 2.0,
    maximum_cycles: int = 5,
) -> tuple[Any, dict[str, Any]]:
    """Fit a sequence-identical rigid core while rejecting flexible outliers."""

    reference_positions, predicted_positions = _sequence_aligned_positions(
        reference, predicted, gemmi
    )
    original_count = len(reference_positions)
    minimum_count = max(5, math.ceil(original_count * 0.2))

    def fit_positions(indices: list[int]) -> Any:
        return gemmi.superpose_positions(
            [reference_positions[index] for index in indices],
            [predicted_positions[index] for index in indices],
        )

    def residuals(fit: Any) -> list[float]:
        values: list[float] = []
        for index in range(original_count):
            transformed = fit.transform.apply(predicted_positions[index])
            reference_position = reference_positions[index]
            values.append(
                math.sqrt(
                    (transformed.x - reference_position.x) ** 2
                    + (transformed.y - reference_position.y) ** 2
                    + (transformed.z - reference_position.z) ** 2
                )
            )
        return values

    def refine(seed: list[int]) -> tuple[Any, list[int], int] | None:
        retained = seed
        cycles = 0
        for _ in range(maximum_cycles):
            fit = fit_positions(retained)
            next_retained = [
                index
                for index, distance in enumerate(residuals(fit))
                if distance <= cutoff_angstrom
            ]
            if len(next_retained) < minimum_count:
                return None
            cycles += 1
            if next_retained == retained:
                return fit, retained, cycles
            retained = next_retained
        return fit_positions(retained), retained, cycles

    # An all-residue fit can sit between two domains, leaving even a large true
    # core outside the final 2 A cutoff.  Build one deterministic least-trimmed
    # path first: repeatedly retain the 75% lowest-residual pairs, never fewer
    # than the documented 20%/five-residue floor.  Every coarse fit that already
    # has enough absolute-cutoff support seeds a full refinement over all pairs,
    # which also permits previously trimmed residues to re-enter.
    coarse_retained = list(range(original_count))
    coarse_retained_counts: list[int] = []
    candidates: list[tuple[tuple[Any, ...], Any, list[int], int]] = []

    def record_candidate(seed_fit: Any) -> None:
        inliers = [
            index
            for index, distance in enumerate(residuals(seed_fit))
            if distance <= cutoff_angstrom
        ]
        if len(inliers) < minimum_count:
            return
        refined = refine(inliers)
        if refined is None:
            return
        fit, retained, cycles = refined
        candidates.append(
            (
                (-len(retained), float(fit.rmsd), tuple(retained)),
                fit,
                retained,
                cycles,
            )
        )

    while True:
        coarse_fit = fit_positions(coarse_retained)
        coarse_retained_counts.append(len(coarse_retained))
        coarse_residuals = residuals(coarse_fit)
        record_candidate(coarse_fit)
        if len(coarse_retained) == minimum_count:
            break
        next_count = max(
            minimum_count,
            math.floor(len(coarse_retained) * 0.75),
        )
        ranked = sorted(
            range(original_count),
            key=lambda index: (coarse_residuals[index], index),
        )
        coarse_retained = sorted(ranked[:next_count])

    # The global least-trimmed path can still converge on a compact mobile
    # domain when a long protein's lever arm biases the first rotation.  Seed a
    # bounded set of overlapping contiguous sequence windows as alternative
    # domain hypotheses.  A window is only a seed: final support is always
    # measured over every aligned residue, so the largest coherent core wins.
    window_step = max(1, minimum_count // 2)
    window_starts = list(
        range(0, original_count - minimum_count + 1, window_step)
    )
    final_start = original_count - minimum_count
    if window_starts[-1] != final_start:
        window_starts.append(final_start)
    for start in window_starts:
        record_candidate(fit_positions(list(range(start, start + minimum_count))))

    if not candidates:
        raise EvaluationError(
            "robust receptor alignment retained too few sequence-aligned residues"
        )
    _score, fit, retained, cycles = min(candidates, key=lambda row: row[0])
    return fit, {
        "policy": "sequence-ca-iterative-outlier-rejection/v1",
        "cutoff_angstrom": float(cutoff_angstrom),
        "maximum_cycles": int(maximum_cycles),
        "cycles_completed": cycles,
        "coarse_policy": (
            "deterministic-75-percent-least-trimmed-plus-sequence-windows/v1"
        ),
        "coarse_retained_counts": coarse_retained_counts,
        "coarse_window_count": len(window_starts),
        "aligned_residue_count": original_count,
        "retained_residue_count": len(retained),
        "retained_fraction": len(retained) / original_count,
    }


def _receptor_candidate_key(
    candidate: dict[str, Any], *, stable_chain_pair: bool
) -> tuple[Any, ...]:
    if stable_chain_pair:
        return (
            -candidate["sequence_similarity"],
            candidate["reference_chain"],
            candidate["predicted_chain"],
            candidate["receptor_rmsd"],
        )
    return (
        -candidate["sequence_similarity"],
        candidate["receptor_rmsd"],
        candidate["reference_chain"],
        candidate["predicted_chain"],
    )


def best_receptor_superposition(
    reference_model: Any,
    predicted_model: Any,
    *,
    stable_chain_pair: bool = False,
    reference_chain_ids: Collection[str] | None = None,
    predicted_chain_ids: Collection[str] | None = None,
    robust_core: bool = False,
    expected_sequence: str | None = None,
) -> dict[str, Any]:
    """Return a sequence-compatible transform from prediction to reference.

    Evaluation retains its historical lowest-RMSD chain choice. Blind ensemble
    assembly opts into ``stable_chain_pair`` so equivalent chains cannot change
    the shared coordinate frame from one predicted pose to the next.
    """

    try:
        import gemmi
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError("receptor alignment requires Gemmi") from exc
    allowed_reference = set(reference_chain_ids) if reference_chain_ids is not None else None
    allowed_predicted = set(predicted_chain_ids) if predicted_chain_ids is not None else None
    if allowed_reference is not None and not allowed_reference:
        raise EvaluationError("reference receptor-chain filter is empty")
    if allowed_predicted is not None and not allowed_predicted:
        raise EvaluationError("predicted receptor-chain filter is empty")
    if expected_sequence is not None and (
        not expected_sequence or not expected_sequence.isalpha()
    ):
        raise EvaluationError("expected receptor sequence must contain letters only")
    normalized_expected_sequence = (
        expected_sequence.upper() if expected_sequence is not None else None
    )
    best: dict[str, Any] | None = None
    for reference_chain, reference_polymer in _polymer_chains(reference_model):
        if allowed_reference is not None and reference_chain not in allowed_reference:
            continue
        reference_sequence = _sequence(reference_polymer, gemmi)
        if (
            normalized_expected_sequence is not None
            and reference_sequence != normalized_expected_sequence
        ):
            continue
        for predicted_chain, predicted_polymer in _polymer_chains(predicted_model):
            if allowed_predicted is not None and predicted_chain not in allowed_predicted:
                continue
            predicted_sequence = _sequence(predicted_polymer, gemmi)
            if (
                normalized_expected_sequence is not None
                and predicted_sequence != normalized_expected_sequence
            ):
                continue
            similarity = difflib.SequenceMatcher(
                None, reference_sequence, predicted_sequence, autojunk=False
            ).ratio()
            if similarity < 0.5:
                continue
            try:
                if robust_core:
                    superposition, robust_audit = _robust_sequence_superposition(
                        reference_polymer, predicted_polymer, gemmi
                    )
                else:
                    superposition = _sequence_superposition(
                        reference_polymer, predicted_polymer, gemmi
                    )
                    robust_audit = None
            except Exception:
                continue
            if not math.isfinite(superposition.rmsd):
                continue
            candidate = {
                "reference_chain": reference_chain,
                "predicted_chain": predicted_chain,
                "sequence_similarity": similarity,
                "receptor_rmsd": float(superposition.rmsd),
                "transform": superposition.transform,
                "chain_selection_policy": (
                    "filtered-stable-sequence-chain-pair/v1"
                    if stable_chain_pair and allowed_reference is not None
                    else (
                        "stable-sequence-chain-pair/v1"
                        if stable_chain_pair
                        else "best-sequence-then-rmsd/v1"
                    )
                ),
            }
            if robust_audit is not None:
                candidate["robust_core"] = robust_audit
            if normalized_expected_sequence is not None:
                candidate["sequence_binding_policy"] = "exact-task-sequence/v1"
            if best is None or _receptor_candidate_key(
                candidate, stable_chain_pair=stable_chain_pair
            ) < _receptor_candidate_key(best, stable_chain_pair=stable_chain_pair):
                best = candidate
    if best is None:
        raise EvaluationError("no compatible receptor chains could be aligned")
    return best


def _ligands(model: Any, heavy_atoms: int, component_id: str | None = None) -> list[tuple[str, Any]]:
    component = component_id.upper() if component_id else None
    rows: list[tuple[str, Any]] = []
    for chain in model:
        for residue in chain:
            if component is not None and residue.name.upper() != component:
                continue
            if len(_heavy_atoms(residue)) == heavy_atoms:
                rows.append((chain.name, residue))
    return rows


def _connectivity_molecule(residue: Any, Chem: Any, rdDetermineBonds: Any) -> Any:
    atoms = _heavy_atoms(residue)
    molecule = Chem.RWMol()
    conformer = Chem.Conformer(len(atoms))
    for index, atom in enumerate(atoms):
        molecule.AddAtom(Chem.Atom(atom.element.atomic_number))
        conformer.SetAtomPosition(index, (atom.pos.x, atom.pos.y, atom.pos.z))
    result = molecule.GetMol()
    result.AddConformer(conformer)
    try:
        rdDetermineBonds.DetermineConnectivity(result)
    except Exception as exc:
        raise EvaluationError("could not infer ligand connectivity from coordinates") from exc
    # Bond order does not affect symmetry for RMSD here; using connectivity-only
    # single bonds avoids differences in aromatic/bond-order annotation between
    # an experimental CCD residue and a method's generic LIG component.
    for bond in result.GetBonds():
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    for atom in result.GetAtoms():
        atom.SetIsAromatic(False)
        atom.SetNoImplicit(True)
    return result


def _symmetry_mappings(predicted: Any, reference: Any) -> tuple[tuple[int, ...], ...]:
    mappings = reference.GetSubstructMatches(
        predicted, uniquify=False, useChirality=False, maxMatches=100_000
    )
    if not mappings:
        raise EvaluationError("predicted and reference ligand connectivity do not match")
    return mappings


def _apply_transform(coordinates: Any, transform: Any, gemmi: Any, numpy: Any) -> Any:
    transformed = []
    for x, y, z in coordinates:
        position = transform.apply(gemmi.Position(float(x), float(y), float(z)))
        transformed.append([position.x, position.y, position.z])
    return numpy.array(transformed, dtype=float)


def _mapped_rmsd(
    predicted: Any, reference: Any, mappings: Any, numpy: Any
) -> tuple[float, tuple[int, ...]]:
    best = math.inf
    best_mapping: tuple[int, ...] | None = None
    for mapping in mappings:
        ordered_reference = reference[list(mapping)]
        delta = predicted - ordered_reference
        value = float(numpy.sqrt(numpy.sum(delta * delta) / len(mapping)))
        if value < best:
            best = value
            best_mapping = tuple(int(index) for index in mapping)
    if best_mapping is None:
        raise EvaluationError("no ligand symmetry mapping could be scored")
    return best, best_mapping


def _reference_order(predicted: Any, mapping: tuple[int, ...], numpy: Any) -> Any:
    """Reorder predicted coordinates so every model shares reference atom order."""

    ordered = numpy.empty_like(predicted)
    for predicted_index, reference_index in enumerate(mapping):
        ordered[reference_index] = predicted[predicted_index]
    return ordered


def _transform_json(transform: Any) -> dict[str, Any]:
    matrix = [[float(value) for value in row] for row in transform.mat.tolist()]
    vector = [float(transform.vec.x), float(transform.vec.y), float(transform.vec.z)]
    return {"rotation": matrix, "translation": vector}


def evaluate_ligand_pose(
    reference_path: str | Path,
    prediction_path: str | Path,
    *,
    component_id: str,
    heavy_atoms: int,
) -> dict[str, Any]:
    """Evaluate one predicted complex against one released reference assembly."""

    if not isinstance(component_id, str) or not component_id.strip():
        raise EvaluationError("component_id must be non-empty")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise EvaluationError("heavy_atoms must be a positive integer")
    gemmi, numpy, Chem, rdDetermineBonds = _dependencies()
    try:
        reference = gemmi.read_structure(str(reference_path))
        prediction = gemmi.read_structure(str(prediction_path))
        reference.setup_entities()
        prediction.setup_entities()
        reference_model = reference[0]
        prediction_model = prediction[0]
    except Exception as exc:
        raise EvaluationError("could not parse reference/prediction coordinates") from exc

    reference_ligands = _ligands(reference_model, heavy_atoms, component_id)
    predicted_ligands = _ligands(prediction_model, heavy_atoms)
    reference_polymers = _polymer_chains(reference_model)
    predicted_polymers = _polymer_chains(prediction_model)
    if not reference_ligands:
        raise EvaluationError(f"reference contains no {component_id} ligand with {heavy_atoms} atoms")
    if not predicted_ligands:
        raise EvaluationError(f"prediction contains no ligand with {heavy_atoms} heavy atoms")
    if not reference_polymers or not predicted_polymers:
        raise EvaluationError("reference and prediction must both contain a receptor polymer")

    best: dict[str, Any] | None = None
    mapping_cache: dict[tuple[int, int], Any] = {}
    for reference_chain, reference_polymer in reference_polymers:
        reference_sequence = _sequence(reference_polymer, gemmi)
        for predicted_chain, predicted_polymer in predicted_polymers:
            predicted_sequence = _sequence(predicted_polymer, gemmi)
            similarity = difflib.SequenceMatcher(
                None, reference_sequence, predicted_sequence, autojunk=False
            ).ratio()
            if similarity < 0.5:
                continue
            try:
                superposition = _sequence_superposition(
                    reference_polymer, predicted_polymer, gemmi
                )
            except Exception:
                continue
            if not math.isfinite(superposition.rmsd):
                continue
            transform = superposition.transform
            for reference_ligand_chain, reference_ligand in reference_ligands:
                reference_coordinates = _coordinates(reference_ligand, numpy)
                for predicted_ligand_chain, predicted_ligand in predicted_ligands:
                    key = (id(predicted_ligand), id(reference_ligand))
                    mappings = mapping_cache.get(key)
                    if mappings is None:
                        predicted_molecule = _connectivity_molecule(
                            predicted_ligand, Chem, rdDetermineBonds
                        )
                        reference_molecule = _connectivity_molecule(
                            reference_ligand, Chem, rdDetermineBonds
                        )
                        mappings = _symmetry_mappings(predicted_molecule, reference_molecule)
                        mapping_cache[key] = mappings
                    predicted_coordinates = _apply_transform(
                        _coordinates(predicted_ligand, numpy), transform, gemmi, numpy
                    )
                    rmsd, symmetry_mapping = _mapped_rmsd(
                        predicted_coordinates, reference_coordinates, mappings, numpy
                    )
                    if best is None or rmsd < best["rmsd"]:
                        plddt_values = [
                            float(atom.b_iso) for atom in _heavy_atoms(predicted_ligand)
                        ]
                        best = {
                            "evaluator_version": EVALUATOR_VERSION,
                            "rmsd": rmsd,
                            "receptor_rmsd": float(superposition.rmsd),
                            "sequence_similarity": similarity,
                            "reference_receptor_chain": reference_chain,
                            "predicted_receptor_chain": predicted_chain,
                            "reference_ligand_chain": reference_ligand_chain,
                            "reference_ligand_residue": reference_ligand.name,
                            "predicted_ligand_chain": predicted_ligand_chain,
                            "predicted_ligand_residue": predicted_ligand.name,
                            "ligand_plddt": (
                                sum(plddt_values) / len(plddt_values) if plddt_values else None
                            ),
                            "transform": _transform_json(transform),
                            "predicted_ligand_coordinates": predicted_coordinates.tolist(),
                            "predicted_ligand_coordinates_reference_order": _reference_order(
                                predicted_coordinates, symmetry_mapping, numpy
                            ).tolist(),
                            "reference_ligand_coordinates": reference_coordinates.tolist(),
                            "symmetry_mapping": list(symmetry_mapping),
                            "predicted_ligand_atoms": [
                                {"name": atom.name, "element": atom.element.name}
                                for atom in _heavy_atoms(predicted_ligand)
                            ],
                            "reference_ligand_atoms": [
                                {"name": atom.name, "element": atom.element.name}
                                for atom in _heavy_atoms(reference_ligand)
                            ],
                        }
    if best is None:
        raise EvaluationError("no compatible receptor/ligand mapping could be evaluated")
    return best


__all__ = [
    "EVALUATOR_VERSION",
    "EvaluationError",
    "best_receptor_superposition",
    "evaluate_ligand_pose",
]
