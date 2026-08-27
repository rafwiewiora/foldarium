"""Reference-coordinate ligand-pose evaluation for Wednesday reveal.

Heavy scientific dependencies are imported lazily so Saturday intake and GPU
workers remain dependency-free.  The scorer aligns compatible receptor chains,
tries reference/predicted ligand copies, and computes the lowest graph-symmetry
aware heavy-atom RMSD without subsequently fitting the ligand itself.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
from pathlib import Path
from collections.abc import Collection, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

EVALUATOR_VERSION = "foldarium-receptor-aligned-symmetry-rmsd/v4"
REFERENCE_POCKET_RADIUS_ANGSTROM = 8.0
# Released partial references must retain at least 80% of the CCD heavy-atom count.
# This general floor covers observed de-reacted terminal groups such as 15/18 R06
# (83.3%) and 21/24 IO0 (87.5%) without target-specific ratios.
PARTIAL_REFERENCE_COVERAGE_MIN = 0.80
RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY = (
    "foldarium.released-partial-reference-override/v1"
)
RELEASED_PARTIAL_REFERENCE_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("weekly-2026-08-22-beta-v1", "26WD"): {
        "target_id": "26WD",
        "component_id": "AAO",
        "expected_heavy_atoms": 66,
        "minimum_observed_heavy_atoms": 52,
    },
}

def _validate_minimum_reference_heavy_atoms_override(
    heavy_atoms: int,
    minimum_observed: int,
) -> None:
    if isinstance(minimum_observed, bool) or not isinstance(minimum_observed, int):
        raise EvaluationError(
            "minimum_reference_heavy_atoms must be a positive integer"
        )
    if minimum_observed < 1:
        raise EvaluationError(
            "minimum_reference_heavy_atoms must be a positive integer"
        )
    if minimum_observed >= heavy_atoms:
        raise EvaluationError(
            "minimum_reference_heavy_atoms must be below expected heavy_atoms"
        )


def released_partial_reference_override_for_item(
    round_id: str,
    item_id: str,
    *,
    target_id: str,
    component_id: str,
    heavy_atoms: int,
) -> dict[str, Any] | None:
    """Return one authenticated historical override or fail closed on mismatch."""

    if not isinstance(round_id, str) or not round_id.strip():
        raise EvaluationError("released partial-reference round_id is invalid")
    if not isinstance(item_id, str) or not item_id.strip():
        raise EvaluationError("released partial-reference item_id is invalid")
    if not isinstance(target_id, str) or not target_id.strip():
        raise EvaluationError("released partial-reference target_id is invalid")
    if not isinstance(component_id, str) or not component_id.strip():
        raise EvaluationError("released partial-reference component_id is invalid")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise EvaluationError("released partial-reference heavy_atoms is invalid")

    spec = RELEASED_PARTIAL_REFERENCE_OVERRIDES.get((round_id.strip(), item_id.strip()))
    if spec is None:
        return None
    normalized_target = target_id.strip().upper()
    normalized_component = component_id.strip().upper()
    if normalized_target != spec["target_id"]:
        raise EvaluationError(
            "released partial-reference override target_id binding mismatch"
        )
    if normalized_component != spec["component_id"]:
        raise EvaluationError(
            "released partial-reference override component_id binding mismatch"
        )
    if heavy_atoms != spec["expected_heavy_atoms"]:
        raise EvaluationError(
            "released partial-reference override heavy_atoms binding mismatch"
        )
    minimum_observed = spec["minimum_observed_heavy_atoms"]
    _validate_minimum_reference_heavy_atoms_override(heavy_atoms, minimum_observed)
    return {
        "policy": RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
        "minimum_observed_heavy_atoms": minimum_observed,
    }


def _partial_reference_coverage_threshold(
    heavy_atoms: int,
    *,
    minimum_observed_heavy_atoms: int | None,
) -> float:
    if minimum_observed_heavy_atoms is None:
        return PARTIAL_REFERENCE_COVERAGE_MIN
    _validate_minimum_reference_heavy_atoms_override(
        heavy_atoms, minimum_observed_heavy_atoms
    )
    return minimum_observed_heavy_atoms / heavy_atoms


LIGAND_MAPPING_POLICY_FULL = "full-reference-graph-symmetry/v1"
LIGAND_MAPPING_POLICY_PARTIAL = "partial-reference-connected-subgraph/v1"
LIGAND_MAPPING_POLICY_FULL_EXPLICIT = "full-reference-explicit-component-bonds/v1"
LIGAND_MAPPING_POLICY_PARTIAL_EXPLICIT = "partial-reference-explicit-component-bonds/v1"
LIGAND_MAPPING_POLICY_FULL_TASK_SMILES = (
    "full-reference-task-smiles-to-component-bonds/v1"
)
LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES = (
    "partial-reference-task-smiles-to-component-bonds/v1"
)
TOPOLOGY_SOURCE_INFERRED = "coordinate-inferred/v1"
TOPOLOGY_SOURCE_EXPLICIT = "chem-comp-bonds/v1"
TOPOLOGY_SOURCE_TASK_SMILES = "task-smiles/v1"

_CHEM_COMP_BOND_CACHE: dict[tuple[str, str], frozenset[tuple[str, str]] | None] = {}


class EvaluationError(RuntimeError):
    """Raised when a pose cannot be evaluated unambiguously."""


def _structure_dependencies():
    try:
        import gemmi
        import numpy
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError(
            "structure export requires Gemmi and NumPy; install the evaluation runtime"
        ) from exc
    return gemmi, numpy


def _dependencies():
    gemmi, numpy = _structure_dependencies()
    try:
        from rdkit import Chem
        from rdkit.Chem import rdDetermineBonds
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError(
            "evaluation requires Gemmi, NumPy, and RDKit; install the evaluation runtime"
        ) from exc
    return gemmi, numpy, Chem, rdDetermineBonds


def _heavy_atoms(residue: Any) -> list[Any]:
    return [atom for atom in residue if atom.element.name != "H"]


def _altloc_label(atom: Any) -> str:
    value = atom.altloc
    if not value or value in ("\0", " "):
        return ""
    return value


def _residue_altloc_keys(residue: Any) -> list[str]:
    keys = sorted(
        {
            label
            for atom in _heavy_atoms(residue)
            for label in [_altloc_label(atom)]
            if label
        }
    )
    return keys or [""]


def _conformer_heavy_atoms(residue: Any, altloc_key: str) -> list[Any]:
    if altloc_key == "":
        return _heavy_atoms(residue)
    atoms: list[Any] = []
    for atom in _heavy_atoms(residue):
        label = _altloc_label(atom)
        if label in ("", altloc_key):
            atoms.append(atom)
    return atoms


def _coordinates_from_atoms(atoms: Sequence[Any], numpy: Any) -> Any:
    return numpy.array(
        [[atom.pos.x, atom.pos.y, atom.pos.z] for atom in atoms],
        dtype=float,
    )


def _coordinates(residue: Any, numpy: Any) -> Any:
    return _coordinates_from_atoms(_heavy_atoms(residue), numpy)


def _polymer_chains(
    model: Any, *, minimum_residues: int = 5
) -> list[tuple[str, Any]]:
    return [
        (chain.name, chain.get_polymer())
        for chain in model
        if len(chain.get_polymer()) >= minimum_residues
    ]


def _sequence(polymer: Any, gemmi: Any) -> str:
    return gemmi.one_letter_code([residue.name for residue in polymer])


def _atom_position(residue: Any, name: str) -> Any | None:
    for atom in residue:
        if atom.name.strip() == name:
            return atom.pos
    return None


def _sequence_aligned_positions(
    reference: Any,
    predicted: Any,
    gemmi: Any,
    *,
    minimum_count: int = 5,
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
    if len(reference_positions) < minimum_count:
        raise EvaluationError(
            f"fewer than {minimum_count} sequence-aligned receptor C-alpha atoms"
        )
    return reference_positions, predicted_positions


def _sequence_superposition(reference: Any, predicted: Any, gemmi: Any) -> Any:
    """Superpose predicted onto reference using sequence-aligned C-alpha pairs."""

    reference_positions, predicted_positions = _sequence_aligned_positions(
        reference, predicted, gemmi
    )
    result = gemmi.superpose_positions(reference_positions, predicted_positions)
    if math.isfinite(result.rmsd):
        return result

    # Gemmi's platform-specific SVD can return a non-finite fit for a valid
    # rank-deficient point set (for example, a short collinear receptor in a
    # test or a genuinely linear peptide). NumPy's Kabsch SVD deterministically
    # resolves the same least-squares fit without changing successful Gemmi
    # results.
    try:
        import numpy
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError(
            "non-finite receptor alignment requires the NumPy fallback"
        ) from exc
    reference_array = numpy.asarray(
        [[position.x, position.y, position.z] for position in reference_positions],
        dtype=float,
    )
    predicted_array = numpy.asarray(
        [[position.x, position.y, position.z] for position in predicted_positions],
        dtype=float,
    )
    reference_center = reference_array.mean(axis=0)
    predicted_center = predicted_array.mean(axis=0)
    covariance = (predicted_array - predicted_center).T @ (
        reference_array - reference_center
    )
    left, _singular, right = numpy.linalg.svd(covariance)
    rotation = right.T @ left.T
    if numpy.linalg.det(rotation) < 0:
        right[-1] *= -1
        rotation = right.T @ left.T
    translation = reference_center - rotation @ predicted_center
    transformed = (rotation @ predicted_array.T).T + translation
    rmsd = float(
        numpy.sqrt(numpy.mean(numpy.sum((transformed - reference_array) ** 2, axis=1)))
    )
    if not math.isfinite(rmsd):
        raise EvaluationError("receptor alignment fallback is non-finite")
    transform = gemmi.Transform()
    transform.mat.fromlist(rotation.tolist())
    transform.vec.fromlist(translation.tolist())
    return SimpleNamespace(transform=transform, rmsd=rmsd)


def _robust_position_superposition(
    reference_positions: Sequence[Any],
    predicted_positions: Sequence[Any],
    gemmi: Any,
    *,
    group_labels: Sequence[str] | None = None,
    cutoff_angstrom: float = 2.0,
    maximum_cycles: int = 5,
) -> tuple[Any, dict[str, Any]]:
    """Fit the largest coherent rigid core across pre-matched C-alpha pairs."""

    original_count = len(reference_positions)
    if original_count != len(predicted_positions):
        raise EvaluationError("receptor C-alpha pair counts differ")
    if original_count < 5:
        raise EvaluationError("fewer than five sequence-aligned receptor C-alpha atoms")
    if group_labels is None:
        labels = ["receptor"] * original_count
    else:
        labels = list(group_labels)
        if len(labels) != original_count or any(not label for label in labels):
            raise EvaluationError("receptor C-alpha group labels are invalid")
    grouped_indices: dict[str, list[int]] = {}
    for index, label in enumerate(labels):
        grouped_indices.setdefault(label, []).append(index)
    # A complex-wide percentage would reject a large, well-defined domain just
    # because unrelated chains are also present.  Relative chain motion is an
    # explicit outlier case here, so meaningful support is 20% of the longest
    # submitted chain (or five residues), while candidates and ranking still
    # use every pooled chain and always choose the largest coherent core.
    minimum_count = max(
        5,
        math.ceil(max(len(indices) for indices in grouped_indices.values()) * 0.2),
    )

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

    # Global least-trimmed fitting can be trapped between independently moving
    # chains or domains.  Build deterministic sequence-local hypotheses within
    # each submitted chain.  A short window is only a seed: candidate support is
    # always re-measured over the complete pooled complex and must satisfy the
    # complex-wide 20%/five-residue floor above.
    support_window_count = 0
    local_window_count = 0
    local_seed_residue_count = min(minimum_count, 12)
    for label in sorted(grouped_indices):
        indices = grouped_indices[label]
        support_seed_count = min(len(indices), minimum_count)
        support_step = max(1, support_seed_count // 2)
        support_starts = list(
            range(0, len(indices) - support_seed_count + 1, support_step)
        )
        support_final_start = len(indices) - support_seed_count
        if support_starts[-1] != support_final_start:
            support_starts.append(support_final_start)
        for start in support_starts:
            record_candidate(
                fit_positions(indices[start : start + support_seed_count])
            )
        support_window_count += len(support_starts)

        local_count = min(len(indices), local_seed_residue_count)
        local_step = max(1, local_count // 2)
        local_starts = list(range(0, len(indices) - local_count + 1, local_step))
        local_final_start = len(indices) - local_count
        if local_starts[-1] != local_final_start:
            local_starts.append(local_final_start)
        for start in local_starts:
            record_candidate(fit_positions(indices[start : start + local_count]))
        local_window_count += len(local_starts)

    if not candidates:
        raise EvaluationError(
            "robust receptor alignment retained too few sequence-aligned residues"
        )
    _score, fit, retained, cycles = min(candidates, key=lambda row: row[0])
    per_group = []
    retained_set = set(retained)
    for label in sorted(grouped_indices):
        indices = grouped_indices[label]
        retained_count = sum(index in retained_set for index in indices)
        per_group.append(
            {
                "group_id": label,
                "aligned_residue_count": len(indices),
                "retained_residue_count": retained_count,
                "retained_fraction": retained_count / len(indices),
            }
        )
    return fit, {
        "policy": "sequence-ca-iterative-outlier-rejection/v1",
        "cutoff_angstrom": float(cutoff_angstrom),
        "maximum_cycles": int(maximum_cycles),
        "cycles_completed": cycles,
        "minimum_support_policy": "20-percent-longest-submitted-chain-or-five/v1",
        "minimum_retained_residue_count": minimum_count,
        "coarse_policy": (
            "deterministic-pooled-75-percent-least-trimmed-plus-per-chain-windows/v3"
        ),
        "coarse_retained_counts": coarse_retained_counts,
        "coarse_window_count": support_window_count,
        "local_seed_residue_count": local_seed_residue_count,
        "local_window_count": local_window_count,
        "aligned_residue_count": original_count,
        "retained_residue_count": len(retained),
        "retained_fraction": len(retained) / original_count,
        "per_group": per_group,
    }


def _robust_sequence_superposition(
    reference: Any,
    predicted: Any,
    gemmi: Any,
    *,
    cutoff_angstrom: float = 2.0,
    maximum_cycles: int = 5,
) -> tuple[Any, dict[str, Any]]:
    """Fit a sequence-aligned rigid core while rejecting flexible outliers."""

    reference_positions, predicted_positions = _sequence_aligned_positions(
        reference, predicted, gemmi
    )
    return _robust_position_superposition(
        reference_positions,
        predicted_positions,
        gemmi,
        cutoff_angstrom=cutoff_angstrom,
        maximum_cycles=maximum_cycles,
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Return a deterministic linearly interpolated percentile."""

    if not values:
        raise EvaluationError("cannot summarize an empty receptor displacement set")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _displacement_summary(values: Sequence[float]) -> dict[str, Any]:
    """Summarize post-transform C-alpha displacement without another fit."""

    normalized = [float(value) for value in values]
    if not normalized or any(not math.isfinite(value) or value < 0 for value in normalized):
        raise EvaluationError("receptor displacements must be finite non-negative values")
    return {
        "count": len(normalized),
        "rmsd": math.sqrt(sum(value * value for value in normalized) / len(normalized)),
        "p50": _percentile(normalized, 0.50),
        "p90": _percentile(normalized, 0.90),
        "p95": _percentile(normalized, 0.95),
        "p99": _percentile(normalized, 0.99),
        "max": max(normalized),
    }


def exact_complex_receptor_superposition(
    reference_model: Any,
    predicted_model: Any,
    *,
    expected_chain_sequences: Mapping[str, str],
) -> dict[str, Any]:
    """Align all submitted protein chains through one robust complex-wide fit.

    Chain identity is immutable task input, not inferred from ligand proximity
    or prediction geometry.  Every expected chain must occur exactly once under
    its submitted ID and exact sequence in both models.  Sequence-aligned CA
    pairs from all chains are pooled; independently moving chains and flexible
    domains can then be rejected as geometric outliers from one shared frame.
    """

    try:
        import gemmi
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError("receptor alignment requires Gemmi") from exc
    if (
        not isinstance(expected_chain_sequences, Mapping)
        or not expected_chain_sequences
    ):
        raise EvaluationError("expected receptor chain sequences must be non-empty")
    normalized: dict[str, str] = {}
    for chain_id, sequence in expected_chain_sequences.items():
        if (
            not isinstance(chain_id, str)
            or not chain_id
            or not isinstance(sequence, str)
            or not sequence
            or not sequence.isalpha()
        ):
            raise EvaluationError("expected receptor chain sequences are invalid")
        normalized[chain_id] = sequence.upper()

    def model_chains(model: Any, role: str) -> dict[str, Any]:
        chains: dict[str, Any] = {}
        for chain_id, polymer in _polymer_chains(model, minimum_residues=1):
            if chain_id in normalized:
                if chain_id in chains:
                    raise EvaluationError(
                        f"{role} receptor chain {chain_id} is duplicated"
                    )
                chains[chain_id] = polymer
        missing = sorted(set(normalized) - set(chains))
        if missing:
            raise EvaluationError(
                f"{role} receptor lacks submitted protein chain(s): "
                f"{', '.join(missing)}"
            )
        for chain_id, expected_sequence in normalized.items():
            if _sequence(chains[chain_id], gemmi) != expected_sequence:
                raise EvaluationError(
                    f"{role} receptor chain {chain_id} does not match its "
                    "submitted sequence"
                )
        return chains

    reference_chains = model_chains(reference_model, "reference")
    predicted_chains = model_chains(predicted_model, "predicted")
    reference_positions: list[Any] = []
    predicted_positions: list[Any] = []
    group_labels: list[str] = []
    for chain_id in sorted(normalized):
        chain_reference, chain_predicted = _sequence_aligned_positions(
            reference_chains[chain_id],
            predicted_chains[chain_id],
            gemmi,
            minimum_count=1,
        )
        reference_positions.extend(chain_reference)
        predicted_positions.extend(chain_predicted)
        group_labels.extend([chain_id] * len(chain_reference))
    superposition, robust_audit = _robust_position_superposition(
        reference_positions,
        predicted_positions,
        gemmi,
        group_labels=group_labels,
    )
    post_transform_displacements: list[float] = []
    post_transform_by_chain: dict[str, list[float]] = {
        chain_id: [] for chain_id in sorted(normalized)
    }
    for reference_position, predicted_position, chain_id in zip(
        reference_positions,
        predicted_positions,
        group_labels,
    ):
        transformed = superposition.transform.apply(predicted_position)
        displacement = math.sqrt(
            (transformed.x - reference_position.x) ** 2
            + (transformed.y - reference_position.y) ** 2
            + (transformed.z - reference_position.z) ** 2
        )
        post_transform_displacements.append(displacement)
        post_transform_by_chain[chain_id].append(displacement)
    robust_audit["per_chain"] = [
        {
            "chain_id": row["group_id"],
            "aligned_residue_count": row["aligned_residue_count"],
            "retained_residue_count": row["retained_residue_count"],
            "retained_fraction": row["retained_fraction"],
        }
        for row in robust_audit.pop("per_group")
    ]
    chain_ids = sorted(normalized)
    return {
        "reference_chains": chain_ids,
        "predicted_chains": chain_ids,
        "sequence_similarity": 1.0,
        "receptor_rmsd": float(superposition.rmsd),
        "transform": superposition.transform,
        "chain_selection_policy": "exact-task-complex-robust-core/v1",
        "sequence_binding_policy": "exact-task-chain-id-and-sequence/v1",
        "robust_core": robust_audit,
        "post_transform_ca": {
            "policy": "all-sequence-matched-ca-displacement-without-refit/v1",
            **_displacement_summary(post_transform_displacements),
            "per_chain": [
                {
                    "chain_id": chain_id,
                    **_displacement_summary(post_transform_by_chain[chain_id]),
                }
                for chain_id in sorted(post_transform_by_chain)
            ],
        },
    }


def exact_complex_tm_superposition(
    reference_model: Any,
    predicted_model: Any,
    *,
    expected_chain_sequences: Mapping[str, str],
) -> dict[str, Any]:
    """Fit one full-complex frame by a deterministic, normalized TM objective.

    Correspondence is fixed exclusively by submitted chain ID and exact sequence.
    Geometry never selects or permutes chains.  The returned transform is suitable
    for the complete predicted object, including protein and ligand coordinates.
    """

    try:
        import gemmi
        import numpy
    except (ImportError, ModuleNotFoundError) as exc:
        raise EvaluationError(
            "TM receptor alignment requires Gemmi and NumPy"
        ) from exc
    if (
        not isinstance(expected_chain_sequences, Mapping)
        or not expected_chain_sequences
    ):
        raise EvaluationError("expected receptor chain sequences must be non-empty")
    normalized: dict[str, str] = {}
    for chain_id, sequence in expected_chain_sequences.items():
        if (
            not isinstance(chain_id, str)
            or not chain_id
            or not isinstance(sequence, str)
            or not sequence
            or not sequence.isalpha()
        ):
            raise EvaluationError("expected receptor chain sequences are invalid")
        normalized[chain_id] = sequence.upper()

    def bound_chains(model: Any, role: str) -> dict[str, Any]:
        chains: dict[str, Any] = {}
        for chain_id, polymer in _polymer_chains(model, minimum_residues=1):
            if chain_id in normalized:
                if chain_id in chains:
                    raise EvaluationError(
                        f"{role} receptor chain {chain_id} is duplicated"
                    )
                chains[chain_id] = polymer
        missing = sorted(set(normalized) - set(chains))
        if missing:
            raise EvaluationError(
                f"{role} receptor lacks submitted protein chain(s): "
                f"{', '.join(missing)}"
            )
        for chain_id, expected_sequence in normalized.items():
            if _sequence(chains[chain_id], gemmi) != expected_sequence:
                raise EvaluationError(
                    f"{role} receptor chain {chain_id} does not match its "
                    "submitted sequence"
                )
        return chains

    reference_chains = bound_chains(reference_model, "reference")
    predicted_chains = bound_chains(predicted_model, "predicted")
    reference_rows: list[list[float]] = []
    predicted_rows: list[list[float]] = []
    labels: list[str] = []
    for chain_id in sorted(normalized):
        reference_positions, predicted_positions = _sequence_aligned_positions(
            reference_chains[chain_id],
            predicted_chains[chain_id],
            gemmi,
            minimum_count=1,
        )
        for reference_position, predicted_position in zip(
            reference_positions, predicted_positions
        ):
            reference_rows.append(
                [reference_position.x, reference_position.y, reference_position.z]
            )
            predicted_rows.append(
                [predicted_position.x, predicted_position.y, predicted_position.z]
            )
            labels.append(chain_id)
    if len(reference_rows) < 5:
        raise EvaluationError("fewer than five exact-complex receptor C-alpha atoms")
    reference = numpy.asarray(reference_rows, dtype=float)
    predicted = numpy.asarray(predicted_rows, dtype=float)
    label_array = numpy.asarray(labels)
    length = len(reference)
    d0 = 0.5 if length <= 15 else max(0.5, 1.24 * (length - 15) ** (1 / 3) - 1.8)

    def kabsch(
        indices: Any | None = None, weights: Any | None = None
    ) -> tuple[Any, Any]:
        source = predicted if indices is None else predicted[indices]
        target = reference if indices is None else reference[indices]
        local_weights = (
            numpy.ones(len(source), dtype=float) if weights is None else weights
        )
        local_weights = numpy.asarray(local_weights, dtype=float)
        local_weights = local_weights / local_weights.sum()
        source_center = numpy.sum(source * local_weights[:, None], axis=0)
        target_center = numpy.sum(target * local_weights[:, None], axis=0)
        covariance = (source - source_center).T @ (
            (target - target_center) * local_weights[:, None]
        )
        left, _singular, right = numpy.linalg.svd(covariance)
        rotation = right.T @ left.T
        if numpy.linalg.det(rotation) < 0:
            right[-1] *= -1
            rotation = right.T @ left.T
        return rotation, target_center - rotation @ source_center

    starts: list[tuple[str, Any, Any]] = [
        ("identity", numpy.eye(3), numpy.zeros(3)),
        ("all-ca-kabsch", *kabsch()),
    ]
    window_size = 24
    for chain_id in sorted(normalized):
        indices = numpy.flatnonzero(label_array == chain_id)
        starts.append((f"chain:{chain_id}", *kabsch(indices)))
        count = min(window_size, len(indices))
        step = max(1, count // 2)
        offsets = list(range(0, len(indices) - count + 1, step))
        final_offset = len(indices) - count
        if offsets[-1] != final_offset:
            offsets.append(final_offset)
        for offset in offsets:
            starts.append(
                (
                    f"chain-window:{chain_id}:{offset}:{count}",
                    *kabsch(indices[offset : offset + count]),
                )
            )

    best: tuple[tuple[Any, ...], int, str, Any, Any, Any, int] | None = None
    for seed_index, (seed, rotation, translation) in enumerate(starts):
        iterations = 0
        for iteration in range(30):
            distances = numpy.linalg.norm(
                (rotation @ predicted.T).T + translation - reference, axis=1
            )
            weights = 1.0 / (1.0 + (distances / d0) ** 2) ** 2
            updated_rotation, updated_translation = kabsch(weights=weights)
            iterations = iteration + 1
            converged = bool(
                numpy.max(numpy.abs(updated_rotation - rotation)) < 1e-10
                and numpy.max(numpy.abs(updated_translation - translation)) < 1e-9
            )
            rotation, translation = updated_rotation, updated_translation
            if converged:
                break
        distances = numpy.linalg.norm(
            (rotation @ predicted.T).T + translation - reference, axis=1
        )
        tm_score = float(numpy.mean(1.0 / (1.0 + (distances / d0) ** 2)))
        rmsd = float(numpy.sqrt(numpy.mean(distances**2)))
        key = (
            tm_score,
            float(numpy.mean(distances <= 10.0)),
            -float(numpy.median(distances)),
            -rmsd,
            -seed_index,
        )
        if best is None or key > best[0]:
            best = (key, seed_index, seed, rotation, translation, distances, iterations)
    assert best is not None
    key, seed_index, seed, rotation, translation, distances, iterations = best
    transform = gemmi.Transform()
    transform.mat.fromlist(rotation.tolist())
    transform.vec.fromlist(translation.tolist())

    def coverage(values: Any, cutoff: float) -> tuple[int, float]:
        count = int(numpy.sum(values <= cutoff))
        return count, count / len(values)

    per_chain = []
    for chain_id in sorted(normalized):
        chain_distances = distances[label_array == chain_id]
        within5, fraction5 = coverage(chain_distances, 5.0)
        within10, fraction10 = coverage(chain_distances, 10.0)
        within20, fraction20 = coverage(chain_distances, 20.0)
        per_chain.append(
            {
                "chain_id": chain_id,
                "aligned_residue_count": len(chain_distances),
                "retained_residue_count": within5,
                "retained_fraction": fraction5,
                "within_5_angstrom_count": within5,
                "within_5_angstrom_fraction": fraction5,
                "within_10_angstrom_count": within10,
                "within_10_angstrom_fraction": fraction10,
                "within_20_angstrom_count": within20,
                "within_20_angstrom_fraction": fraction20,
                **_displacement_summary(chain_distances.tolist()),
            }
        )
    within5, fraction5 = coverage(distances, 5.0)
    within10, fraction10 = coverage(distances, 10.0)
    within20, fraction20 = coverage(distances, 20.0)
    tm_score = float(key[0])
    post_transform = {
        "policy": (
            "all-exact-task-sequence-matched-ca-displacement-without-refit/v2"
        ),
        **_displacement_summary(distances.tolist()),
        "within_5_angstrom_count": within5,
        "within_5_angstrom_fraction": fraction5,
        "within_10_angstrom_count": within10,
        "within_10_angstrom_fraction": fraction10,
        "within_20_angstrom_count": within20,
        "within_20_angstrom_fraction": fraction20,
        "per_chain": per_chain,
    }
    return {
        "reference_chains": sorted(normalized),
        "predicted_chains": sorted(normalized),
        "sequence_similarity": 1.0,
        "receptor_tm_score": tm_score,
        "receptor_distance": 1.0 - tm_score,
        "receptor_rmsd": post_transform["rmsd"],
        "transform": transform,
        "chain_selection_policy": "exact-task-complex-global-tm/v1",
        "sequence_binding_policy": "exact-task-chain-id-and-sequence/v1",
        "global_coverage": {
            "policy": "fixed-correspondence-normalized-tm-irls/v1",
            "objective": "mean(1/(1+(ca_displacement/d0)^2))",
            "normalization_policy": "length-normalized-tm-d0-floor-0.5/v1",
            "normalization_length": length,
            "normalization_d0_angstrom": d0,
            "seed_policy": (
                "identity-all-ca-each-chain-and-24-ca-overlapping-windows/v1"
            ),
            "seed_count": len(starts),
            "selected_seed_index": seed_index,
            "selected_seed": seed,
            "maximum_iterations": 30,
            "iterations_completed": iterations,
            "aligned_residue_count": length,
            "retained_residue_count": within5,
            "retained_fraction": fraction5,
            "tm_score": tm_score,
            "per_chain": per_chain,
        },
        "post_transform_ca": post_transform,
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
    return [
        (chain, residue)
        for chain, residue, _altloc, _atoms in _exact_ligand_conformers(
            model, heavy_atoms, component_id
        )
    ]


def _exact_ligand_conformers(
    model: Any, heavy_atoms: int, component_id: str | None = None
) -> list[tuple[str, Any, str, list[Any]]]:
    component = component_id.upper() if component_id else None
    rows: list[tuple[str, Any, str, list[Any]]] = []
    for chain in model:
        for residue in chain:
            if component is not None and residue.name.upper() != component:
                continue
            for altloc in _residue_altloc_keys(residue):
                atoms = _conformer_heavy_atoms(residue, altloc)
                if len(atoms) == heavy_atoms:
                    rows.append((chain.name, residue, altloc, atoms))
    rows.sort(key=lambda row: (row[0], row[1].seqid.num, row[1].name, row[2]))
    return rows


def _partial_reference_ligand_conformers(
    model: Any,
    heavy_atoms: int,
    component_id: str,
    *,
    minimum_observed_heavy_atoms: int | None = None,
) -> list[tuple[str, Any, str, list[Any], int, float]]:
    """Return connected-coverage partial conformers below the expected heavy-atom count."""

    component = component_id.upper()
    minimum_coverage = _partial_reference_coverage_threshold(
        heavy_atoms,
        minimum_observed_heavy_atoms=minimum_observed_heavy_atoms,
    )
    rows: list[tuple[str, Any, str, list[Any], int, float]] = []
    for chain in model:
        for residue in chain:
            if residue.name.upper() != component:
                continue
            for altloc in _residue_altloc_keys(residue):
                atoms = _conformer_heavy_atoms(residue, altloc)
                observed = len(atoms)
                if observed >= heavy_atoms:
                    continue
                coverage = observed / heavy_atoms
                if coverage >= minimum_coverage:
                    rows.append((chain.name, residue, altloc, atoms, observed, coverage))
    rows.sort(key=lambda row: (row[0], row[1].seqid.num, row[1].name, row[2]))
    return rows


def _partial_reference_ligands(
    model: Any,
    heavy_atoms: int,
    component_id: str,
    *,
    minimum_observed_heavy_atoms: int | None = None,
) -> list[tuple[str, Any, int, float]]:
    """Return connected-coverage partial references below the expected heavy-atom count."""

    rows: list[tuple[str, Any, int, float]] = []
    for chain, residue, _altloc, _atoms, observed, coverage in _partial_reference_ligand_conformers(
        model,
        heavy_atoms,
        component_id,
        minimum_observed_heavy_atoms=minimum_observed_heavy_atoms,
    ):
        rows.append((chain, residue, observed, coverage))
    return rows


def _reference_ligand_candidates(
    model: Any,
    heavy_atoms: int,
    component_id: str,
    *,
    minimum_observed_heavy_atoms: int | None = None,
) -> list[tuple[str, Any, str, list[Any], str, int, float]]:
    """Prefer exact conformers; otherwise allow conservative partial conformers only."""

    exact = _exact_ligand_conformers(model, heavy_atoms, component_id)
    if exact:
        return [
            (chain, residue, altloc, atoms, "full", heavy_atoms, 1.0)
            for chain, residue, altloc, atoms in exact
        ]
    return [
        (chain, residue, altloc, atoms, "partial", observed, coverage)
        for chain, residue, altloc, atoms, observed, coverage in _partial_reference_ligand_conformers(
            model,
            heavy_atoms,
            component_id,
            minimum_observed_heavy_atoms=minimum_observed_heavy_atoms,
        )
    ]


def _is_connected_molecule(molecule: Any, Chem: Any) -> bool:
    return len(Chem.GetMolFrags(molecule)) == 1


def _ligand_scoring_audit(
    *,
    heavy_atoms: int,
    observed: int,
    scored: int,
    policy: str,
    minimum_observed_heavy_atoms: int | None = None,
) -> dict[str, Any]:
    audit = {
        "reference_heavy_atoms_expected": heavy_atoms,
        "reference_heavy_atoms_observed": observed,
        "reference_heavy_atoms_scored": scored,
        "reference_coverage": observed / heavy_atoms,
        "ligand_mapping_policy": policy,
    }
    if minimum_observed_heavy_atoms is not None:
        _validate_minimum_reference_heavy_atoms_override(
            heavy_atoms, minimum_observed_heavy_atoms
        )
        audit["reference_heavy_atoms_minimum_observed"] = minimum_observed_heavy_atoms
        audit["released_partial_reference_override_policy"] = (
            RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY
        )
    return audit


def _connectivity_molecule_from_atoms(
    atoms: Sequence[Any], Chem: Any, rdDetermineBonds: Any
) -> Any:
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
    return _finalize_connectivity_molecule(result, Chem)


def _finalize_connectivity_molecule(molecule: Any, Chem: Any) -> Any:
    # Bond order does not affect symmetry for RMSD here; using connectivity-only
    # single bonds avoids differences in aromatic/bond-order annotation between
    # an experimental CCD residue and a method's generic LIG component.
    for bond in molecule.GetBonds():
        bond.SetBondType(Chem.BondType.SINGLE)
        bond.SetIsAromatic(False)
    for atom in molecule.GetAtoms():
        atom.SetIsAromatic(False)
        atom.SetNoImplicit(True)
    return molecule


def _atom_name_key(atom: Any) -> str:
    return atom.name.strip().upper()


def _chem_comp_bond_edges(
    coordinate_path: str | Path, component_id: str, gemmi: Any
) -> frozenset[tuple[str, str]] | None:
    """Return normalized heavy-atom bond endpoints for one mmCIF component, if present."""

    resolved = str(Path(coordinate_path).resolve())
    cache_key = (resolved, component_id.upper())
    if cache_key in _CHEM_COMP_BOND_CACHE:
        return _CHEM_COMP_BOND_CACHE[cache_key]
    path = Path(resolved)
    if path.suffix.lower() != ".cif":
        _CHEM_COMP_BOND_CACHE[cache_key] = None
        return None
    try:
        block = gemmi.cif.read_file(resolved).sole_block()
    except (OSError, ValueError):
        _CHEM_COMP_BOND_CACHE[cache_key] = None
        return None
    bond_table = block.find("_chem_comp_bond.", ["comp_id", "atom_id_1", "atom_id_2"])
    if bond_table is None:
        _CHEM_COMP_BOND_CACHE[cache_key] = None
        return None
    component = component_id.upper()
    edges: set[tuple[str, str]] = set()
    for row in bond_table:
        if str(row[0]).upper() != component:
            continue
        left = str(row[1]).strip().upper()
        right = str(row[2]).strip().upper()
        if not left or not right or left == right:
            continue
        edges.add((min(left, right), max(left, right)))
    result = frozenset(edges) if edges else None
    _CHEM_COMP_BOND_CACHE[cache_key] = result
    return result


def _task_smiles_connectivity_molecule_from_atoms(
    atoms: Sequence[Any],
    ligand_smiles: str,
    heavy_atoms: int,
    Chem: Any,
) -> Any | None:
    """Build a connectivity-only molecule from task SMILES in conformer atom order."""

    if not isinstance(ligand_smiles, str) or not ligand_smiles.strip():
        return None
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        return None
    if len(atoms) != heavy_atoms:
        return None
    source = Chem.MolFromSmiles(ligand_smiles.strip())
    if source is None:
        return None
    source = Chem.RemoveHs(source)
    expected_elements = [atom.GetAtomicNum() for atom in source.GetAtoms()]
    if len(expected_elements) != heavy_atoms:
        return None
    observed_elements = [atom.element.atomic_number for atom in atoms]
    if observed_elements != expected_elements:
        return None
    name_keys = [_atom_name_key(atom) for atom in atoms]
    if len(set(name_keys)) != len(name_keys):
        return None
    molecule = Chem.RWMol()
    conformer = Chem.Conformer(len(atoms))
    for index, atom in enumerate(atoms):
        molecule.AddAtom(Chem.Atom(atom.element.atomic_number))
        conformer.SetAtomPosition(index, (atom.pos.x, atom.pos.y, atom.pos.z))
    for bond in source.GetBonds():
        left = bond.GetBeginAtomIdx()
        right = bond.GetEndAtomIdx()
        molecule.AddBond(left, right, Chem.BondType.SINGLE)
    result = molecule.GetMol()
    result.AddConformer(conformer)
    return _finalize_connectivity_molecule(result, Chem)


def _explicit_connectivity_molecule_from_atoms(
    atoms: Sequence[Any],
    bond_edges: Collection[tuple[str, str]],
    Chem: Any,
    *,
    require_connected: bool,
) -> Any | None:
    """Build a connectivity-only molecule from mmCIF ``_chem_comp_bond`` endpoints."""

    name_to_index: dict[str, int] = {}
    for index, atom in enumerate(atoms):
        key = _atom_name_key(atom)
        if key in name_to_index:
            return None
        name_to_index[key] = index
    applicable_edges: set[tuple[int, int]] = set()
    for left, right in bond_edges:
        if left not in name_to_index or right not in name_to_index:
            continue
        edge = (
            min(name_to_index[left], name_to_index[right]),
            max(name_to_index[left], name_to_index[right]),
        )
        applicable_edges.add(edge)
    if not applicable_edges:
        return None
    molecule = Chem.RWMol()
    conformer = Chem.Conformer(len(atoms))
    for index, atom in enumerate(atoms):
        molecule.AddAtom(Chem.Atom(atom.element.atomic_number))
        conformer.SetAtomPosition(index, (atom.pos.x, atom.pos.y, atom.pos.z))
    for left, right in sorted(applicable_edges):
        molecule.AddBond(left, right, Chem.BondType.SINGLE)
    result = molecule.GetMol()
    result.AddConformer(conformer)
    result = _finalize_connectivity_molecule(result, Chem)
    if require_connected and len(Chem.GetMolFrags(result)) != 1:
        return None
    return result


def _resolve_ligand_mappings(
    *,
    predicted_atoms: Sequence[Any],
    reference_atoms: Sequence[Any],
    coordinate_path_predicted: str | Path,
    coordinate_path_reference: str | Path,
    predicted_component_id: str,
    reference_component_id: str,
    reference_mode: str,
    gemmi: Any,
    Chem: Any,
    rdDetermineBonds: Any,
    ligand_smiles: str | None = None,
    heavy_atoms: int | None = None,
) -> tuple[tuple[tuple[int, ...], ...], str, str] | None:
    """Resolve graph mappings using inferred, explicit, then task-SMILES topology."""

    predicted_inferred = _connectivity_molecule_from_atoms(
        predicted_atoms, Chem, rdDetermineBonds
    )
    reference_inferred = _connectivity_molecule_from_atoms(
        reference_atoms, Chem, rdDetermineBonds
    )
    if reference_mode == "full":
        try:
            mappings = _symmetry_mappings(predicted_inferred, reference_inferred)
        except EvaluationError:
            mappings = None
        else:
            return mappings, LIGAND_MAPPING_POLICY_FULL, TOPOLOGY_SOURCE_INFERRED
    else:
        if not _is_connected_molecule(reference_inferred, Chem):
            mappings = None
        elif not _is_connected_molecule(predicted_inferred, Chem):
            mappings = None
        else:
            try:
                mappings = _safe_partial_subgraph_mappings(
                    predicted_inferred, reference_inferred
                )
            except EvaluationError:
                mappings = None
            else:
                return mappings, LIGAND_MAPPING_POLICY_PARTIAL, TOPOLOGY_SOURCE_INFERRED

    predicted_edges = _chem_comp_bond_edges(
        coordinate_path_predicted, predicted_component_id, gemmi
    )
    reference_edges = _chem_comp_bond_edges(
        coordinate_path_reference, reference_component_id, gemmi
    )
    if predicted_edges is not None and reference_edges is not None:
        predicted_explicit = _explicit_connectivity_molecule_from_atoms(
            predicted_atoms,
            predicted_edges,
            Chem,
            require_connected=True,
        )
        reference_explicit = _explicit_connectivity_molecule_from_atoms(
            reference_atoms,
            reference_edges,
            Chem,
            require_connected=True,
        )
        if predicted_explicit is not None and reference_explicit is not None:
            if reference_mode == "full":
                try:
                    mappings = _symmetry_mappings(predicted_explicit, reference_explicit)
                except EvaluationError:
                    mappings = None
                else:
                    return (
                        mappings,
                        LIGAND_MAPPING_POLICY_FULL_EXPLICIT,
                        TOPOLOGY_SOURCE_EXPLICIT,
                    )
            elif _is_connected_molecule(reference_explicit, Chem) and _is_connected_molecule(
                predicted_explicit, Chem
            ):
                try:
                    mappings = _safe_partial_subgraph_mappings(
                        predicted_explicit, reference_explicit
                    )
                except EvaluationError:
                    mappings = None
                else:
                    return (
                        mappings,
                        LIGAND_MAPPING_POLICY_PARTIAL_EXPLICIT,
                        TOPOLOGY_SOURCE_EXPLICIT,
                    )

    if ligand_smiles and reference_edges is not None and heavy_atoms is not None:
        predicted_task = _task_smiles_connectivity_molecule_from_atoms(
            predicted_atoms,
            ligand_smiles,
            heavy_atoms,
            Chem,
        )
        reference_explicit = _explicit_connectivity_molecule_from_atoms(
            reference_atoms,
            reference_edges,
            Chem,
            require_connected=True,
        )
        if predicted_task is not None and reference_explicit is not None:
            if reference_mode == "full":
                if not _is_connected_molecule(predicted_task, Chem):
                    return None
                if not _is_connected_molecule(reference_explicit, Chem):
                    return None
                try:
                    mappings = _symmetry_mappings(predicted_task, reference_explicit)
                except EvaluationError:
                    return None
                return (
                    mappings,
                    LIGAND_MAPPING_POLICY_FULL_TASK_SMILES,
                    TOPOLOGY_SOURCE_TASK_SMILES,
                )
            if not _is_connected_molecule(reference_explicit, Chem):
                return None
            if not _is_connected_molecule(predicted_task, Chem):
                return None
            try:
                mappings = _safe_partial_subgraph_mappings(
                    predicted_task, reference_explicit
                )
            except EvaluationError:
                return None
            return (
                mappings,
                LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES,
                TOPOLOGY_SOURCE_TASK_SMILES,
            )

    return None


def _connectivity_molecule(residue: Any, Chem: Any, rdDetermineBonds: Any) -> Any:
    return _connectivity_molecule_from_atoms(_heavy_atoms(residue), Chem, rdDetermineBonds)


def _symmetry_mappings(predicted: Any, reference: Any) -> tuple[tuple[int, ...], ...]:
    mappings = reference.GetSubstructMatches(
        predicted, uniquify=False, useChirality=False, maxMatches=100_000
    )
    if not mappings:
        raise EvaluationError("predicted and reference ligand connectivity do not match")
    return mappings


def _partial_subgraph_mappings(
    predicted: Any, reference: Any
) -> tuple[tuple[int, ...], ...]:
    mappings = predicted.GetSubstructMatches(
        reference, uniquify=False, useChirality=False, maxMatches=100_000
    )
    if not mappings:
        raise EvaluationError(
            "partial reference ligand is not a subgraph of the predicted ligand"
        )
    return mappings


def _predicted_neighbor_lists(molecule: Any) -> list[list[int]]:
    neighbors = [[] for _ in range(molecule.GetNumAtoms())]
    for bond in molecule.GetBonds():
        begin = bond.GetBeginAtomIdx()
        end = bond.GetEndAtomIdx()
        neighbors[begin].append(end)
        neighbors[end].append(begin)
    return neighbors


def _is_safe_terminal_omission_mapping(
    predicted: Any, mapping: Sequence[int]
) -> bool:
    """Accept only pendant omissions: each unmatched component has one core attachment."""

    atom_count = predicted.GetNumAtoms()
    if len(mapping) == 0 or len(set(mapping)) != len(mapping):
        return False
    mapped = set(int(index) for index in mapping)
    if any(index < 0 or index >= atom_count for index in mapped):
        return False
    unmatched = set(range(atom_count)) - mapped
    if not unmatched:
        return False

    neighbors = _predicted_neighbor_lists(predicted)
    visited: set[int] = set()
    for start in unmatched:
        if start in visited:
            continue
        component: list[int] = []
        stack = [start]
        while stack:
            node = stack.pop()
            if node in visited or node not in unmatched:
                continue
            visited.add(node)
            component.append(node)
            for neighbor in neighbors[node]:
                if neighbor in unmatched and neighbor not in visited:
                    stack.append(neighbor)

        boundary_bonds: set[tuple[int, int]] = set()
        for node in component:
            for neighbor in neighbors[node]:
                if neighbor in mapped:
                    boundary_bonds.add((min(node, neighbor), max(node, neighbor)))
        if len(boundary_bonds) != 1:
            return False
    return True


def _safe_partial_subgraph_mappings(
    predicted: Any, reference: Any
) -> tuple[tuple[int, ...], ...]:
    mappings = _partial_subgraph_mappings(predicted, reference)
    safe = tuple(
        tuple(int(index) for index in mapping)
        for mapping in mappings
        if _is_safe_terminal_omission_mapping(predicted, mapping)
    )
    if not safe:
        raise EvaluationError(
            "partial reference mapping would omit non-terminal or bridging atoms"
        )
    return safe


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


def _partial_mapped_rmsd(
    predicted: Any, reference: Any, mappings: Any, numpy: Any
) -> tuple[float, tuple[int, ...]]:
    best = math.inf
    best_mapping: tuple[int, ...] | None = None
    for mapping in mappings:
        aligned_predicted = predicted[list(mapping)]
        delta = aligned_predicted - reference
        value = float(numpy.sqrt(numpy.sum(delta * delta) / len(mapping)))
        if value < best:
            best = value
            best_mapping = tuple(int(index) for index in mapping)
    if best_mapping is None:
        raise EvaluationError("no partial ligand subgraph mapping could be scored")
    return best, best_mapping


def _reference_order(predicted: Any, mapping: tuple[int, ...], numpy: Any) -> Any:
    """Reorder predicted coordinates so every model shares reference atom order."""

    ordered = numpy.empty_like(predicted)
    for predicted_index, reference_index in enumerate(mapping):
        ordered[reference_index] = predicted[predicted_index]
    return ordered


def _partial_reference_order(predicted: Any, mapping: tuple[int, ...], numpy: Any) -> Any:
    """Return transformed predicted coordinates for each observed reference atom."""

    return predicted[numpy.array(mapping, dtype=int)]


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
    ligand_smiles: str | None = None,
    ligand_order_policy: str | None = None,
    minimum_reference_heavy_atoms: int | None = None,
) -> dict[str, Any]:
    """Evaluate one predicted complex against one released reference assembly."""

    if not isinstance(component_id, str) or not component_id.strip():
        raise EvaluationError("component_id must be non-empty")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise EvaluationError("heavy_atoms must be a positive integer")
    if minimum_reference_heavy_atoms is not None:
        _validate_minimum_reference_heavy_atoms_override(
            heavy_atoms, minimum_reference_heavy_atoms
        )
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

    reference_ligands = _reference_ligand_candidates(
        reference_model,
        heavy_atoms,
        component_id,
        minimum_observed_heavy_atoms=minimum_reference_heavy_atoms,
    )
    predicted_ligands = _exact_ligand_conformers(prediction_model, heavy_atoms)
    reference_polymers = _polymer_chains(reference_model)
    predicted_polymers = _polymer_chains(prediction_model)
    if not reference_ligands:
        raise EvaluationError(f"reference contains no {component_id} ligand with {heavy_atoms} atoms")
    if not predicted_ligands:
        raise EvaluationError(f"prediction contains no ligand with {heavy_atoms} heavy atoms")
    if not reference_polymers or not predicted_polymers:
        raise EvaluationError("reference and prediction must both contain a receptor polymer")

    best: dict[str, Any] | None = None
    mapping_cache: dict[tuple[Any, ...], Any] = {}
    reference_path = Path(reference_path).resolve()
    prediction_path = Path(prediction_path).resolve()
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
            for (
                reference_ligand_chain,
                reference_ligand,
                reference_altloc,
                reference_atoms,
                reference_mode,
                observed_count,
                _reference_coverage,
            ) in reference_ligands:
                reference_coordinates = _coordinates_from_atoms(reference_atoms, numpy)
                for (
                    predicted_ligand_chain,
                    predicted_ligand,
                    predicted_altloc,
                    predicted_atoms,
                ) in predicted_ligands:
                    key = (
                        str(prediction_path),
                        predicted_ligand.name.upper(),
                        predicted_altloc,
                        str(reference_path),
                        reference_ligand.name.upper(),
                        reference_altloc,
                        reference_mode,
                    )
                    cached = mapping_cache.get(key)
                    if cached is None:
                        resolved = _resolve_ligand_mappings(
                            predicted_atoms=predicted_atoms,
                            reference_atoms=reference_atoms,
                            coordinate_path_predicted=prediction_path,
                            coordinate_path_reference=reference_path,
                            predicted_component_id=predicted_ligand.name,
                            reference_component_id=reference_ligand.name,
                            reference_mode=reference_mode,
                            gemmi=gemmi,
                            Chem=Chem,
                            rdDetermineBonds=rdDetermineBonds,
                            ligand_smiles=ligand_smiles,
                            heavy_atoms=heavy_atoms,
                        )
                        if resolved is None:
                            mapping_cache[key] = False
                            continue
                        mappings, mapping_policy, topology_source = resolved
                        mapping_cache[key] = (mappings, mapping_policy, topology_source)
                    elif cached is False:
                        continue
                    else:
                        mappings, mapping_policy, topology_source = cached
                    predicted_coordinates = _apply_transform(
                        _coordinates_from_atoms(predicted_atoms, numpy),
                        transform,
                        gemmi,
                        numpy,
                    )
                    if reference_mode == "full":
                        rmsd, symmetry_mapping = _mapped_rmsd(
                            predicted_coordinates,
                            reference_coordinates,
                            mappings,
                            numpy,
                        )
                        coordinates_reference_order = _reference_order(
                            predicted_coordinates, symmetry_mapping, numpy
                        )
                        scored_count = heavy_atoms
                    else:
                        rmsd, symmetry_mapping = _partial_mapped_rmsd(
                            predicted_coordinates,
                            reference_coordinates,
                            mappings,
                            numpy,
                        )
                        coordinates_reference_order = _partial_reference_order(
                            predicted_coordinates, symmetry_mapping, numpy
                        )
                        scored_count = observed_count
                    if best is None or rmsd < best["rmsd"]:
                        plddt_values = [float(atom.b_iso) for atom in predicted_atoms]
                        candidate = {
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
                            "predicted_ligand_coordinates_reference_order": (
                                coordinates_reference_order.tolist()
                            ),
                            "reference_ligand_coordinates": reference_coordinates.tolist(),
                            "symmetry_mapping": list(symmetry_mapping),
                            "predicted_ligand_atoms": [
                                {"name": atom.name, "element": atom.element.name}
                                for atom in predicted_atoms
                            ],
                            "reference_ligand_atoms": [
                                {"name": atom.name, "element": atom.element.name}
                                for atom in reference_atoms
                            ],
                            **_ligand_scoring_audit(
                                heavy_atoms=heavy_atoms,
                                observed=observed_count,
                                scored=scored_count,
                                policy=mapping_policy,
                                minimum_observed_heavy_atoms=minimum_reference_heavy_atoms,
                            ),
                            "ligand_topology_source": topology_source,
                        }
                        if ligand_order_policy:
                            candidate["ligand_order_policy"] = ligand_order_policy
                        if (
                            topology_source == TOPOLOGY_SOURCE_TASK_SMILES
                            and isinstance(ligand_smiles, str)
                            and ligand_smiles.strip()
                        ):
                            candidate["task_smiles_sha256"] = hashlib.sha256(
                                ligand_smiles.strip().encode("utf-8")
                            ).hexdigest()
                        if reference_altloc:
                            candidate["reference_ligand_altloc"] = reference_altloc
                        if predicted_altloc:
                            candidate["predicted_ligand_altloc"] = predicted_altloc
                        candidate["reference_pocket_pdb"] = _reference_pocket_pdb_from_model(
                            reference_model,
                            reference_coordinates,
                        )
                        best = candidate
    if best is None:
        raise EvaluationError("no compatible receptor/ligand mapping could be evaluated")
    return best


def _reference_model_chain(model: Any, chain_name: str) -> Any:
    for chain in model:
        if chain.name == chain_name:
            return chain
    raise EvaluationError(f"reference is missing chain {chain_name}")


def _residue_within_radius(
    residue: Any,
    anchor_coordinates: Any,
    numpy: Any,
    radius_sq: float,
) -> bool:
    for atom in _heavy_atoms(residue):
        delta = anchor_coordinates - numpy.array(
            [atom.pos.x, atom.pos.y, atom.pos.z], dtype=float
        )
        if float(numpy.min(numpy.sum(delta * delta, axis=1))) <= radius_sq:
            return True
    return False


def _reference_pocket_pdb_from_model(
    model: Any,
    anchor_coordinates: Any,
    *,
    radius_angstrom: float = REFERENCE_POCKET_RADIUS_ANGSTROM,
) -> str:
    """Extract bounded pocket ATOM records from an in-memory reference model."""

    _, numpy = _structure_dependencies()
    anchors = numpy.asarray(anchor_coordinates, dtype=float)
    if anchors.ndim != 2 or anchors.shape[1] != 3 or not anchors.size:
        raise EvaluationError("anchor coordinates must be a non-empty Nx3 array")
    radius_sq = float(radius_angstrom) ** 2

    contacting_chain_ids: list[str] = []
    for chain in model:
        polymer = chain.get_polymer()
        if len(polymer) < 1:
            continue
        if any(
            _residue_within_radius(residue, anchors, numpy, radius_sq)
            for residue in polymer
        ):
            contacting_chain_ids.append(chain.name)
    if not contacting_chain_ids:
        raise EvaluationError("reference pocket export is empty")

    contacting_chain_ids.sort()
    chain_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    if len(contacting_chain_ids) > len(chain_alphabet):
        raise EvaluationError("too many contacting chains for pocket export")
    export_chain_by_source = {
        source: chain_alphabet[index] for index, source in enumerate(contacting_chain_ids)
    }

    lines: list[str] = []
    serial = 0
    for source_chain_id in contacting_chain_ids:
        export_chain_id = export_chain_by_source[source_chain_id]
        for residue in _reference_model_chain(model, source_chain_id).get_polymer():
            heavy_atoms = _heavy_atoms(residue)
            if not heavy_atoms:
                continue
            if not _residue_within_radius(residue, anchors, numpy, radius_sq):
                continue
            for atom in heavy_atoms:
                serial += 1
                lines.append(
                    _reference_pocket_atom_line(serial, atom, residue, export_chain_id)
                )
    if not lines:
        raise EvaluationError("reference pocket export is empty")
    return "\n".join(lines) + "\nEND\n"


def _reference_pocket_atom_line(
    serial: int,
    atom: Any,
    residue: Any,
    chain_name: str,
) -> str:
    name = re.sub(r"[^A-Za-z0-9'\"*]", "", atom.name)[:4] or atom.element.name[:2]
    residue_name = re.sub(r"[^A-Za-z0-9]", "", residue.name)[:3] or "UNK"
    return (
        f"ATOM  {serial:5d} {name:<4s} {residue_name:>3s} {chain_name[:1]:1s}"
        f"{int(residue.seqid.num):4d}    {atom.pos.x:8.3f}{atom.pos.y:8.3f}{atom.pos.z:8.3f}"
        f"  1.00{float(atom.b_iso):6.2f}          {atom.element.name[:2]:>2s}"
    )


def extract_reference_pocket_pdb(
    reference_path: str | Path,
    score: Mapping[str, Any],
    *,
    radius_angstrom: float = REFERENCE_POCKET_RADIUS_ANGSTROM,
) -> str:
    """Extract bounded reference-receptor pocket ATOM records near the matched ligand."""

    raw_coordinates = score.get("reference_ligand_coordinates")
    if not isinstance(raw_coordinates, list) or not raw_coordinates:
        raise EvaluationError("evaluator result lacks reference_ligand_coordinates")
    anchor_coordinates = []
    for raw_point in raw_coordinates:
        if not isinstance(raw_point, list) or len(raw_point) != 3:
            raise EvaluationError("reference_ligand_coordinates entry is invalid")
        normalized = []
        for value in raw_point:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise EvaluationError("reference_ligand_coordinates must be finite")
            normalized.append(float(value))
        anchor_coordinates.append(normalized)
    gemmi, numpy = _structure_dependencies()
    try:
        reference = gemmi.read_structure(str(reference_path))
        reference.setup_entities()
        model = reference[0]
    except Exception as exc:
        raise EvaluationError("could not parse reference coordinates for pocket export") from exc
    return _reference_pocket_pdb_from_model(
        model,
        numpy.array(anchor_coordinates, dtype=float),
        radius_angstrom=radius_angstrom,
    )


__all__ = [
    "EVALUATOR_VERSION",
    "EvaluationError",
    "LIGAND_MAPPING_POLICY_FULL",
    "LIGAND_MAPPING_POLICY_FULL_EXPLICIT",
    "LIGAND_MAPPING_POLICY_FULL_TASK_SMILES",
    "LIGAND_MAPPING_POLICY_PARTIAL",
    "LIGAND_MAPPING_POLICY_PARTIAL_EXPLICIT",
    "LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES",
    "PARTIAL_REFERENCE_COVERAGE_MIN",
    "RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY",
    "RELEASED_PARTIAL_REFERENCE_OVERRIDES",
    "released_partial_reference_override_for_item",
    "TOPOLOGY_SOURCE_EXPLICIT",
    "TOPOLOGY_SOURCE_INFERRED",
    "TOPOLOGY_SOURCE_TASK_SMILES",
    "REFERENCE_POCKET_RADIUS_ANGSTROM",
    "best_receptor_superposition",
    "evaluate_ligand_pose",
    "extract_reference_pocket_pdb",
    "_reference_pocket_pdb_from_model",
    "_chem_comp_bond_edges",
    "_explicit_connectivity_molecule_from_atoms",
    "_resolve_ligand_mappings",
    "_task_smiles_connectivity_molecule_from_atoms",
]
