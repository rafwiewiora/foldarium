from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from foldarium_pipeline import interactions as interactions_module
from foldarium_pipeline.interactions import (
    INTERACTION_POLICY,
    INTERACTION_TYPES,
    InteractionFingerprintError,
    _coordinates,
    summarize_ifp,
)


class InteractionSummaryTests(unittest.TestCase):
    def test_counts_unique_residue_interaction_bits_and_reports_types(self) -> None:
        ifp = {
            ("LIG1.X", "ASP10.A"): {
                "ImplicitHBDonor": ({"distance": 2.8}, {"distance": 3.0}),
                "VdWContact": ({"distance": 3.4},),
            },
            ("LIG1.X", "PHE20.A"): {
                "PiStacking": ({"distance": 4.0},),
                "Hydrophobic": ({"distance": 3.7},),
            },
        }

        summary = summarize_ifp(ifp)

        # Multiple atom-level H-bond occurrences are one boolean fingerprint
        # bit for the same residue and interaction type.
        self.assertEqual(summary["count"], 4)
        self.assertEqual(summary["interacting_residue_count"], 2)
        self.assertEqual(
            summary["by_type"],
            {
                "Hydrophobic": 1,
                "ImplicitHBDonor": 1,
                "PiStacking": 1,
                "VdWContact": 1,
            },
        )
        self.assertEqual(
            summary["residues"],
            [
                {
                    "id": "ASP10.A",
                    "types": ["ImplicitHBDonor", "VdWContact"],
                },
                {
                    "id": "PHE20.A",
                    "types": ["Hydrophobic", "PiStacking"],
                },
            ],
        )

    def test_ignores_empty_metadata_and_empty_residue_rows(self) -> None:
        self.assertEqual(
            summarize_ifp({("LIG1.X", "ALA1.A"): {"Hydrophobic": ()}}),
            {
                "count": 0,
                "interacting_residue_count": 0,
                "by_type": {},
                "residues": [],
            },
        )

    def test_rejects_an_unconfigured_interaction(self) -> None:
        with self.assertRaisesRegex(
            InteractionFingerprintError, "unconfigured interaction"
        ):
            summarize_ifp({("LIG1.X", "ALA1.A"): {"WaterBridge": ({},)}})

    def test_rejects_bad_coordinates_before_chemistry_work(self) -> None:
        for value in ([], [[1.0, 2.0]], [[1.0, 2.0, float("nan")]], [[True, 2, 3]]):
            with self.subTest(value=value), self.assertRaises(InteractionFingerprintError):
                _coordinates(value)

    def test_contract_is_fixed_and_heavy_atom_aware(self) -> None:
        self.assertEqual(
            INTERACTION_POLICY, "prolif-heavy-atom-unique-residue-type/v1"
        )
        self.assertIn("ImplicitHBAcceptor", INTERACTION_TYPES)
        self.assertIn("ImplicitHBDonor", INTERACTION_TYPES)
        self.assertNotIn("HBAcceptor", INTERACTION_TYPES)
        self.assertNotIn("HBDonor", INTERACTION_TYPES)

    def test_calculator_builds_exact_pose_molecules_and_returns_provenance(self) -> None:
        class FakeMol:
            def __init__(self, atoms: int) -> None:
                self.atoms = atoms
                self.conformers = []

            def GetNumAtoms(self):
                return self.atoms

            def RemoveAllConformers(self):
                self.conformers.clear()

            def AddConformer(self, conformer, assignId=False):
                self.conformers.append((conformer, assignId))

        class FakeConformer:
            def __init__(self, atoms: int) -> None:
                self.positions = [None] * atoms

            def SetAtomPosition(self, index, point):
                self.positions[index] = point

        ligand = FakeMol(2)
        protein = FakeMol(20)
        fake_chem = SimpleNamespace(
            MolFromSmiles=lambda _smiles: ligand,
            RemoveHs=lambda mol: mol,
            Conformer=FakeConformer,
            MolFromPDBFile=lambda *_args, **_kwargs: protein,
        )

        molecule_calls = []

        class FakeMolecule:
            @classmethod
            def from_rdkit(cls, mol, **kwargs):
                molecule_calls.append((mol, kwargs))
                return (mol, kwargs)

        class FakeFingerprint:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def generate(self, _ligand, _protein, *, metadata):
                self.metadata = metadata
                return {
                    ("LIG1.X", "TYR7.A"): {
                        "Hydrophobic": ({"distance": 3.5},),
                        "VdWContact": ({"distance": 3.2},),
                    }
                }

        fake_prolif = SimpleNamespace(Molecule=FakeMolecule, Fingerprint=FakeFingerprint)
        with TemporaryDirectory() as temporary:
            protein_path = Path(temporary, "protein.pdb")
            protein_path.write_text("END\n", encoding="utf-8")
            with (
                patch.object(
                    interactions_module,
                    "_installed_versions",
                    return_value={"prolif": "2.2.0", "rdkit": "2026.3.4"},
                ),
                patch.object(
                    interactions_module,
                    "_dependencies",
                    return_value=(fake_prolif, fake_chem, lambda x, y, z: (x, y, z)),
                ),
            ):
                summary = interactions_module.calculate_interaction_summary(
                    protein_path,
                    "CO",
                    [[1, 2, 3], [4, 5, 6]],
                )

        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["engine"], "prolif")
        self.assertEqual(summary["engine_version"], "2.2.0")
        self.assertEqual(summary["rdkit_version"], "2026.3.4")
        self.assertEqual(summary["policy"], INTERACTION_POLICY)
        self.assertEqual(ligand.conformers[0][0].positions, [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])
        self.assertEqual(molecule_calls[0][1], {"resname": "LIG", "resnumber": 1, "chain": "X"})
        self.assertEqual(molecule_calls[1], (protein, {}))

    def test_pose_file_adapter_passes_exact_coordinates_to_calculator(self) -> None:
        class FakePosition:
            def __init__(self, x, y, z):
                self.x, self.y, self.z = x, y, z

        class FakeConformer:
            def GetAtomPosition(self, index):
                return FakePosition(index + 1, index + 2, index + 3)

        class FakePose:
            def GetNumConformers(self):
                return 1

            def GetConformer(self):
                return FakeConformer()

            def GetNumAtoms(self):
                return 2

        fake_chem = SimpleNamespace(MolFromPDBFile=lambda *_args, **_kwargs: FakePose())
        with TemporaryDirectory() as temporary:
            protein_path = Path(temporary, "protein.pdb")
            ligand_path = Path(temporary, "pose.pdb")
            protein_path.write_text("END\n", encoding="utf-8")
            ligand_path.write_text("END\n", encoding="utf-8")
            with (
                patch.object(
                    interactions_module,
                    "_dependencies",
                    return_value=(object(), fake_chem, object()),
                ),
                patch.object(
                    interactions_module,
                    "calculate_interaction_summary",
                    return_value={"count": 3},
                ) as calculate,
            ):
                summary = interactions_module.calculate_interaction_summary_from_pose(
                    protein_path, ligand_path, "CC"
                )

        self.assertEqual(summary, {"count": 3})
        calculate.assert_called_once_with(
            protein_path, "CC", [(1.0, 2.0, 3.0), (2.0, 3.0, 4.0)]
        )


if __name__ == "__main__":
    unittest.main()
