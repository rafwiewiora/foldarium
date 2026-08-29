from __future__ import annotations

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

from foldarium_pipeline import training_similarity
from foldarium_pipeline.rnp_similarity import (
    RNP_NOVELTY_THRESHOLD,
    RNP_STYLE_METHOD,
    RNP_STYLE_VERSION,
    ligand_aligned_sucos,
    molecule_from_ccd_coordinates,
    rnp_style_top25_similarity,
)
from foldarium_pipeline.training_similarity import (
    SCORER_VERSION,
    AtomCloud,
    TrainingAnalog,
    similarity_result,
)

try:
    import gemmi
    import numpy
    from rdkit import Chem
    from rdkit.Chem import AllChem
except ImportError:  # pragma: no cover - exercised by dependency-light CI
    gemmi = None
    numpy = None
    Chem = None
    AllChem = None


@unittest.skipIf(
    gemmi is None or numpy is None or Chem is None,
    "evaluation extras are unavailable",
)
class RnPStyleSimilarityTests(unittest.TestCase):
    @staticmethod
    def _embedded_molecule(smiles: str):
        molecule = Chem.AddHs(Chem.MolFromSmiles(smiles))
        parameters = AllChem.ETKDGv3()
        parameters.randomSeed = 20260827
        if AllChem.EmbedMolecule(molecule, parameters) != 0:
            raise AssertionError("test molecule embedding failed")
        return Chem.RemoveHs(molecule)

    @staticmethod
    def _transform_molecule(molecule, rotation, translation):
        transformed = Chem.Mol(molecule)
        conformer = transformed.GetConformer()
        for index in range(transformed.GetNumAtoms()):
            position = conformer.GetAtomPosition(index)
            vector = numpy.asarray([position.x, position.y, position.z])
            moved = rotation @ vector + translation
            conformer.SetAtomPosition(index, tuple(float(value) for value in moved))
        return transformed

    @staticmethod
    def _write_ccd(path: Path, component: str, *, extra_atom: bool = False) -> None:
        atoms = [
            f"{component} C1 C 0",
            f"{component} C2 C 0",
            f"{component} O1 O 0",
        ]
        if extra_atom:
            atoms.append(f"{component} N1 N 0")
        bonds = [
            f"{component} C1 C2 SING",
            f"{component} C2 O1 SING",
        ]
        if extra_atom:
            bonds.append(f"{component} O1 N1 SING")
        path.write_text(
            "\n".join(
                [
                    f"data_{component}",
                    "loop_",
                    "_chem_comp_atom.comp_id",
                    "_chem_comp_atom.atom_id",
                    "_chem_comp_atom.type_symbol",
                    "_chem_comp_atom.charge",
                    *atoms,
                    "loop_",
                    "_chem_comp_bond.comp_id",
                    "_chem_comp_bond.atom_id_1",
                    "_chem_comp_bond.atom_id_2",
                    "_chem_comp_bond.value_order",
                    *bonds,
                    "",
                ]
            )
        )

    @staticmethod
    def _ligand_residue(
        component: str,
        *,
        renamed: bool = False,
        disconnected: bool = False,
    ):
        residue = gemmi.Residue()
        residue.name = component
        residue.het_flag = "H"
        residue.seqid = gemmi.SeqId(1, " ")
        names = ("C43", "C44", "O43") if renamed else ("C1", "C2", "O1")
        for name, element, position in (
            (names[0], "C", (0.0, 0.0, 0.0)),
            (names[1], "C", (1.5, 0.0, 0.0)),
            (
                names[2],
                "O",
                (20.0, 0.5, 0.0) if disconnected else (2.8, 0.5, 0.0),
            ),
        ):
            atom = gemmi.Atom()
            atom.name = name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(*position)
            residue.add_atom(atom)
        return residue

    @staticmethod
    def _protein_chain(*, distant: bool):
        chain = gemmi.Chain("A")
        for index, position in enumerate(
            ((50.0, 0.0, 0.0), (55.0, 0.0, 0.0))
            if distant
            else ((0.0, 3.0, 0.0), (2.0, 3.0, 0.0)),
            1,
        ):
            residue = gemmi.Residue()
            residue.name = "ALA"
            residue.seqid = gemmi.SeqId(index, " ")
            atom = gemmi.Atom()
            atom.name = "CA"
            atom.element = gemmi.Element("C")
            atom.pos = gemmi.Position(*position)
            residue.add_atom(atom)
            chain.add_residue(residue)
        return chain

    @classmethod
    def _write_query(
        cls, root: Path, *, renamed_ligand_atoms: bool = False
    ) -> tuple[Path, Path]:
        protein = gemmi.Structure()
        protein_model = gemmi.Model("1")
        protein_model.add_chain(cls._protein_chain(distant=False))
        protein.add_model(protein_model)
        protein_path = root / "query.cif"
        protein.make_mmcif_document().write_file(str(protein_path))

        ligand = gemmi.Structure()
        ligand_model = gemmi.Model("1")
        ligand_chain = gemmi.Chain("X")
        ligand_chain.add_residue(
            cls._ligand_residue("LIG", renamed=renamed_ligand_atoms)
        )
        ligand_model.add_chain(ligand_chain)
        ligand.add_model(ligand_model)
        ligand_path = root / "query-ligand.cif"
        ligand.make_mmcif_document().write_file(str(ligand_path))
        return protein_path, ligand_path

    @classmethod
    def _write_training(
        cls,
        root: Path,
        name: str,
        component: str,
        *,
        distant: bool,
        renamed_ligand_atoms: bool = False,
        disconnected_ligand: bool = False,
        ligand_residue=None,
    ) -> Path:
        structure = gemmi.Structure()
        model = gemmi.Model("1")
        model.add_chain(cls._protein_chain(distant=distant))
        ligand_chain = gemmi.Chain("X")
        ligand_chain.add_residue(
            ligand_residue
            or cls._ligand_residue(
                component,
                renamed=renamed_ligand_atoms,
                disconnected=disconnected_ligand,
            )
        )
        model.add_chain(ligand_chain)
        structure.add_model(model)
        path = root / f"{name}.cif"
        structure.make_mmcif_document().write_file(str(path))
        return path

    @staticmethod
    def _write_ambiguous_ccd(path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    "data_AMB",
                    "loop_",
                    "_chem_comp_atom.comp_id",
                    "_chem_comp_atom.atom_id",
                    "_chem_comp_atom.type_symbol",
                    "_chem_comp_atom.charge",
                    "AMB C1 C 0",
                    "AMB O1 O 0",
                    "AMB O2 O -1",
                    "loop_",
                    "_chem_comp_bond.comp_id",
                    "_chem_comp_bond.atom_id_1",
                    "_chem_comp_bond.atom_id_2",
                    "_chem_comp_bond.value_order",
                    "AMB C1 O1 DOUB",
                    "AMB C1 O2 SING",
                    "",
                ]
            )
        )

    @staticmethod
    def _ambiguous_ligand_residue():
        residue = gemmi.Residue()
        residue.name = "AMB"
        residue.het_flag = "H"
        residue.seqid = gemmi.SeqId(1, " ")
        for name, element, position in (
            ("C43", "C", (0.0, 0.0, 0.0)),
            ("O43", "O", (-1.25, 0.0, 0.0)),
            ("O44", "O", (1.25, 0.0, 0.0)),
        ):
            atom = gemmi.Atom()
            atom.name = name
            atom.element = gemmi.Element(element)
            atom.pos = gemmi.Position(*position)
            residue.add_atom(atom)
        return residue

    @staticmethod
    def _analog(path: Path, pdb_id: str, component: str, rank: int) -> TrainingAnalog:
        return TrainingAnalog(
            pdb_id=pdb_id,
            ligand=component,
            identity=0.4,
            local_rmsd=0.5,
            local_residue_count=2,
            hit_rank=rank,
            cloud=AtomCloud(
                positions=numpy.asarray([[0.0, 0.0, 0.0]]),
                radii=numpy.asarray([1.7]),
            ),
            _source_structure=str(path),
            _source_structure_sha256=sha256(path.read_bytes()).hexdigest(),
            _ligand_chain_index=1,
            _ligand_residue_index=0,
            _query_residue_indices=(0, 1),
            _target_chain_index=0,
            _target_residue_indices=(0, 1),
        )

    def test_rigid_translation_and_rotation_do_not_change_ligand_aligned_sucos(
        self,
    ) -> None:
        query = self._embedded_molecule("CCOc1ccccc1")
        rotation = numpy.asarray(
            [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        )
        moved = self._transform_molecule(
            query, rotation, numpy.asarray([31.0, -17.0, 8.0])
        )

        baseline = ligand_aligned_sucos(query, query)
        transformed = ligand_aligned_sucos(query, moved)

        self.assertAlmostEqual(
            baseline["sucos"], transformed["sucos"], places=6
        )
        self.assertAlmostEqual(transformed["sucos"], 1.0, places=6)

    def test_fully_renamed_coordinate_atoms_use_unique_ccd_topology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ccd = root / "ETL.cif"
            self._write_ccd(ccd, "ETL")
            query_protein, query_ligand = self._write_query(
                root, renamed_ligand_atoms=True
            )
            analog = self._analog(
                self._write_training(root, "near", "ETL", distant=False),
                "1ABC",
                "ETL",
                1,
            )

            result = rnp_style_top25_similarity(
                query_protein,
                query_ligand,
                "ETL",
                [analog],
                ccd_cache_directory=root / "cache",
                ccd_loader=lambda _component: ccd,
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["classification"], "familiar")
            self.assertEqual(result["train_pdb"], "1ABC")
            self.assertEqual(result["evaluated_candidate_count"], 1)
            self.assertFalse(result["candidate_failures"])

    def test_renamed_atoms_preserve_two_letter_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ccd = root / "CLM.cif"
            ccd.write_text(
                "\n".join(
                    [
                        "data_CLM",
                        "loop_",
                        "_chem_comp_atom.comp_id",
                        "_chem_comp_atom.atom_id",
                        "_chem_comp_atom.type_symbol",
                        "_chem_comp_atom.charge",
                        "CLM C1 C 0",
                        "CLM CL1 CL 0",
                        "loop_",
                        "_chem_comp_bond.comp_id",
                        "_chem_comp_bond.atom_id_1",
                        "_chem_comp_bond.atom_id_2",
                        "_chem_comp_bond.value_order",
                        "CLM C1 CL1 SING",
                        "",
                    ]
                )
            )
            residue = gemmi.Residue()
            residue.name = "CLM"
            residue.het_flag = "H"
            residue.seqid = gemmi.SeqId(1, " ")
            for name, element, position in (
                ("C43", "C", (0.0, 0.0, 0.0)),
                ("CL43", "Cl", (1.75, 0.0, 0.0)),
            ):
                atom = gemmi.Atom()
                atom.name = name
                atom.element = gemmi.Element(element)
                atom.pos = gemmi.Position(*position)
                residue.add_atom(atom)

            molecule = molecule_from_ccd_coordinates(residue, "CLM", ccd)

            self.assertEqual(
                [atom.GetSymbol() for atom in molecule.GetAtoms()],
                ["C", "Cl"],
            )
            self.assertEqual(molecule.GetNumBonds(), 1)

    def test_ambiguous_or_incorrect_renamed_connectivity_remains_unknown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query_ccd = root / "ETL.cif"
            ambiguous_ccd = root / "AMB.cif"
            self._write_ccd(query_ccd, "ETL")
            self._write_ambiguous_ccd(ambiguous_ccd)
            query_protein, query_ligand = self._write_query(root)
            ambiguous = self._analog(
                self._write_training(
                    root,
                    "ambiguous",
                    "AMB",
                    distant=False,
                    ligand_residue=self._ambiguous_ligand_residue(),
                ),
                "2DEF",
                "AMB",
                1,
            )
            disconnected = self._analog(
                self._write_training(
                    root,
                    "disconnected",
                    "ETL",
                    distant=False,
                    renamed_ligand_atoms=True,
                    disconnected_ligand=True,
                ),
                "3GHI",
                "ETL",
                2,
            )

            result = rnp_style_top25_similarity(
                query_protein,
                query_ligand,
                "ETL",
                [ambiguous, disconnected],
                ccd_cache_directory=root / "cache",
                ccd_loader=lambda component: (
                    ambiguous_ccd if component == "AMB" else query_ccd
                ),
            )

            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["classification"], "unknown")
            self.assertIsNone(result["sucos_shape_pocket_qcov"])
            self.assertEqual(result["evaluated_candidate_count"], 0)
            self.assertEqual(len(result["candidate_failures"]), 2)
            reasons = [
                failure["reason"] for failure in result["candidate_failures"]
            ]
            self.assertTrue(any("no unique valid" in reason for reason in reasons))
            self.assertTrue(any("not one connected" in reason for reason in reasons))

    def test_pocket_mismatch_lowers_combined_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ccd = root / "ETL.cif"
            self._write_ccd(ccd, "ETL")
            query_protein, query_ligand = self._write_query(root)
            near = self._analog(
                self._write_training(root, "near", "ETL", distant=False),
                "1ABC",
                "ETL",
                1,
            )
            far = self._analog(
                self._write_training(root, "far", "ETL", distant=True),
                "2DEF",
                "ETL",
                2,
            )
            kwargs = {
                "query_structure": query_protein,
                "query_ligand_structure": query_ligand,
                "query_component_id": "ETL",
                "ccd_cache_directory": root / "cache",
                "ccd_loader": lambda _component: ccd,
            }

            near_result = rnp_style_top25_similarity(
                analogs=[near], **kwargs
            )
            far_result = rnp_style_top25_similarity(analogs=[far], **kwargs)

            self.assertEqual(near_result["method"], RNP_STYLE_METHOD)
            self.assertEqual(near_result["version"], RNP_STYLE_VERSION)
            self.assertEqual(near_result["threshold"], RNP_NOVELTY_THRESHOLD)
            self.assertEqual(near_result["classification"], "familiar")
            self.assertFalse(near_result["novel"])
            self.assertEqual(near_result["reason"], "rnp-score-at-least-0.25")
            self.assertEqual(near_result["train_pdb"], "1ABC")
            self.assertEqual(near_result["train_het"], "ETL")
            self.assertEqual(near_result["pocket_qcov"], 1.0)
            self.assertEqual(far_result["pocket_qcov"], 0.0)
            self.assertAlmostEqual(
                near_result["sucos"], far_result["sucos"], places=12
            )
            self.assertGreater(
                near_result["sucos_shape_pocket_qcov"],
                far_result["sucos_shape_pocket_qcov"],
            )

    def test_invalid_ccd_chemistry_is_unknown_candidate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            query_ccd = root / "ETL.cif"
            invalid_ccd = root / "BAD.cif"
            self._write_ccd(query_ccd, "ETL")
            self._write_ccd(invalid_ccd, "BAD", extra_atom=True)
            query_protein, query_ligand = self._write_query(root)
            invalid = self._analog(
                self._write_training(root, "invalid", "BAD", distant=False),
                "3GHI",
                "BAD",
                1,
            )

            result = rnp_style_top25_similarity(
                query_protein,
                query_ligand,
                "ETL",
                [invalid],
                ccd_cache_directory=root / "cache",
                ccd_loader=lambda component: (
                    query_ccd if component == "ETL" else invalid_ccd
                ),
            )

            self.assertEqual(result["status"], "unknown")
            self.assertIsNone(result["sucos_shape_pocket_qcov"])
            self.assertEqual(result["evaluated_candidate_count"], 0)
            self.assertEqual(result["candidate_failures"][0]["pdb_id"], "3GHI")
            self.assertIn(
                "do not match the CCD element multiset",
                result["candidate_failures"][0]["reason"],
            )

    def test_invalid_candidate_is_logged_without_invalidating_valid_score(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid_ccd = root / "ETL.cif"
            invalid_ccd = root / "BAD.cif"
            self._write_ccd(valid_ccd, "ETL")
            self._write_ccd(invalid_ccd, "BAD", extra_atom=True)
            query_protein, query_ligand = self._write_query(root)
            invalid = self._analog(
                self._write_training(root, "invalid", "BAD", distant=False),
                "3GHI",
                "BAD",
                1,
            )
            valid = self._analog(
                self._write_training(root, "valid", "ETL", distant=False),
                "1ABC",
                "ETL",
                2,
            )

            result = rnp_style_top25_similarity(
                query_protein,
                query_ligand,
                "ETL",
                [invalid, valid],
                ccd_cache_directory=root / "cache",
                ccd_loader=lambda component: (
                    invalid_ccd if component == "BAD" else valid_ccd
                ),
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["classification"], "familiar")
            self.assertEqual(result["train_pdb"], "1ABC")
            self.assertIsNotNone(result["sucos_shape_pocket_qcov"])
            self.assertEqual(result["evaluated_candidate_count"], 1)
            self.assertEqual(len(result["candidate_failures"]), 1)
            self.assertEqual(result["candidate_failures"][0]["pdb_id"], "3GHI")

    def test_rnp_threshold_is_independent_of_canonical_metric_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ccd = root / "ETL.cif"
            self._write_ccd(ccd, "ETL")
            query_protein, query_ligand = self._write_query(root)
            analog = self._analog(
                self._write_training(root, "near", "ETL", distant=False),
                "1ABC",
                "ETL",
                1,
            )
            with mock.patch.object(training_similarity, "NOVELTY_THRESHOLD", 1.1):
                result = rnp_style_top25_similarity(
                    query_protein,
                    query_ligand,
                    "ETL",
                    [analog],
                    ccd_cache_directory=root / "cache",
                    ccd_loader=lambda _component: ccd,
                )

        self.assertEqual(RNP_NOVELTY_THRESHOLD, 0.25)
        self.assertEqual(result["classification"], "familiar")
        self.assertEqual(result["threshold"], 0.25)

    def test_existing_similarity_result_and_version_are_unchanged(self) -> None:
        cloud = AtomCloud(
            positions=numpy.asarray([[0.0, 0.0, 0.0]]),
            radii=numpy.asarray([1.7]),
        )
        analog = TrainingAnalog(
            pdb_id="1ABC",
            ligand="DRG",
            identity=0.42,
            local_rmsd=0.8,
            local_residue_count=7,
            hit_rank=1,
            cloud=cloud,
        )

        result = similarity_result(
            cloud,
            [analog],
            [],
            [{"pdb": "1ABC", "identity": 0.42}],
        )

        self.assertEqual(
            SCORER_VERSION, "foldseek-pdb100-carried-ligand-overlap/v7"
        )
        self.assertEqual(
            result,
            {
                "classification": "familiar",
                "reason": "training-ligand-overlap-at-least-0.25",
                "novel": False,
                "train_pdb": "1ABC",
                "train_het": "DRG",
                "train_identity": 0.42,
                "train_max_protein_identity": 0.42,
                "train_align_rmsd": 0.8,
                "train_local_residue_count": 7,
                "train_hit_rank": 1,
                "train_shape_overlap": 1.0,
                "foldseek_hit_count": 1,
                "training_analog_count": 1,
                "candidate_failures": [],
                "cutoff": "2021-09-30",
                "novel_threshold": 0.25,
                "pocket_radius_angstrom": 8.0,
                "maximum_local_rmsd_angstrom": 3.0,
                "foldseek_database": "pdb100",
                "foldseek_mode": "3diaa",
                "scorer_version": "foldseek-pdb100-carried-ligand-overlap/v7",
            },
        )


if __name__ == "__main__":
    unittest.main()
