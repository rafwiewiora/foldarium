from __future__ import annotations

import math
import unittest

from foldarium_pipeline.evaluation import (
    EvaluationError,
    best_receptor_superposition,
    exact_complex_receptor_superposition,
    exact_complex_tm_superposition,
    _receptor_candidate_key,
    _robust_sequence_superposition,
    _sequence_superposition,
)

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


if __name__ == "__main__":
    unittest.main()
