from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from foldarium_pipeline.evaluation import (
    EVALUATOR_VERSION,
    EvaluationError,
    LIGAND_MAPPING_POLICY_FULL,
    LIGAND_MAPPING_POLICY_FULL_EXPLICIT,
    LIGAND_MAPPING_POLICY_FULL_TASK_SMILES,
    LIGAND_MAPPING_POLICY_PARTIAL,
    LIGAND_MAPPING_POLICY_PARTIAL_EXPLICIT,
    LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES,
    PARTIAL_REFERENCE_COVERAGE_MIN,
    RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
    TOPOLOGY_SOURCE_EXPLICIT,
    TOPOLOGY_SOURCE_INFERRED,
    TOPOLOGY_SOURCE_TASK_SMILES,
    best_receptor_superposition,
    evaluate_ligand_pose,
    exact_complex_receptor_superposition,
    exact_complex_tm_superposition,
    released_partial_reference_override_for_item,
    _chem_comp_bond_edges,
    _conformer_heavy_atoms,
    _exact_ligand_conformers,
    _explicit_connectivity_molecule_from_atoms,
    _is_safe_terminal_omission_mapping,
    _receptor_candidate_key,
    _residue_altloc_keys,
    _resolve_ligand_mappings,
    _robust_sequence_superposition,
    _sequence_superposition,
    _task_smiles_connectivity_molecule_from_atoms,
)
from foldarium_pipeline.wednesday_reveal import _evaluation_fields

try:
    import gemmi

    HAS_GEMMI = True
except (ImportError, ModuleNotFoundError):
    HAS_GEMMI = False


class FakeAtom:
    def __init__(self, position) -> None:
        self.name = "CA"
        self.pos = position


class FakeResidue(list):
    def __init__(self, position) -> None:
        super().__init__([FakeAtom(position)])
        self.name = "ALA"


class FakeChain:
    def __init__(self, name, polymer) -> None:
        self.name = name
        self._polymer = polymer

    def get_polymer(self):
        return self._polymer


class ReceptorCandidatePolicyTests(unittest.TestCase):
    def test_blind_assembly_can_prefer_one_stable_chain_pair(self) -> None:
        chain_a = {
            "sequence_similarity": 1.0,
            "receptor_rmsd": 8.0,
            "reference_chain": "A",
            "predicted_chain": "A",
        }
        chain_c = {
            "sequence_similarity": 1.0,
            "receptor_rmsd": 2.0,
            "reference_chain": "C",
            "predicted_chain": "C",
        }

        self.assertLess(
            _receptor_candidate_key(chain_c, stable_chain_pair=False),
            _receptor_candidate_key(chain_a, stable_chain_pair=False),
        )
        self.assertLess(
            _receptor_candidate_key(chain_a, stable_chain_pair=True),
            _receptor_candidate_key(chain_c, stable_chain_pair=True),
        )


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class RobustCoreSuperpositionTests(unittest.TestCase):
    @staticmethod
    def translated_polymer(count: int, translation=(0.0, 0.0, 0.0)):
        return [
            FakeResidue(
                gemmi.Position(
                    index * 1.3 + translation[0],
                    (index % 7) * 1.7 + translation[1],
                    (index % 5) * 0.9 + translation[2],
                )
            )
            for index in range(count)
        ]

    def test_plain_fit_uses_kabsch_when_gemmi_returns_nonfinite(self) -> None:
        class NonfiniteGemmi:
            def __getattr__(self, name):
                return getattr(gemmi, name)

            @staticmethod
            def superpose_positions(_reference, _predicted):
                return type("NonfiniteFit", (), {"rmsd": math.nan})()

        reference = [
            FakeResidue(gemmi.Position(float(index), 0.0, 0.0))
            for index in range(7)
        ]
        predicted = [
            FakeResidue(gemmi.Position(float(index) + 4.0, -3.0, 2.0))
            for index in range(7)
        ]
        fit = _sequence_superposition(reference, predicted, NonfiniteGemmi())

        self.assertLess(fit.rmsd, 1e-10)
        transformed = fit.transform.apply(predicted[3][0].pos)
        self.assertAlmostEqual(transformed.x, reference[3][0].pos.x)
        self.assertAlmostEqual(transformed.y, reference[3][0].pos.y)
        self.assertAlmostEqual(transformed.z, reference[3][0].pos.z)

    def test_binds_both_filtered_chains_to_the_exact_task_sequence(self) -> None:
        reference_model = [FakeChain("A", self.translated_polymer(6))]
        predicted_model = [
            FakeChain("A", self.translated_polymer(6, translation=(4.0, -2.0, 1.0)))
        ]

        result = best_receptor_superposition(
            reference_model,
            predicted_model,
            stable_chain_pair=True,
            reference_chain_ids={"A"},
            predicted_chain_ids={"A"},
            robust_core=True,
            expected_sequence="AAAAAA",
        )

        self.assertEqual(result["reference_chain"], "A")
        self.assertEqual(result["predicted_chain"], "A")
        self.assertEqual(result["sequence_binding_policy"], "exact-task-sequence/v1")
        with self.assertRaisesRegex(EvaluationError, "no compatible receptor chains"):
            best_receptor_superposition(
                reference_model,
                predicted_model,
                reference_chain_ids={"A"},
                predicted_chain_ids={"A"},
                robust_core=True,
                expected_sequence="AAAATA",
            )

    def test_recovers_a_majority_core_from_a_coherently_moved_domain(self) -> None:
        reference = self.translated_polymer(40)
        predicted = self.translated_polymer(40, translation=(7.0, -4.0, 3.0))
        for residue in predicted[30:]:
            residue[0].pos.y += 20.0

        robust, audit = _robust_sequence_superposition(reference, predicted, gemmi)

        self.assertEqual(audit["aligned_residue_count"], 40)
        self.assertEqual(audit["retained_residue_count"], 30)
        self.assertEqual(
            audit["coarse_policy"],
            "deterministic-pooled-75-percent-least-trimmed-plus-per-chain-windows/v3",
        )
        self.assertEqual(audit["local_seed_residue_count"], 8)
        self.assertEqual(audit["coarse_retained_counts"][0], 40)
        self.assertIn(30, audit["coarse_retained_counts"])
        self.assertLess(robust.rmsd, 1e-5)
        transformed = robust.transform.apply(predicted[10][0].pos)
        self.assertAlmostEqual(transformed.x, reference[10][0].pos.x, places=5)
        self.assertAlmostEqual(transformed.y, reference[10][0].pos.y, places=5)
        self.assertAlmostEqual(transformed.z, reference[10][0].pos.z, places=5)

    def test_pools_exact_task_chains_and_rejects_relative_chain_motion(self) -> None:
        translation = (7.0, -4.0, 3.0)
        reference_model = [
            FakeChain("A", self.translated_polymer(40)),
            FakeChain("B", self.translated_polymer(60, translation=(0.0, 50.0, 0.0))),
            FakeChain("C", self.translated_polymer(40, translation=(0.0, 100.0, 0.0))),
        ]
        predicted_model = [
            FakeChain("A", self.translated_polymer(40, translation=translation)),
            FakeChain("B", self.translated_polymer(60, translation=(40.0, 50.0, 0.0))),
            FakeChain(
                "C",
                self.translated_polymer(
                    40,
                    translation=(
                        translation[0],
                        100.0 + translation[1],
                        translation[2],
                    ),
                ),
            ),
        ]

        result = exact_complex_receptor_superposition(
            reference_model,
            predicted_model,
            expected_chain_sequences={
                "A": "A" * 40,
                "B": "A" * 60,
                "C": "A" * 40,
            },
        )
        reverse = exact_complex_receptor_superposition(
            predicted_model,
            reference_model,
            expected_chain_sequences={
                "A": "A" * 40,
                "B": "A" * 60,
                "C": "A" * 40,
            },
        )

        self.assertEqual(result["reference_chains"], ["A", "B", "C"])
        self.assertEqual(result["predicted_chains"], ["A", "B", "C"])
        self.assertEqual(
            result["sequence_binding_policy"],
            "exact-task-chain-id-and-sequence/v1",
        )
        self.assertLess(result["receptor_rmsd"], 1e-5)
        self.assertEqual(result["receptor_rmsd"], reverse["receptor_rmsd"])
        self.assertEqual(result["robust_core"], reverse["robust_core"])
        self.assertEqual(result["robust_core"]["aligned_residue_count"], 140)
        contributions = {
            row["chain_id"]: row
            for row in result["robust_core"]["per_chain"]
        }
        self.assertEqual(contributions["A"]["retained_residue_count"], 40)
        self.assertEqual(contributions["B"]["retained_residue_count"], 0)
        self.assertEqual(contributions["C"]["retained_residue_count"], 40)
        post_transform = result["post_transform_ca"]
        self.assertEqual(post_transform["count"], 140)
        self.assertGreater(post_transform["rmsd"], 20.0)
        post_by_chain = {
            row["chain_id"]: row for row in post_transform["per_chain"]
        }
        self.assertLess(post_by_chain["A"]["rmsd"], 1e-5)
        self.assertGreater(post_by_chain["B"]["rmsd"], 30.0)
        self.assertLess(post_by_chain["C"]["rmsd"], 1e-5)

    def test_exact_complex_rmsd_is_symmetric_to_numerical_tolerance(self) -> None:
        reference = self.translated_polymer(80)
        predicted = self.translated_polymer(80, translation=(7.0, -4.0, 3.0))
        for index, residue in enumerate(predicted):
            residue[0].pos.x += math.sin(index) * 0.08
            residue[0].pos.y += math.cos(index) * 0.06
            if index >= 64:
                residue[0].pos.z += 12.0
        expected = {"A": "A" * 80}

        forward = exact_complex_receptor_superposition(
            [FakeChain("A", reference)],
            [FakeChain("A", predicted)],
            expected_chain_sequences=expected,
        )
        reverse = exact_complex_receptor_superposition(
            [FakeChain("A", predicted)],
            [FakeChain("A", reference)],
            expected_chain_sequences=expected,
        )

        self.assertLess(
            abs(forward["receptor_rmsd"] - reverse["receptor_rmsd"]),
            1e-10,
        )
        self.assertEqual(forward["robust_core"], reverse["robust_core"])

    def test_exact_task_complex_fails_closed_for_missing_chain(self) -> None:
        reference_model = [FakeChain("A", self.translated_polymer(6))]
        predicted_model = [FakeChain("A", self.translated_polymer(6))]

        with self.assertRaisesRegex(EvaluationError, "lacks submitted protein chain"):
            exact_complex_receptor_superposition(
                reference_model,
                predicted_model,
                expected_chain_sequences={"A": "AAAAAA", "B": "AAAAAA"},
            )

    def test_global_tm_frame_cannot_anchor_on_an_unrelated_small_chain_only(self) -> None:
        """Regression for the A1CIK/PyMOL-style tiny-core frame failure."""

        chain_b_reference = self.translated_polymer(342, (0.0, 50.0, 0.0))
        chain_b_predicted = self.translated_polymer(342, (40.0, 50.0, 0.0))
        for index, residue in enumerate(chain_b_predicted):
            # A broad domain can be consistently close without satisfying the
            # 2 A iterative core used by the historical/PyMOL-like diagnostic.
            residue[0].pos.y += 4.5 * math.sin(index)
            residue[0].pos.z += 4.5 * math.cos(index)
        chain_c_predicted = self.translated_polymer(190, (80.0, 100.0, 0.0))
        for index, residue in enumerate(chain_c_predicted):
            residue[0].pos.y += 4.5 * math.sin(index)
            residue[0].pos.z += 4.5 * math.cos(index)
        reference_model = [
            FakeChain("A", self.translated_polymer(120)),
            FakeChain("B", chain_b_reference),
            FakeChain("C", self.translated_polymer(190, (0.0, 100.0, 0.0))),
        ]
        predicted_model = [
            # A is exact but is not representative of the dominant complex.
            FakeChain("A", self.translated_polymer(120)),
            FakeChain("B", chain_b_predicted),
            FakeChain("C", chain_c_predicted),
        ]
        expected_sequences = {"A": "A" * 120, "B": "A" * 342, "C": "A" * 190}
        historical = exact_complex_receptor_superposition(
            reference_model,
            predicted_model,
            expected_chain_sequences=expected_sequences,
        )
        result = exact_complex_tm_superposition(
            reference_model,
            predicted_model,
            expected_chain_sequences=expected_sequences,
        )

        historical_support = {
            row["chain_id"]: row for row in historical["robust_core"]["per_chain"]
        }
        support = {
            row["chain_id"]: row for row in result["global_coverage"]["per_chain"]
        }
        self.assertEqual(historical_support["A"]["retained_residue_count"], 120)
        self.assertEqual(historical_support["B"]["retained_residue_count"], 0)
        self.assertGreater(result["receptor_tm_score"], 0.40)
        self.assertEqual(support["A"]["retained_residue_count"], 0)
        self.assertGreater(support["B"]["retained_residue_count"], 300)
        self.assertEqual(support["C"]["retained_residue_count"], 0)
        self.assertGreater(result["global_coverage"]["retained_residue_count"], 300)
        transformed = result["transform"].apply(predicted_model[1].get_polymer()[10][0].pos)
        expected = reference_model[1].get_polymer()[10][0].pos
        displacement = math.sqrt(
            (transformed.x - expected.x) ** 2
            + (transformed.y - expected.y) ** 2
            + (transformed.z - expected.z) ** 2
        )
        self.assertLess(displacement, 5.0)

    def test_rejects_flexible_ca_outliers_and_recovers_the_shared_core_frame(self) -> None:
        translation = (7.0, -4.0, 3.0)
        reference = []
        predicted = []
        for index in range(62):
            reference_position = gemmi.Position(
                index * 1.3,
                (index % 7) * 1.7,
                (index % 5) * 0.9,
            )
            predicted_position = gemmi.Position(
                reference_position.x + translation[0],
                reference_position.y + translation[1],
                reference_position.z + translation[2],
            )
            if index == 60:
                predicted_position.y += 25.0
            elif index == 61:
                predicted_position.y -= 25.0
            reference.append(FakeResidue(reference_position))
            predicted.append(FakeResidue(predicted_position))

        plain = _sequence_superposition(reference, predicted, gemmi)
        robust, audit = _robust_sequence_superposition(reference, predicted, gemmi)

        self.assertGreater(plain.rmsd, 4.0)
        self.assertLess(robust.rmsd, 1e-5)
        self.assertEqual(
            audit["policy"], "sequence-ca-iterative-outlier-rejection/v1"
        )
        self.assertEqual(audit["aligned_residue_count"], 62)
        self.assertEqual(audit["retained_residue_count"], 60)
        self.assertLess(audit["retained_fraction"], 1.0)
        self.assertEqual(audit["local_seed_residue_count"], 12)
        transformed = robust.transform.apply(predicted[10][0].pos)
        self.assertAlmostEqual(transformed.x, reference[10][0].pos.x, places=5)
        self.assertAlmostEqual(transformed.y, reference[10][0].pos.y, places=5)
        self.assertAlmostEqual(transformed.z, reference[10][0].pos.z, places=5)


def _add_protein_chain(model: Any, chain_id: str = "A", residue_count: int = 10) -> None:
    chain = gemmi.Chain(chain_id)
    for index in range(residue_count):
        residue = gemmi.Residue()
        residue.name = "ALA"
        residue.seqid.num = index + 1
        atom = gemmi.Atom()
        atom.name = "CA"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(index * 3.8, 0.0, 0.0)
        residue.add_atom(atom)
        chain.add_residue(residue)
    model.add_chain(chain)


def _add_ligand_residue(
    model: Any,
    *,
    chain_id: str,
    component_id: str,
    positions: list[tuple[float, float, float]],
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> None:
    chain = gemmi.Chain(chain_id)
    residue = gemmi.Residue()
    residue.name = component_id
    residue.seqid.num = 1
    for index, (x, y, z) in enumerate(positions):
        atom = gemmi.Atom()
        atom.name = f"C{index + 1:02d}"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(
            x + translation[0],
            y + translation[1],
            z + translation[2],
        )
        residue.add_atom(atom)
    chain.add_residue(residue)
    model.add_chain(chain)


def _linear_chain_positions(count: int, spacing: float = 1.54) -> list[tuple[float, float, float]]:
    return [(index * spacing, 0.0, 0.0) for index in range(count)]


def _scaffold_with_terminal_branches(
    scaffold_count: int,
    terminal_count: int,
    *,
    spacing: float = 1.54,
    branch_length: float = 1.54,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    scaffold = _linear_chain_positions(scaffold_count, spacing=spacing)
    anchor_indices = [
        scaffold_count // 3,
        (2 * scaffold_count) // 3,
        scaffold_count - 1,
    ][:terminal_count]
    terminals = [
        (scaffold[anchor][0], scaffold[anchor][1] + branch_length, scaffold[anchor][2])
        for anchor in anchor_indices
    ]
    return scaffold, terminals


def _below_partial_coverage_observed(expected_heavy_atoms: int) -> int:
    minimum_observed = math.ceil(expected_heavy_atoms * PARTIAL_REFERENCE_COVERAGE_MIN)
    return max(1, minimum_observed - 1)


def _write_pose_fixture(
    path: Path,
    *,
    ligand_positions: list[tuple[float, float, float]],
    component_id: str = "IO0",
    ligand_chain: str = "B",
    ligand_translation: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> None:
    structure = gemmi.Structure()
    structure.add_model(gemmi.Model(0))
    model = structure[0]
    _add_protein_chain(model)
    _add_ligand_residue(
        model,
        chain_id=ligand_chain,
        component_id=component_id,
        positions=ligand_positions,
        translation=ligand_translation,
    )
    structure.write_minimal_pdb(str(path))


def _add_altloc_ligand_residue(
    model: Any,
    *,
    chain_id: str,
    component_id: str,
    atoms: list[tuple[str, tuple[float, float, float], str]],
) -> None:
    chain = gemmi.Chain(chain_id)
    residue = gemmi.Residue()
    residue.name = component_id
    residue.seqid.num = 1
    for name, (x, y, z), altloc in atoms:
        atom = gemmi.Atom()
        atom.name = name
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(x, y, z)
        atom.altloc = altloc if altloc else "\0"
        residue.add_atom(atom)
    chain.add_residue(residue)
    model.add_chain(chain)


def _altloc_reference_atoms(
    *,
    per_conformer: int,
    common_count: int,
    altloc_labels: tuple[str, ...] = ("A", "B"),
    spacing: float = 1.54,
    altloc_y_offset: float = 5.0,
) -> list[tuple[str, tuple[float, float, float], str]]:
    atoms: list[tuple[str, tuple[float, float, float], str]] = []
    for index in range(common_count):
        position = (index * spacing, 0.0, 0.0)
        atoms.append((f"C{index + 1:02d}", position, ""))
    alt_specific = per_conformer - common_count
    for altloc_index, altloc in enumerate(altloc_labels):
        y_offset = 0.0 if altloc_index == 0 else altloc_y_offset
        for offset in range(alt_specific):
            index = common_count + offset
            position = (index * spacing, y_offset, 0.0)
            atoms.append((f"C{index + 1:02d}", position, altloc))
    return atoms


def _write_altloc_pose_fixtures(
    root: Path,
    *,
    reference_atoms: list[tuple[str, tuple[float, float, float], str]],
    predicted_positions: list[tuple[float, float, float]],
    component_id: str = "A1E",
) -> tuple[Path, Path]:
    reference_path = root / "reference.pdb"
    prediction_path = root / "prediction.pdb"

    reference = gemmi.Structure()
    reference.add_model(gemmi.Model(0))
    reference_model = reference[0]
    _add_protein_chain(reference_model)
    _add_altloc_ligand_residue(
        reference_model,
        chain_id="B",
        component_id=component_id,
        atoms=reference_atoms,
    )
    reference.write_minimal_pdb(str(reference_path))

    prediction = gemmi.Structure()
    prediction.add_model(gemmi.Model(0))
    prediction_model = prediction[0]
    _add_protein_chain(prediction_model)
    _add_ligand_residue(
        prediction_model,
        chain_id="C",
        component_id=component_id,
        positions=predicted_positions,
    )
    prediction.write_minimal_pdb(str(prediction_path))
    return reference_path, prediction_path


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class LigandPoseEvaluationTests(unittest.TestCase):
    component_id = "IO0"
    heavy_atoms = 24

    def setUp(self) -> None:
        try:
            from rdkit import Chem  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("RDKit is an optional evaluation dependency")

    def test_exact_reference_path_is_unchanged_except_for_audit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            positions = _linear_chain_positions(self.heavy_atoms)
            _write_pose_fixture(reference_path, ligand_positions=positions)
            _write_pose_fixture(
                prediction_path,
                ligand_positions=positions,
                ligand_translation=(0.0, 0.2, 0.0),
            )

            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertEqual(score["evaluator_version"], EVALUATOR_VERSION)
        self.assertAlmostEqual(score["rmsd"], 0.2, places=5)
        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL)
        self.assertEqual(score["reference_heavy_atoms_expected"], self.heavy_atoms)
        self.assertEqual(score["reference_heavy_atoms_observed"], self.heavy_atoms)
        self.assertEqual(score["reference_heavy_atoms_scored"], self.heavy_atoms)
        self.assertAlmostEqual(score["reference_coverage"], 1.0)
        self.assertEqual(
            len(score["predicted_ligand_coordinates_reference_order"]),
            self.heavy_atoms,
        )
        self.assertEqual(len(score["symmetry_mapping"]), self.heavy_atoms)

    def test_partial_connected_subgraph_scores_observed_atoms_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            full_positions = _linear_chain_positions(self.heavy_atoms)
            partial_positions = full_positions[:21]
            _write_pose_fixture(reference_path, ligand_positions=partial_positions)
            predicted_positions = [
                (x, y + 0.3, z) for x, y, z in full_positions
            ]
            _write_pose_fixture(prediction_path, ligand_positions=predicted_positions)

            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertAlmostEqual(score["rmsd"], 0.3, places=5)
        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL)
        self.assertEqual(score["reference_heavy_atoms_observed"], 21)
        self.assertEqual(score["reference_heavy_atoms_scored"], 21)
        self.assertAlmostEqual(score["reference_coverage"], 21 / 24)
        self.assertEqual(len(score["reference_ligand_coordinates"]), 21)
        self.assertEqual(len(score["predicted_ligand_coordinates"]), 24)
        self.assertEqual(
            len(score["predicted_ligand_coordinates_reference_order"]),
            21,
        )
        self.assertEqual(len(score["symmetry_mapping"]), 21)
        predicted = score["predicted_ligand_coordinates"]
        reference_order = score["predicted_ligand_coordinates_reference_order"]
        for index, predicted_index in enumerate(score["symmetry_mapping"]):
            self.assertEqual(reference_order[index], predicted[predicted_index])

    def test_partial_terminal_branches_accept_15_of_18(self) -> None:
        component_id = "R06"
        heavy_atoms = 18
        scaffold, terminals = _scaffold_with_terminal_branches(15, 3)
        full_positions = scaffold + terminals
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            _write_pose_fixture(
                reference_path,
                ligand_positions=scaffold,
                component_id=component_id,
            )
            predicted_positions = [
                (x, y + 0.25, z) for x, y, z in full_positions
            ]
            _write_pose_fixture(
                prediction_path,
                ligand_positions=predicted_positions,
                component_id=component_id,
            )

            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=component_id,
                heavy_atoms=heavy_atoms,
            )

        self.assertAlmostEqual(score["rmsd"], 0.25, places=5)
        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL)
        self.assertEqual(score["reference_heavy_atoms_observed"], 15)
        self.assertEqual(score["reference_heavy_atoms_scored"], 15)
        self.assertAlmostEqual(score["reference_coverage"], 15 / 18)
        self.assertGreaterEqual(score["reference_coverage"], PARTIAL_REFERENCE_COVERAGE_MIN)

    def test_safe_partial_filter_rejects_bridging_omission(self) -> None:
        from rdkit import Chem

        molecule = Chem.RWMol()
        for _ in range(18):
            molecule.AddAtom(Chem.Atom(6))
        for index in range(17):
            molecule.AddBond(index, index + 1, Chem.BondType.SINGLE)
        predicted = molecule.GetMol()

        bridging_mapping = tuple(list(range(7)) + list(range(8, 16)))
        terminal_mapping = tuple(range(15))

        self.assertFalse(
            _is_safe_terminal_omission_mapping(predicted, bridging_mapping)
        )
        self.assertTrue(
            _is_safe_terminal_omission_mapping(predicted, terminal_mapping)
        )

    def test_below_threshold_partial_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            observed = _below_partial_coverage_observed(self.heavy_atoms)
            self.assertLess(observed / self.heavy_atoms, PARTIAL_REFERENCE_COVERAGE_MIN)
            _write_pose_fixture(
                reference_path,
                ligand_positions=_linear_chain_positions(observed),
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
            )

            with self.assertRaisesRegex(
                EvaluationError,
                f"reference contains no {self.component_id} ligand with {self.heavy_atoms} atoms",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_disconnected_partial_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            disconnected = (
                _linear_chain_positions(11)
                + [(40.0 + index * 1.54, 0.0, 0.0) for index in range(10)]
            )
            _write_pose_fixture(reference_path, ligand_positions=disconnected)
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_nonmatching_partial_graph_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            star = [(0.0, 0.0, 0.0)] + [
                (
                    1.54 * math.cos(index * 2 * math.pi / 20),
                    1.54 * math.sin(index * 2 * math.pi / 20),
                    0.0,
                )
                for index in range(20)
            ]
            _write_pose_fixture(reference_path, ligand_positions=star)
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_prediction_must_remain_full_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            _write_pose_fixture(
                reference_path,
                ligand_positions=_linear_chain_positions(21),
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(21),
            )

            with self.assertRaisesRegex(
                EvaluationError,
                f"prediction contains no ligand with {self.heavy_atoms} heavy atoms",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                )


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class AltlocLigandEvaluationTests(unittest.TestCase):
    component_id = "A1E"
    heavy_atoms = 10
    common_count = 3

    def setUp(self) -> None:
        try:
            from rdkit import Chem  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("RDKit is an optional evaluation dependency")

    def test_altloc_residue_does_not_double_count(self) -> None:
        reference_atoms = _altloc_reference_atoms(
            per_conformer=self.heavy_atoms,
            common_count=0,
        )
        predicted_positions = _linear_chain_positions(self.heavy_atoms)
        with tempfile.TemporaryDirectory() as temporary:
            reference_path, prediction_path = _write_altloc_pose_fixtures(
                Path(temporary),
                reference_atoms=reference_atoms,
                predicted_positions=predicted_positions,
                component_id=self.component_id,
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertEqual(len(score["reference_ligand_coordinates"]), self.heavy_atoms)
        self.assertEqual(len(score["predicted_ligand_coordinates"]), self.heavy_atoms)
        self.assertIn(score["reference_ligand_altloc"], {"A", "B"})

    def test_altloc_common_atoms_are_included_in_each_conformer(self) -> None:
        reference_atoms = _altloc_reference_atoms(
            per_conformer=self.heavy_atoms,
            common_count=self.common_count,
        )
        with tempfile.TemporaryDirectory() as temporary:
            reference = gemmi.read_structure(
                str(
                    _write_altloc_pose_fixtures(
                        Path(temporary),
                        reference_atoms=reference_atoms,
                        predicted_positions=_linear_chain_positions(self.heavy_atoms),
                        component_id=self.component_id,
                    )[0]
                )
            )
            residue = list(reference[0]["B"])[0]
            self.assertEqual(_residue_altloc_keys(residue), ["A", "B"])
            for altloc in ("A", "B"):
                self.assertEqual(
                    len(_conformer_heavy_atoms(residue, altloc)),
                    self.heavy_atoms,
                )
            conformers = _exact_ligand_conformers(
                reference[0], self.heavy_atoms, self.component_id
            )
        self.assertEqual({row[2] for row in conformers}, {"A", "B"})

    def test_altloc_tries_both_conformers_and_selects_best(self) -> None:
        reference_atoms = _altloc_reference_atoms(
            per_conformer=self.heavy_atoms,
            common_count=self.common_count,
            altloc_y_offset=8.0,
        )
        predicted_positions = [
            (x, y + 0.15, z)
            for x, y, z in _linear_chain_positions(self.heavy_atoms)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            reference_path, prediction_path = _write_altloc_pose_fixtures(
                Path(temporary),
                reference_atoms=reference_atoms,
                predicted_positions=predicted_positions,
                component_id=self.component_id,
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertAlmostEqual(score["rmsd"], 0.15, places=5)
        self.assertEqual(score["reference_ligand_altloc"], "A")
        self.assertNotIn("predicted_ligand_altloc", score)
        fields = _evaluation_fields(score)
        self.assertEqual(fields["reference_ligand_altloc"], "A")
        self.assertNotIn("predicted_ligand_altloc", fields)

    def test_no_altloc_full_scoring_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            positions = _linear_chain_positions(self.heavy_atoms)
            _write_pose_fixture(
                reference_path,
                ligand_positions=positions,
                component_id=self.component_id,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=positions,
                component_id=self.component_id,
                ligand_translation=(0.0, 0.2, 0.0),
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertAlmostEqual(score["rmsd"], 0.2, places=5)
        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL)
        self.assertNotIn("reference_ligand_altloc", score)
        self.assertNotIn("predicted_ligand_altloc", score)


def _write_explicit_bond_mmcif_fixture(
    path: Path,
    *,
    component_id: str,
    positions: list[tuple[float, float, float]],
    bonds: list[tuple[str, str]],
    atom_names: list[str] | None = None,
    ligand_chain: str = "B",
) -> None:
    structure = gemmi.Structure()
    structure.add_model(gemmi.Model(0))
    model = structure[0]
    _add_protein_chain(model)
    chain = gemmi.Chain(ligand_chain)
    residue = gemmi.Residue()
    residue.name = component_id
    residue.seqid.num = 1
    for index, (x, y, z) in enumerate(positions):
        atom = gemmi.Atom()
        atom.name = atom_names[index] if atom_names else f"C{index + 1:02d}"
        atom.element = gemmi.Element("C")
        atom.pos = gemmi.Position(x, y, z)
        residue.add_atom(atom)
    chain.add_residue(residue)
    model.add_chain(chain)
    document = structure.make_mmcif_document()
    bond_loop = document.sole_block().init_loop(
        "_chem_comp_bond.",
        ["comp_id", "atom_id_1", "atom_id_2"],
    )
    for left, right in bonds:
        bond_loop.add_row([component_id, left, right])
    document.write_file(str(path))


def _four_cycle_coords() -> list[tuple[float, float, float]]:
    return [
        (0.0, 0.0, 0.0),
        (1.54, 0.0, 0.0),
        (1.54, 1.54, 0.0),
        (0.0, 1.54, 0.0),
    ]


def _four_chain_coords() -> list[tuple[float, float, float]]:
    return [(index * 1.54, 0.0, 0.0) for index in range(4)]


def _four_cycle_bonds(prefix: str) -> list[tuple[str, str]]:
    labels = [f"{prefix}{index + 1:02d}" for index in range(4)]
    return [
        (labels[0], labels[1]),
        (labels[1], labels[2]),
        (labels[2], labels[3]),
        (labels[3], labels[0]),
    ]


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class ExplicitBondFallbackTests(unittest.TestCase):
    reference_component = "SVR"
    predicted_component = "LIG0"
    heavy_atoms = 4

    def setUp(self) -> None:
        try:
            from rdkit import Chem  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("RDKit is an optional evaluation dependency")

    def test_explicit_fallback_scores_when_inferred_graphs_differ(self) -> None:
        cycle_bonds = _four_cycle_bonds("C")
        predicted_bonds = _four_cycle_bonds("L")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            predicted_positions = [
                (x, y + 0.25, z) for x, y, z in _four_cycle_coords()
            ]
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=predicted_positions,
                bonds=predicted_bonds,
                atom_names=["L01", "L02", "L03", "L04"],
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.reference_component,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertEqual(score["evaluator_version"], EVALUATOR_VERSION)
        self.assertGreater(score["rmsd"], 0.0)
        self.assertEqual(
            score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL_EXPLICIT
        )
        self.assertEqual(score["ligand_topology_source"], TOPOLOGY_SOURCE_EXPLICIT)
        self.assertEqual(len(score["symmetry_mapping"]), self.heavy_atoms)
        fields = _evaluation_fields(score)
        self.assertEqual(fields["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL_EXPLICIT)

    def test_explicit_fallback_preserves_graph_symmetry_mappings(self) -> None:
        from rdkit import Chem
        from rdkit.Chem import rdDetermineBonds

        cycle_bonds = _four_cycle_bonds("C")
        predicted_bonds = _four_cycle_bonds("L")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=_four_cycle_coords(),
                bonds=predicted_bonds,
                atom_names=["L01", "L02", "L03", "L04"],
            )
            reference_atoms = list(gemmi.read_structure(str(reference_path))[0]["B"][0])
            predicted_atoms = list(gemmi.read_structure(str(prediction_path))[0]["B"][0])
            resolved = _resolve_ligand_mappings(
                predicted_atoms=predicted_atoms,
                reference_atoms=reference_atoms,
                coordinate_path_predicted=prediction_path,
                coordinate_path_reference=reference_path,
                predicted_component_id=self.predicted_component,
                reference_component_id=self.reference_component,
                reference_mode="full",
                gemmi=gemmi,
                Chem=Chem,
                rdDetermineBonds=rdDetermineBonds,
            )

        self.assertIsNotNone(resolved)
        mappings, policy, topology_source = resolved
        self.assertEqual(policy, LIGAND_MAPPING_POLICY_FULL_EXPLICIT)
        self.assertEqual(topology_source, TOPOLOGY_SOURCE_EXPLICIT)
        self.assertEqual(len(mappings), 8)

    def test_one_sided_explicit_bond_table_fails_closed(self) -> None:
        cycle_bonds = _four_cycle_bonds("C")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.pdb"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_four_cycle_coords(),
                component_id=self.predicted_component,
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_malformed_explicit_bond_table_fails_closed(self) -> None:
        cycle_bonds = _four_cycle_bonds("C")
        predicted_bonds = _four_cycle_bonds("L")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            structure = gemmi.Structure()
            structure.add_model(gemmi.Model(0))
            model = structure[0]
            _add_protein_chain(model)
            chain = gemmi.Chain("B")
            residue = gemmi.Residue()
            residue.name = self.reference_component
            residue.seqid.num = 1
            for index, (x, y, z) in enumerate(_four_chain_coords()):
                atom = gemmi.Atom()
                atom.name = "C01"
                atom.element = gemmi.Element("C")
                atom.pos = gemmi.Position(x, y, z)
                residue.add_atom(atom)
            chain.add_residue(residue)
            model.add_chain(chain)
            document = structure.make_mmcif_document()
            bond_loop = document.sole_block().init_loop(
                "_chem_comp_bond.",
                ["comp_id", "atom_id_1", "atom_id_2"],
            )
            for left, right in cycle_bonds:
                bond_loop.add_row([self.reference_component, left, right])
            document.write_file(str(reference_path))
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=_four_cycle_coords(),
                bonds=predicted_bonds,
                atom_names=["L01", "L02", "L03", "L04"],
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_incomplete_explicit_bond_table_fails_closed(self) -> None:
        partial_bonds = [("C01", "C02"), ("C03", "C04")]
        predicted_bonds = _four_cycle_bonds("L")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=partial_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=_four_cycle_coords(),
                bonds=predicted_bonds,
                atom_names=["L01", "L02", "L03", "L04"],
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_non_isomorphic_explicit_tables_fail_closed(self) -> None:
        chain_bonds = [("C01", "C02"), ("C02", "C03"), ("C03", "C04")]
        cycle_bonds = _four_cycle_bonds("L")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=chain_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=_four_cycle_coords(),
                bonds=cycle_bonds,
                atom_names=["L01", "L02", "L03", "L04"],
            )

            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_partial_explicit_fallback_when_inferred_partial_fails(self) -> None:
        heavy_atoms = 5
        reference_positions = [(index * 1.54, 0.0, 0.0) for index in range(4)]
        predicted_positions = _four_cycle_coords() + [(7.7, 0.0, 0.0)]
        reference_bonds = [("C01", "C02"), ("C02", "C03"), ("C03", "C04")]
        predicted_bonds = [
            ("L01", "L02"),
            ("L02", "L03"),
            ("L03", "L04"),
            ("L04", "L01"),
            ("L04", "L05"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=reference_positions,
                bonds=reference_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=predicted_positions,
                bonds=predicted_bonds,
                atom_names=["L01", "L02", "L03", "L04", "L05"],
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.reference_component,
                heavy_atoms=heavy_atoms,
            )

        self.assertEqual(
            score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL_EXPLICIT
        )
        self.assertEqual(score["ligand_topology_source"], TOPOLOGY_SOURCE_EXPLICIT)
        self.assertEqual(score["reference_heavy_atoms_observed"], 4)
        self.assertEqual(score["reference_heavy_atoms_scored"], 4)
        self.assertAlmostEqual(score["reference_coverage"], 0.8)

    def test_pdb_without_bond_tables_keeps_inferred_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            positions = _linear_chain_positions(self.heavy_atoms)
            _write_pose_fixture(
                reference_path,
                ligand_positions=positions,
                component_id=self.reference_component,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=positions,
                component_id=self.predicted_component,
                ligand_translation=(0.0, 0.2, 0.0),
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.reference_component,
                heavy_atoms=self.heavy_atoms,
            )

        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL)
        self.assertEqual(score["ligand_topology_source"], TOPOLOGY_SOURCE_INFERRED)
        self.assertIsNone(
            _chem_comp_bond_edges(reference_path, self.reference_component, gemmi)
        )

    def test_explicit_helper_rejects_duplicate_atom_names(self) -> None:
        from rdkit import Chem

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "duplicate.cif"
            structure = gemmi.Structure()
            structure.add_model(gemmi.Model(0))
            model = structure[0]
            _add_protein_chain(model)
            chain = gemmi.Chain("B")
            residue = gemmi.Residue()
            residue.name = "LIG"
            residue.seqid.num = 1
            for index in range(2):
                atom = gemmi.Atom()
                atom.name = "C01"
                atom.element = gemmi.Element("C")
                atom.pos = gemmi.Position(index * 1.54, 0.0, 0.0)
                residue.add_atom(atom)
            chain.add_residue(residue)
            model.add_chain(chain)
            document = structure.make_mmcif_document()
            bond_loop = document.sole_block().init_loop(
                "_chem_comp_bond.",
                ["comp_id", "atom_id_1", "atom_id_2"],
            )
            bond_loop.add_row(["LIG", "C01", "C01"])
            document.write_file(str(path))
            atoms = list(gemmi.read_structure(str(path))[0]["B"][0])
            edges = _chem_comp_bond_edges(path, "LIG", gemmi)
            molecule = _explicit_connectivity_molecule_from_atoms(
                atoms,
                edges or frozenset(),
                Chem,
                require_connected=True,
            )

        self.assertIsNone(molecule)


TASK_SMILES_CYCLE = "C1CCC1"


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class TaskSmilesFallbackTests(unittest.TestCase):
    reference_component = "SVR"
    predicted_component = "LIG0"
    heavy_atoms = 4
    task_smiles = TASK_SMILES_CYCLE

    def setUp(self) -> None:
        try:
            from rdkit import Chem  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("RDKit is an optional evaluation dependency")

    def test_bondless_prediction_with_task_smiles_and_explicit_reference_succeeds(
        self,
    ) -> None:
        cycle_bonds = _four_cycle_bonds("C")
        square = _four_cycle_coords()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=square,
                bonds=[],
                atom_names=["L01", "L02", "L03", "L04"],
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.reference_component,
                heavy_atoms=self.heavy_atoms,
                ligand_smiles=self.task_smiles,
            )

        self.assertEqual(score["evaluator_version"], EVALUATOR_VERSION)
        self.assertEqual(
            score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL_TASK_SMILES
        )
        self.assertEqual(score["ligand_topology_source"], TOPOLOGY_SOURCE_TASK_SMILES)
        self.assertGreater(score["rmsd"], 0.0)
        self.assertEqual(len(score["symmetry_mapping"]), self.heavy_atoms)

    def test_prediction_element_order_mismatch_rejects_task_smiles_fallback(self) -> None:
        from rdkit import Chem

        task_smiles = "CNCO"
        heavy_atoms = 4
        cycle_bonds = [("C01", "C02"), ("C02", "C03"), ("C03", "C04")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            structure = gemmi.Structure()
            structure.add_model(gemmi.Model(0))
            model = structure[0]
            _add_protein_chain(model)
            chain = gemmi.Chain("B")
            residue = gemmi.Residue()
            residue.name = self.predicted_component
            residue.seqid.num = 1
            elements = ["N", "C", "O", "C"]
            for index, symbol in enumerate(elements):
                atom = gemmi.Atom()
                atom.name = f"L{index + 1:02d}"
                atom.element = gemmi.Element(symbol)
                x, y, z = _four_cycle_coords()[index]
                atom.pos = gemmi.Position(x, y, z)
                residue.add_atom(atom)
            chain.add_residue(residue)
            model.add_chain(chain)
            structure.make_mmcif_document().write_file(str(prediction_path))
            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=heavy_atoms,
                    ligand_smiles=task_smiles,
                )
        source = Chem.RemoveHs(Chem.MolFromSmiles(task_smiles))
        expected = [atom.GetAtomicNum() for atom in source.GetAtoms()]
        observed = [7, 6, 8, 6]
        self.assertNotEqual(observed, expected)

    def test_missing_task_smiles_cannot_invoke_fallback(self) -> None:
        cycle_bonds = _four_cycle_bonds("C")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=_four_chain_coords(),
                bonds=cycle_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=_four_cycle_coords(),
                bonds=[],
                atom_names=["L01", "L02", "L03", "L04"],
            )
            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_reference_without_explicit_bonds_rejects_when_inferred_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            _write_pose_fixture(
                reference_path,
                ligand_positions=_four_chain_coords(),
                component_id=self.reference_component,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_four_cycle_coords(),
                component_id=self.predicted_component,
            )
            with self.assertRaisesRegex(
                EvaluationError,
                "no compatible receptor/ligand mapping could be evaluated",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.reference_component,
                    heavy_atoms=self.heavy_atoms,
                    ligand_smiles=self.task_smiles,
                )

    def test_partial_task_smiles_fallback_preserves_pendant_safeguard(self) -> None:
        heavy_atoms = 5
        task_smiles = "CCCCC"
        reference_positions = _four_cycle_coords()
        predicted_positions = [(index * 1.54, 0.0, 0.0) for index in range(5)]
        reference_bonds = [("C01", "C02"), ("C02", "C03"), ("C03", "C04")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.cif"
            prediction_path = root / "prediction.cif"
            _write_explicit_bond_mmcif_fixture(
                reference_path,
                component_id=self.reference_component,
                positions=reference_positions,
                bonds=reference_bonds,
            )
            _write_explicit_bond_mmcif_fixture(
                prediction_path,
                component_id=self.predicted_component,
                positions=predicted_positions,
                bonds=[],
                atom_names=["L01", "L02", "L03", "L04", "L05"],
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.reference_component,
                heavy_atoms=heavy_atoms,
                ligand_smiles=task_smiles,
            )

        self.assertEqual(
            score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES
        )
        self.assertEqual(score["reference_heavy_atoms_observed"], 4)
        self.assertEqual(score["reference_heavy_atoms_scored"], 4)

    def test_task_smiles_helper_requires_exact_element_order(self) -> None:
        from rdkit import Chem

        atoms = []
        for index, symbol in enumerate(["N", "C", "O", "C"]):
            atom = gemmi.Atom()
            atom.name = f"L{index + 1:02d}"
            atom.element = gemmi.Element(symbol)
            x, y, z = _four_cycle_coords()[index]
            atom.pos = gemmi.Position(x, y, z)
            atoms.append(atom)
        molecule = _task_smiles_connectivity_molecule_from_atoms(
            atoms,
            "CNCO",
            4,
            Chem,
        )
        self.assertIsNone(molecule)


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class ReferencePocketExportTests(unittest.TestCase):
    def test_extract_reference_pocket_pdb_keeps_complete_near_residues_only(self) -> None:
        from foldarium_pipeline.evaluation import extract_reference_pocket_pdb

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            structure = gemmi.Structure()
            structure.add_model(gemmi.Model(0))
            model = structure[0]
            chain = gemmi.Chain("A")
            near_residue = gemmi.Residue()
            near_residue.name = "ALA"
            near_residue.seqid.num = 1
            near_atom = gemmi.Atom()
            near_atom.name = "CA"
            near_atom.element = gemmi.Element("C")
            near_atom.pos = gemmi.Position(1.0, 2.0, 3.0)
            near_residue.add_atom(near_atom)
            far_side_atom = gemmi.Atom()
            far_side_atom.name = "CB"
            far_side_atom.element = gemmi.Element("C")
            far_side_atom.pos = gemmi.Position(20.0, 20.0, 20.0)
            near_residue.add_atom(far_side_atom)
            chain.add_residue(near_residue)
            far_residue = gemmi.Residue()
            far_residue.name = "ALA"
            far_residue.seqid.num = 2
            far_atom = gemmi.Atom()
            far_atom.name = "CA"
            far_atom.element = gemmi.Element("C")
            far_atom.pos = gemmi.Position(50.0, 50.0, 50.0)
            far_residue.add_atom(far_atom)
            chain.add_residue(far_residue)
            model.add_chain(chain)
            structure.write_minimal_pdb(str(reference_path))
            pocket = extract_reference_pocket_pdb(
                reference_path,
                {
                    "reference_ligand_coordinates": [[1.1, 2.1, 3.1]],
                },
            )
        self.assertIn("ALA A   1", pocket)
        self.assertIn("CB", pocket)
        self.assertNotIn("ALA A   2", pocket)
        self.assertTrue(pocket.endswith("\nEND\n"))

    def test_extract_reference_pocket_pdb_includes_all_contacting_polymer_chains(self) -> None:
        from foldarium_pipeline.evaluation import extract_reference_pocket_pdb

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            structure = gemmi.Structure()
            structure.add_model(gemmi.Model(0))
            model = structure[0]
            for chain_name, position in (("A", (1.0, 2.0, 3.0)), ("B", (1.2, 2.2, 3.2))):
                chain = gemmi.Chain(chain_name)
                residue = gemmi.Residue()
                residue.name = "ALA"
                residue.seqid.num = 1
                atom = gemmi.Atom()
                atom.name = "CA"
                atom.element = gemmi.Element("C")
                atom.pos = gemmi.Position(*position)
                residue.add_atom(atom)
                chain.add_residue(residue)
                model.add_chain(chain)
            structure.write_minimal_pdb(str(reference_path))
            pocket = extract_reference_pocket_pdb(
                reference_path,
                {
                    "reference_ligand_coordinates": [[1.1, 2.1, 3.1]],
                },
            )
        self.assertIn("ALA A   1", pocket)
        self.assertIn("ALA B   1", pocket)

    def test_evaluate_ligand_pose_emits_reference_pocket_pdb(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            positions = [(1.1, 2.1, 3.1)]
            _write_pose_fixture(
                reference_path,
                ligand_positions=positions,
                component_id="DRG",
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=positions,
                component_id="DRG",
            )
            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id="DRG",
                heavy_atoms=1,
            )
        self.assertIn("reference_pocket_pdb", score)
        self.assertTrue(score["reference_pocket_pdb"].endswith("\nEND\n"))
        fields = _evaluation_fields(score)
        self.assertNotIn("reference_pocket_pdb", fields)


@unittest.skipUnless(HAS_GEMMI, "Gemmi is an optional evaluation dependency")
class ReleasedPartialReferenceOverrideTests(unittest.TestCase):
    component_id = "AAO"
    heavy_atoms = 66
    minimum_observed = 52

    def setUp(self) -> None:
        try:
            from rdkit import Chem  # noqa: F401
        except (ImportError, ModuleNotFoundError):
            self.skipTest("RDKit is an optional evaluation dependency")

    def test_default_threshold_rejects_52_of_66_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            observed = self.minimum_observed
            self.assertLess(observed / self.heavy_atoms, PARTIAL_REFERENCE_COVERAGE_MIN)
            _write_pose_fixture(
                reference_path,
                ligand_positions=_linear_chain_positions(observed),
                component_id=self.component_id,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
                component_id=self.component_id,
            )

            with self.assertRaisesRegex(
                EvaluationError,
                f"reference contains no {self.component_id} ligand with {self.heavy_atoms} atoms",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                )

    def test_override_accepts_52_of_66(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            _write_pose_fixture(
                reference_path,
                ligand_positions=_linear_chain_positions(self.minimum_observed),
                component_id=self.component_id,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
                component_id=self.component_id,
                ligand_translation=(0.0, 0.2, 0.0),
            )

            score = evaluate_ligand_pose(
                reference_path,
                prediction_path,
                component_id=self.component_id,
                heavy_atoms=self.heavy_atoms,
                minimum_reference_heavy_atoms=self.minimum_observed,
            )

        self.assertAlmostEqual(score["rmsd"], 0.2, places=5)
        self.assertEqual(score["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL)
        self.assertEqual(score["reference_heavy_atoms_observed"], self.minimum_observed)
        self.assertEqual(
            score["reference_heavy_atoms_minimum_observed"], self.minimum_observed
        )
        self.assertEqual(
            score["released_partial_reference_override_policy"],
            RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
        )
        self.assertLess(score["reference_coverage"], PARTIAL_REFERENCE_COVERAGE_MIN)

    def test_override_rejects_one_atom_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.pdb"
            prediction_path = root / "prediction.pdb"
            observed = self.minimum_observed - 1
            _write_pose_fixture(
                reference_path,
                ligand_positions=_linear_chain_positions(observed),
                component_id=self.component_id,
            )
            _write_pose_fixture(
                prediction_path,
                ligand_positions=_linear_chain_positions(self.heavy_atoms),
                component_id=self.component_id,
            )

            with self.assertRaisesRegex(
                EvaluationError,
                f"reference contains no {self.component_id} ligand with {self.heavy_atoms} atoms",
            ):
                evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=self.component_id,
                    heavy_atoms=self.heavy_atoms,
                    minimum_reference_heavy_atoms=self.minimum_observed,
                )

    def test_released_partial_reference_override_binding_mismatch_raises(self) -> None:
        with self.assertRaisesRegex(
            EvaluationError,
            "heavy_atoms binding mismatch",
        ):
            released_partial_reference_override_for_item(
                "weekly-2026-08-22-beta-v1",
                "26WD",
                target_id="26WD",
                component_id="AAO",
                heavy_atoms=65,
            )

    def test_override_audit_fields_propagate_through_evaluation_fields(self) -> None:
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "reference_heavy_atoms_expected": self.heavy_atoms,
                "reference_heavy_atoms_observed": self.minimum_observed,
                "reference_heavy_atoms_scored": self.minimum_observed,
                "reference_heavy_atoms_minimum_observed": self.minimum_observed,
                "reference_coverage": self.minimum_observed / self.heavy_atoms,
                "ligand_mapping_policy": LIGAND_MAPPING_POLICY_PARTIAL,
                "released_partial_reference_override_policy": (
                    RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY
                ),
            }
        )
        self.assertEqual(
            fields["reference_heavy_atoms_minimum_observed"], self.minimum_observed
        )
        self.assertEqual(
            fields["released_partial_reference_override_policy"],
            RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
        )


if __name__ == "__main__":
    unittest.main()
