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
                    "pose_uri": "supabase://bucket/of3-1.pdb",
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "pose_uri": "supabase://bucket/boltz-1.pdb",
                },
            ],
        }
    ]


class BlindManifestTests(unittest.TestCase):
    def test_empty_round_is_rejected(self) -> None:
        with self.assertRaisesRegex(QuizManifestError, "at least one item"):
            build_blind_manifest("2026-08-08", [])

    def test_removes_method_and_run_identity(self) -> None:
        blind, private = build_blind_manifest("week-2026-08-08", source_items())
        encoded = str(blind)
        self.assertNotIn("openfold3", encoded)
        self.assertNotIn("boltz2", encoded)
        self.assertNotIn("run-of3", encoded)
        self.assertEqual(len(blind["items"][0]["choices"]), 2)
        self.assertEqual(private["blind_manifest_sha256"], manifest_sha256(blind))
        self.assertIn("method", private["items"][0]["choices"][0])

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


class RevealManifestTests(unittest.TestCase):
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
