from __future__ import annotations

import unittest

from foldarium_pipeline.archive import (
    OFFICIAL_CAMEO_EVALUATOR_VERSION,
    ArchiveError,
    build_archive_candidate,
    parse_official_ligand_score,
    build_static_quiz_item,
    classify_pose_ensemble,
    cluster_poses,
)


def score(rmsd: float, x: float, plddt: float = 70.0) -> dict:
    return {
        "rmsd": rmsd,
        "ligand_plddt": plddt,
        "predicted_ligand_coordinates_reference_order": [
            [x, 0.0, 0.0],
            [x + 1.0, 0.0, 0.0],
        ],
    }


def payload() -> dict:
    target_id = "2026-06-20_00000082"
    return {
        "target": {
            "id": target_id,
            "week_id": "2026-06-20",
            "pdbid": "36IQ",
            "labels_submission_3d": "hard",
        },
        "entities": [
            {
                "id": target_id + "_1",
                "entity_type": "protein",
                "canonical_sequence": "M" + "A" * 97,
            },
            {
                "id": target_id + "_2",
                "entity_type": "non_polymer",
                "component_id": "DM2",
                "smiles": "C" * 16,
                "inchi": "InChI=fixture",
            },
        ],
        "biounits": [{"assembly_id": 1}],
        "predictions": [
            {
                "server_id": "993_3D",
                "model": 1,
                "complex_assembly_id": 1,
            }
        ],
    }


class ClusteringTests(unittest.TestCase):
    def test_greedy_clusters_and_medoids_match_historical_rule(self) -> None:
        coordinates = [score(0, x)["predicted_ligand_coordinates_reference_order"] for x in (0, 1, 5)]
        labels, medoids = cluster_poses(coordinates)
        self.assertEqual(labels, [0, 0, 1])
        self.assertEqual(medoids, {0: 0, 1: 2})

    def test_mismatched_atom_counts_are_rejected(self) -> None:
        with self.assertRaises(ArchiveError):
            cluster_poses([[[0, 0, 0]], [[0, 0, 0], [1, 0, 0]]])


class ClassificationTests(unittest.TestCase):
    def test_gameable_allwrong_and_allcorrect(self) -> None:
        game = classify_pose_ensemble({1: score(1.0, 0), 2: score(4.0, 4), 3: score(1.2, 0.2)})
        wrong = classify_pose_ensemble({1: score(3.0, 0), 2: score(4.0, 1), 3: score(5.0, 2)})
        correct = classify_pose_ensemble({1: score(1.0, 0), 2: score(1.1, 0.1), 3: score(1.2, 0.2)})
        self.assertEqual(game["bucket"], "game-able")
        self.assertEqual(wrong["bucket"], "all-wrong")
        self.assertEqual(correct["bucket"], "all-correct")

    def test_multi_pocket_is_filtered(self) -> None:
        result = classify_pose_ensemble({1: score(1.0, 0), 2: score(4.0, 9), 3: score(1.2, 0.2)})
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "multi-pocket")


class CandidateTests(unittest.TestCase):
    def test_candidate_records_public_provenance(self) -> None:
        candidate = build_archive_candidate(payload())
        self.assertEqual(candidate["pdb_id"], "36IQ")
        self.assertEqual(candidate["component_id"], "DM2")
        self.assertEqual(candidate["heavy_atoms"], 16)
        self.assertEqual(candidate["coordinate_manifest"]["license"], "CC-BY-SA-4.0")

    def test_one_malformed_public_target_is_skipped(self) -> None:
        source = payload()
        source["entities"][0]["canonical_sequence"] = "M(A)"
        self.assertIsNone(build_archive_candidate(source))

    def test_static_item_keeps_current_schema(self) -> None:
        candidate = build_archive_candidate(payload())
        scores = {1: score(1.0, 0, 70), 2: score(4.0, 4, 80), 3: score(1.2, 0.2, 75)}
        classification = classify_pose_ensemble(scores)
        item = build_static_quiz_item(candidate, scores, classification)
        self.assertEqual(item["bucket"], "game-able")
        self.assertEqual(item["plddt_pick_sample"], 2)
        self.assertEqual(item["n_heavy"], 16)
        self.assertIn("evaluator_version", item["provenance"])

    def test_official_cameo_score_uses_historical_copy_median(self) -> None:
        value = {
            "results": {
                "details": {
                    "ligand_pose": {
                        "ligands": {
                            "1.Y.DM2": {
                                "rmsd": 1.0,
                                "atom_count": 16,
                                "model_ligand_rmsd": "C.LIG_C1",
                                "trg_bu": "bu_target_hetero_02.cif.gz",
                                "chain_mapping_rmsd": {"1.A": "A"},
                                "transform": "{{1,0,0,2},{0,1,0,3},{0,0,1,4},{0,0,0,1}}",
                            },
                            "1.Z.DM2": {
                                "rmsd": 5.0,
                                "atom_count": 16,
                                "model_ligand_rmsd": "D.LIG_D1",
                                "trg_bu": "02",
                                "chain_mapping_rmsd": {"1.A": "A"},
                                "transform": "{{1,0,0,2},{0,1,0,3},{0,0,1,4},{0,0,0,1}}",
                            },
                        }
                    }
                }
            }
        }
        parsed = parse_official_ligand_score(value, "DM2")
        self.assertEqual(parsed["rmsd"], 3.0)
        self.assertEqual(parsed["assembly_id"], 2)
        self.assertEqual(parsed["evaluator_version"], OFFICIAL_CAMEO_EVALUATOR_VERSION)
        self.assertEqual(parsed["predicted_ligand_residue"], "LIG_C")
        self.assertEqual(parsed["transform"]["translation"], [2.0, 3.0, 4.0])


if __name__ == "__main__":
    unittest.main()
