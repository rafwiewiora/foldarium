from __future__ import annotations

import importlib.util
import unittest

from foldarium_pipeline.ligand_normalization import (
    LigandNormalizationError,
    remove_all_hydrogen_atoms,
)


HAS_RDKIT = importlib.util.find_spec("rdkit") is not None


@unittest.skipUnless(HAS_RDKIT, "RDKit is an optional evaluation dependency")
class LigandNormalizationTests(unittest.TestCase):
    SMILES = r"[H]/N=C(/NCCC[C@@H](C(=O)O)N)\NP(=O)(O)O"

    def test_removes_explicit_h_without_reordering_the_heavy_graph(self) -> None:
        from rdkit import Chem

        source = Chem.MolFromSmiles(self.SMILES)
        expected_elements = [
            atom.GetAtomicNum()
            for atom in source.GetAtoms()
            if atom.GetAtomicNum() != 1
        ]
        normalized, removed = remove_all_hydrogen_atoms(source, Chem)

        self.assertEqual(removed, 1)
        self.assertEqual(
            [atom.GetAtomicNum() for atom in normalized.GetAtoms()],
            expected_elements,
        )

    def test_fails_closed_when_the_builder_changes_the_heavy_graph(self) -> None:
        from rdkit import Chem

        source = Chem.MolFromSmiles(self.SMILES)

        class BrokenBuilder:
            def __init__(self, molecule):
                self.builder = Chem.RWMol(molecule)

            def RemoveAtom(self, index):
                self.builder.RemoveAtom(index)

            def GetMol(self):
                return Chem.MolFromSmiles("CC")

        class BrokenChem:
            RWMol = BrokenBuilder

        with self.assertRaisesRegex(
            LigandNormalizationError,
            "changed heavy-atom order or connectivity",
        ):
            remove_all_hydrogen_atoms(source, BrokenChem)


if __name__ == "__main__":
    unittest.main()
