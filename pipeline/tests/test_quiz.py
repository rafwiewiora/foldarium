from __future__ import annotations

import unittest

from foldarium_pipeline.quiz import (
    QuizManifestError,
    build_blind_manifest,
    build_reveal_manifest,
    manifest_sha256,
)


def source_items() -> list[dict]:
    return [
        {
            "id": "target-1",
            "target_id": "cameo-target-1",
            "ligand": "DRG",
            "week": "2026-08-08",
            "protein_uri": "supabase://bucket/protein.pdb",
            "choices": [
                {
                    "run_id": "run-of3",
                    "sample_id": "sample-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "confidence": {
                        "metric": "ligand_plddt",
                        "value": 82.5,
                        "scale_min": 0,
                        "scale_max": 100,
                        "aggregation": "arithmetic-mean-selected-ligand-heavy-atoms",
                    },
                    "smina_score": {
                        "metric": "smina_affinity",
                        "value": -7.1,
                        "units": "kcal/mol",
                        "protocol": "score_only",
                        "scoring_function": "vina",
                    },
                    "interaction_count": {
                        "metric": "prolif_hbond_residue_count",
                        "value": 9,
                        "policy": "prolif-implicit-hbond-unique-protein-residue/v1",
                    },
                    "pose_uri": "supabase://bucket/of3-1.pdb",
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "confidence": {
                        "metric": "ligand_plddt",
                        "value": 90.0,
                        "scale_min": 0,
                        "scale_max": 100,
                        "aggregation": "arithmetic-mean-selected-ligand-heavy-atoms",
                    },
                    "smina_score": {
                        "metric": "smina_affinity",
                        "value": -6.3,
                        "units": "kcal/mol",
                        "protocol": "score_only",
                        "scoring_function": "vina",
                    },
                    "interaction_count": {
                        "metric": "prolif_hbond_residue_count",
                        "value": 7,
                        "policy": "prolif-implicit-hbond-unique-protein-residue/v1",
                    },
                    "pose_uri": "supabase://bucket/boltz-1.pdb",
                },
            ],
        }
    ]


class BlindManifestTests(unittest.TestCase):
    def test_empty_round_is_rejected(self) -> None:
        with self.assertRaisesRegex(QuizManifestError, "at least one item"):
            build_blind_manifest("2026-08-08", [])

    def test_exposes_method_and_confidence_but_removes_run_identity(self) -> None:
        blind, private = build_blind_manifest("week-2026-08-08", source_items())
        encoded = str(blind)
        self.assertIn("openfold3", encoded)
        self.assertIn("boltz2", encoded)
        self.assertIn("ligand_plddt", encoded)
        self.assertIn("smina_affinity", encoded)
        self.assertIn("prolif_hbond_residue_count", encoded)
        self.assertNotIn("run-of3", encoded)
        self.assertEqual(len(blind["items"][0]["choices"]), 2)
        self.assertEqual(private["blind_manifest_sha256"], manifest_sha256(blind))
        self.assertIn("method", private["items"][0]["choices"][0])

    def test_choice_id_is_stable_when_public_confidence_changes(self) -> None:
        items = source_items()
        blind_before, _ = build_blind_manifest("week", items)
        items[0]["choices"][0]["confidence"]["value"] = 75.0
        blind_after, _ = build_blind_manifest("week", items)

        self.assertEqual(
            {choice["id"] for choice in blind_before["items"][0]["choices"]},
            {choice["id"] for choice in blind_after["items"][0]["choices"]},
        )

    def test_confidence_must_lie_on_its_declared_scale(self) -> None:
        items = source_items()
        items[0]["choices"][0]["confidence"]["value"] = 101.0
        with self.assertRaisesRegex(QuizManifestError, "outside its scale"):
            build_blind_manifest("week", items)

    def test_public_confidence_requires_method_provenance(self) -> None:
        items = source_items()
        del items[0]["choices"][0]["method"]
        del items[0]["choices"][0]["method_version"]
        with self.assertRaisesRegex(QuizManifestError, "confidence requires choice.method"):
            build_blind_manifest("week", items)

    def test_pose_metrics_are_strictly_validated(self) -> None:
        items = source_items()
        items[0]["choices"][0]["smina_score"]["value"] = float("nan")
        with self.assertRaisesRegex(QuizManifestError, "smina_score is invalid"):
            build_blind_manifest("week", items)

        items = source_items()
        items[0]["choices"][0]["interaction_count"]["value"] = -1
        with self.assertRaisesRegex(QuizManifestError, "interaction_count is invalid"):
            build_blind_manifest("week", items)

    def test_is_replay_deterministic(self) -> None:
        self.assertEqual(
            build_blind_manifest("week-2026-08-08", source_items()),
            build_blind_manifest("week-2026-08-08", source_items()),
        )

    def test_choice_requires_a_pose(self) -> None:
        items = source_items()
        del items[0]["choices"][0]["pose_uri"]
        with self.assertRaises(QuizManifestError):
            build_blind_manifest("week", items)

    def test_exposes_only_opaque_cluster_assignments_and_keeps_private_audit(self) -> None:
        items = source_items()
        items[0]["clustering"] = {
            "version": "cluster/v1",
            "threshold_angstrom": 2.0,
            "distance_matrix_sha256": "a" * 64,
        }
        for index, choice in enumerate(items[0]["choices"]):
            choice["cluster_id"] = "cluster_opaque"
            choice["is_rep"] = index == 0
            choice["protein_uri"] = f"supabase://bucket/protein-{index}.pdb"
            choice["pocket_uri"] = f"supabase://bucket/pocket-{index}.pdb"

        blind, private = build_blind_manifest("week", items)

        self.assertEqual(len(blind["items"][0]["choices"]), 2)
        self.assertNotIn("clustering", blind["items"][0])
        self.assertEqual(private["items"][0]["clustering"], items[0]["clustering"])
        for choice in blind["items"][0]["choices"]:
            self.assertEqual(choice["cluster_id"], "cluster_opaque")
            self.assertIsInstance(choice["is_rep"], bool)
            self.assertIn("protein_uri", choice)
            self.assertIn("pocket_uri", choice)

    def test_cluster_representative_flag_requires_an_opaque_cluster_id(self) -> None:
        items = source_items()
        items[0]["choices"][0]["is_rep"] = True
        with self.assertRaisesRegex(QuizManifestError, "requires choice.cluster_id"):
            build_blind_manifest("week", items)


class RevealManifestTests(unittest.TestCase):
    def test_cluster_metadata_never_collapses_wednesday_raw_choice_ids(self) -> None:
        item = source_items()[0]
        template = item["choices"][0]
        item["choices"] = [
            {
                **template,
                "run_id": f"run-{index}",
                "sample_id": f"sample-{index}",
                "pose_uri": f"supabase://bucket/pose-{index}.pdb",
                "cluster_id": "cluster_opaque",
                "is_rep": index == 4,
            }
            for index in range(10)
        ]
        blind, _private = build_blind_manifest("week", [item])
        choices = [
            {"id": choice["id"], "rmsd": 1.0, "correct": True}
            for choice in blind["items"][0]["choices"]
        ]
        reveal = build_reveal_manifest(
            blind, [{"id": item["id"], "choices": choices}]
        )
        self.assertEqual(len(blind["items"][0]["choices"]), 10)
        self.assertEqual(len(reveal["items"][0]["choices"]), 10)

    def test_reveal_requires_every_blind_choice(self) -> None:
        blind, _private = build_blind_manifest("week-2026-08-08", source_items())
        choices = [
            {"id": choice["id"], "rmsd": index + 0.5, "correct": index == 0}
            for index, choice in enumerate(blind["items"][0]["choices"])
        ]
        reveal = build_reveal_manifest(
            blind, [{"id": "target-1", "choices": choices}]
        )
        self.assertEqual(reveal["blind_manifest_sha256"], manifest_sha256(blind))
        self.assertEqual(len(reveal["items"][0]["choices"]), 2)

    def test_missing_choice_fails_closed(self) -> None:
        blind, _private = build_blind_manifest("week-2026-08-08", source_items())
        choice = blind["items"][0]["choices"][0]
        with self.assertRaises(QuizManifestError):
            build_reveal_manifest(
                blind,
                [
                    {
                        "id": "target-1",
                        "choices": [{"id": choice["id"], "rmsd": 1.0, "correct": True}],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
