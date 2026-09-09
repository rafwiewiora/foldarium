"""Shared heavy-atom normalization for authoritative task-SMILES graphs."""

from __future__ import annotations

from typing import Any


LIGAND_SMILES_HEAVY_ATOM_POLICY = (
    "rdkit-delete-all-atomic-number-1-preserve-order-and-connectivity/v1"
)


class LigandNormalizationError(ValueError):
    """Raised when explicit-H removal changes the authoritative heavy graph."""


def remove_all_hydrogen_atoms(molecule: Any, Chem: Any) -> tuple[Any, int]:
    """Delete atomic-number-1 atoms while preserving heavy order and connectivity."""

    source_heavy_indices = [
        atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetAtomicNum() != 1
    ]
    source_hydrogen_count = molecule.GetNumAtoms() - len(source_heavy_indices)
    if not source_hydrogen_count:
        return molecule, 0
    source_elements = [
        molecule.GetAtomWithIdx(index).GetAtomicNum()
        for index in source_heavy_indices
    ]
    source_to_heavy = {
        source_index: heavy_index
        for heavy_index, source_index in enumerate(source_heavy_indices)
    }
    source_edges = {
        tuple(
            sorted(
                (
                    source_to_heavy[bond.GetBeginAtomIdx()],
                    source_to_heavy[bond.GetEndAtomIdx()],
                )
            )
        )
        for bond in molecule.GetBonds()
        if bond.GetBeginAtomIdx() in source_to_heavy
        and bond.GetEndAtomIdx() in source_to_heavy
    }

    builder = Chem.RWMol(molecule)
    for atom_index in reversed(
        [atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetAtomicNum() == 1]
    ):
        builder.RemoveAtom(atom_index)
    heavy_molecule = builder.GetMol()
    observed_elements = [
        atom.GetAtomicNum() for atom in heavy_molecule.GetAtoms()
    ]
    observed_edges = {
        tuple(sorted((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())))
        for bond in heavy_molecule.GetBonds()
    }
    if (
        observed_elements != source_elements
        or observed_edges != source_edges
        or any(atom.GetAtomicNum() == 1 for atom in heavy_molecule.GetAtoms())
    ):
        raise LigandNormalizationError(
            "explicit-H removal changed heavy-atom order or connectivity"
        )
    return heavy_molecule, source_hydrogen_count


__all__ = [
    "LIGAND_SMILES_HEAVY_ATOM_POLICY",
    "LigandNormalizationError",
    "remove_all_hydrogen_atoms",
]
