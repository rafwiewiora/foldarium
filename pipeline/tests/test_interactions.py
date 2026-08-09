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
    PROTEIN_PARSE_POLICY,
    PROTEIN_STANDARDIZATION_POLICY,
    InteractionFingerprintError,
    _coordinates,
    _hbond_summary,
    summarize_ifp,
)


class InteractionSummaryTests(unittest.TestCase):
    def test_counts_unique_residue_interaction_bits_and_reports_types(self) -> None:
        ifp = {
            ("LIG1.X", "ASP10.A"): {
                "ImplicitHBAcceptor": ({"distance": 3.4}, {"distance": 3.5}),
                "ImplicitHBDonor": ({"distance": 3.2},),
            },
            ("LIG1.X", "PHE20.A"): {
                "ImplicitHBDonor": ({"distance": 3.3},),
            },
        }

        summary = summarize_ifp(ifp)

        # Multiple atom-level occurrences and donor/acceptor directions collapse
        # to one public count for the same protein residue.
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["interacting_residue_count"], 2)
        self.assertEqual(
            summary["by_type"],
            {"ImplicitHBAcceptor": 1, "ImplicitHBDonor": 2},
        )
        self.assertEqual(
            summary["residues"],
            [
                {
                    "id": "ASP10.A",
                    "types": ["ImplicitHBAcceptor", "ImplicitHBDonor"],
                },
                {
                    "id": "PHE20.A",
                    "types": ["ImplicitHBDonor"],
                },
            ],
        )

    def test_ignores_empty_metadata_and_empty_residue_rows(self) -> None:
        self.assertEqual(
            summarize_ifp({("LIG1.X", "ALA1.A"): {"ImplicitHBDonor": ()}}),
            {
                "count": 0,
                "interacting_residue_count": 0,
                "by_type": {},
                "residues": [],
            },
        )

    def test_rejects_an_unconfigured_interaction(self) -> None:
        with self.assertRaisesRegex(InteractionFingerprintError, "unconfigured interaction"):
            summarize_ifp({("LIG1.X", "ALA1.A"): {"Hydrophobic": ({},)}})

    def test_rejects_bad_coordinates_before_chemistry_work(self) -> None:
        for value in ([], [[1.0, 2.0]], [[1.0, 2.0, float("nan")]], [[True, 2, 3]]):
            with self.subTest(value=value), self.assertRaises(InteractionFingerprintError):
                _coordinates(value)

    def test_contract_is_fixed_and_heavy_atom_aware(self) -> None:
        self.assertEqual(
            INTERACTION_POLICY,
            "prolif-implicit-hbond-unique-protein-residue/v1",
        )
        self.assertEqual(
            PROTEIN_STANDARDIZATION_POLICY,
            "prolif-molecule-standardizer-standard-amino-acids/v1",
        )
        self.assertEqual(
            PROTEIN_PARSE_POLICY,
            "rdkit-pdb-unsanitized-proximity-connectivity/v1",
        )
        self.assertEqual(
            INTERACTION_TYPES,
            ("ImplicitHBAcceptor", "ImplicitHBDonor"),
        )

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
        protein.GetNumConformers = lambda: 1
        fake_chem = SimpleNamespace(
            MolFromSmiles=lambda _smiles: ligand,
            RemoveHs=lambda mol: mol,
            Conformer=FakeConformer,
            MolFromPDBBlock=lambda *_args, **_kwargs: protein,
        )

        with TemporaryDirectory() as temporary:
            protein_path = Path(temporary, "protein.pdb")
            protein_path.write_text("END\n", encoding="utf-8")
            with (
                patch.object(
                    interactions_module,
                    "_installed_versions",
                    return_value={
                        "prolif": "2.2.0",
                        "rdkit": "2026.3.4",
                    },
                ),
                patch.object(
                    interactions_module,
                    "_dependencies",
                    return_value=(object(), fake_chem, lambda x, y, z: (x, y, z)),
                ),
                patch.object(
                    interactions_module,
                    "_hbond_summary",
                    return_value={
                        "count": 1,
                        "interacting_residue_count": 1,
                        "by_type": {"ImplicitHBDonor": 1},
                        "residues": [
                            {"id": "TYR7.A", "types": ["ImplicitHBDonor"]}
                        ],
                    },
                ),
            ):
                summary = interactions_module.calculate_interaction_summary(
                    protein_path,
                    "CO",
                    [[1, 2, 3], [4, 5, 6]],
                )

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["engine"], "prolif")
        self.assertEqual(summary["engine_version"], "2.2.0")
        self.assertEqual(summary["rdkit_version"], "2026.3.4")
        self.assertEqual(summary["policy"], INTERACTION_POLICY)
        self.assertTrue(summary["implicit_hydrogens"])
        self.assertEqual(summary["geometry_checks"], "prolif-defaults")
        self.assertFalse(summary["include_water"])
        self.assertEqual(ligand.conformers[0][0].positions, [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])

    def test_hbond_runner_standardizes_protein_and_uses_implicit_geometry_checked_detectors(self) -> None:
        calls = {}
        protein = object()
        ligand_molecule = object()
        ifp = {
            ("LIG1.X", "ASP10.A"): {"ImplicitHBAcceptor": ({},)},
        }

        class FakeStandardizer:
            def __call__(self, path):
                calls["protein_path"] = path
                return protein

        class FakeFingerprint:
            def __init__(self, **kwargs):
                calls["fingerprint"] = kwargs

            def generate(self, ligand, receptor, metadata=False):
                calls["generate"] = (ligand, receptor, metadata)
                return ifp

        fake_prolif = SimpleNamespace(
            io=SimpleNamespace(MoleculeStandardizer=FakeStandardizer),
            Molecule=SimpleNamespace(
                from_rdkit=lambda ligand, **kwargs: calls.setdefault(
                    "ligand", (ligand, kwargs)
                ) and ligand_molecule
            ),
            Fingerprint=FakeFingerprint,
        )
        raw_protein = object()
        fake_chem = SimpleNamespace(
            MolFromPDBFile=lambda path, **kwargs: calls.setdefault(
                "pdb", (path, kwargs)
            ) and raw_protein
        )
        path = Path("protein.pdb")
        ligand = object()

        summary = _hbond_summary(fake_prolif, fake_chem, path, ligand)

        self.assertEqual(summary["count"], 1)
        self.assertEqual(calls["protein_path"], raw_protein)
        self.assertEqual(
            calls["pdb"],
            (
                "protein.pdb",
                {
                    "sanitize": False,
                    "removeHs": False,
                    "proximityBonding": True,
                },
            ),
        )
        self.assertEqual(calls["ligand"][1], {"resname": "LIG", "resnumber": 1, "chain": "X"})
        self.assertEqual(
            calls["fingerprint"],
            {
                "interactions": ["HBAcceptor", "HBDonor"],
                "count": False,
                "vicinity_cutoff": 6.0,
                "implicit_hydrogens": True,
            },
        )
        self.assertEqual(calls["generate"], (ligand_molecule, protein, True))

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
