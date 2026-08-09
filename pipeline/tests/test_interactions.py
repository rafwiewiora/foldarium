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
    VDW_RADII_PRESET,
    InteractionFingerprintError,
    _coordinates,
    _protein_vicinity_records,
    _vdw_summary,
    summarize_ifp,
)


class InteractionSummaryTests(unittest.TestCase):
    def test_counts_unique_residue_interaction_bits_and_reports_types(self) -> None:
        ifp = {
            ("LIG1.X", "ASP10.A"): {
                "VdWContact": ({"distance": 3.4}, {"distance": 3.5}),
            },
            ("LIG1.X", "PHE20.A"): {
                "VdWContact": ({"distance": 3.7},),
            },
        }

        summary = summarize_ifp(ifp)

        # Multiple atom-level contacts are one boolean fingerprint bit for the
        # same residue and interaction type.
        self.assertEqual(summary["count"], 2)
        self.assertEqual(summary["interacting_residue_count"], 2)
        self.assertEqual(
            summary["by_type"],
            {"VdWContact": 2},
        )
        self.assertEqual(
            summary["residues"],
            [
                {
                    "id": "ASP10.A",
                    "types": ["VdWContact"],
                },
                {
                    "id": "PHE20.A",
                    "types": ["VdWContact"],
                },
            ],
        )

    def test_ignores_empty_metadata_and_empty_residue_rows(self) -> None:
        self.assertEqual(
            summarize_ifp({("LIG1.X", "ALA1.A"): {"VdWContact": ()}}),
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
            "prolif-vdwcontact-distance-unique-residue-pdb/v1",
        )
        self.assertEqual(VDW_RADII_PRESET, "rdkit")
        self.assertEqual(INTERACTION_TYPES, ("VdWContact",))

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
                    "_protein_vicinity_records",
                    return_value=[(("A", "   7", "", "TYR"), "C", (1.0, 2.0, 3.0))],
                ),
                patch.object(
                    interactions_module,
                    "_vdw_summary",
                    return_value={
                        "count": 1,
                        "interacting_residue_count": 1,
                        "by_type": {"VdWContact": 1},
                        "residues": [{"id": "TYR7.A", "types": ["VdWContact"]}],
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
        self.assertEqual(summary["vdw_radii_preset"], "rdkit")
        self.assertEqual(ligand.conformers[0][0].positions, [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0)])

    def test_vicinity_prefilter_keeps_complete_nearby_residues(self) -> None:
        def atom_line(serial, atom_name, residue, residue_number, x):
            return (
                f"ATOM  {serial:5d} {atom_name:<4} {residue:>3} A{residue_number:4d}    "
                f"{x:8.3f}{0.0:8.3f}{0.0:8.3f}  1.00 20.00           C"
            )

        with TemporaryDirectory() as temporary:
            protein_path = Path(temporary, "protein.pdb")
            protein_path.write_text(
                "\n".join(
                    [
                        atom_line(1, "CA", "ALA", 1, 0.0),
                        atom_line(2, "CB", "ALA", 1, 20.0),
                        atom_line(3, "CA", "GLY", 2, 30.0),
                        "END",
                    ]
                ),
                encoding="utf-8",
            )
            records = _protein_vicinity_records(
                protein_path,
                [(1.0, 0.0, 0.0)],
            )

        # Atom 2 is far away but retained because atom 1 in the same complete
        # residue is within the ProLIF vicinity; only GLY2 is excluded.
        self.assertEqual(len(records), 2)
        self.assertEqual({record[0] for record in records}, {("A", "   1", " ", "ALA")})
        self.assertEqual({record[1] for record in records}, {"C"})
        self.assertEqual({record[2] for record in records}, {(0.0, 0.0, 0.0), (20.0, 0.0, 0.0)})

    def test_vdw_summary_uses_prolif_radii_and_counts_unique_residues(self) -> None:
        class FakeAtom:
            def __init__(self, symbol):
                self.symbol = symbol

            def GetSymbol(self):
                return self.symbol

        class FakeLigand:
            def GetAtoms(self):
                return [FakeAtom("C"), FakeAtom("N")]

        detector = SimpleNamespace(
            tolerance=0.0,
            _get_radii_sum=lambda ligand, protein: {
                ("C", "O"): 3.0,
                ("N", "O"): 2.5,
            }[(ligand, protein)],
        )
        records = [
            (("A", "  10", " ", "ASP"), "O", (2.9, 0.0, 0.0)),
            (("A", "  10", " ", "ASP"), "O", (2.8, 0.0, 0.0)),
            (("A", "  11", " ", "GLU"), "O", (20.0, 0.0, 0.0)),
        ]
        with patch.object(interactions_module, "_vdw_detector", return_value=detector):
            summary = _vdw_summary(
                records,
                FakeLigand(),
                [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)],
            )

        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["by_type"], {"VdWContact": 1})
        self.assertEqual(summary["residues"], [{"id": "ASP10.A", "types": ["VdWContact"]}])

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
