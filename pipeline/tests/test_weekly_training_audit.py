from __future__ import annotations

import unittest
from dataclasses import asdict
from unittest import mock

from foldarium_pipeline.weekly_training_audit import (
    BlindTarget,
    WeeklyTrainingAuditError,
    load_all_targets,
    targets_from_detail,
)


def _detail() -> dict:
    reference = "https://files.rcsb.org/download/1ABC.cif.gz"
    return {
        "format_version": "foldarium.weekly-retrospective-detail/v1",
        "round": {
            "round_id": "weekly-test-v1",
            "blind_week": "2026-01-01",
            "item_count": 1,
        },
        "blind_manifest": {
            "items": [
                {
                    "id": "1ABC",
                    "ligand": {"component_id": "DRG"},
                    "protein_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                    + "a" * 64,
                    "pocket_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                    + "b" * 64,
                    "choices": [
                        {
                            "id": "pose-1",
                            "pose_uri": "https://example.supabase.co/storage/v1/object/public/bucket/"
                            + "c" * 64,
                        }
                    ],
                }
            ]
        },
        "reveal_manifest": {
            "items": [
                {
                    "id": "1ABC",
                    "choices": [
                        {
                            "id": "pose-1",
                            "correct": True,
                            "reference_uri": reference,
                        }
                    ],
                }
            ]
        },
        "answer_overlays": [
            {
                "item_id": "1ABC",
                "crystal_ligand_pdb": (
                    "HETATM    1 C1   LIG X   1       0.000   0.000   0.000"
                    "  1.00  0.00           C\nEND\n"
                ),
            }
        ],
        "retrospective": {
            "questions": [
                {
                    "item_id": "1ABC",
                    "automated_entries": [
                        {"participant": "Boltz-2", "correct": True}
                    ],
                }
            ]
        },
    }


class WeeklyTrainingAuditContractTests(unittest.TestCase):
    def test_full_audit_requires_exactly_three_publications(self) -> None:
        with mock.patch(
            "foldarium_pipeline.weekly_training_audit.load_publications",
            return_value=[],
        ):
            with self.assertRaisesRegex(WeeklyTrainingAuditError, "3 published rounds"):
                load_all_targets("https://www.foldarium.org", "/tmp/cache")

    def test_blind_target_contains_no_reveal_side_fields(self) -> None:
        blind, exact = targets_from_detail(_detail())
        self.assertEqual(len(blind), 1)
        self.assertEqual(len(exact), 1)
        blind_payload = asdict(blind[0])
        self.assertEqual(
            set(blind_payload),
            {
                "round_id",
                "blind_week",
                "item_id",
                "ligand_component_id",
                "protein_uri",
                "pocket_uri",
                "choices",
            },
        )
        serialized = repr(blind_payload)
        for forbidden in ("correct", "reference_uri", "crystal", "rmsd", "overlay"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_reference_must_match_the_item_pdb(self) -> None:
        detail = _detail()
        detail["reveal_manifest"]["items"][0]["choices"][0][
            "reference_uri"
        ] = "https://files.rcsb.org/download/2DEF.cif.gz"
        with self.assertRaisesRegex(WeeklyTrainingAuditError, "reference URI"):
            targets_from_detail(detail)

    def test_blind_type_rejects_accidental_extra_fields(self) -> None:
        with self.assertRaises(TypeError):
            BlindTarget(
                round_id="round",
                blind_week="2026-01-01",
                item_id="1ABC",
                ligand_component_id="DRG",
                protein_uri="protein",
                pocket_uri="pocket",
                choices=(("pose", "uri"),),
                correct=True,  # type: ignore[call-arg]
            )


if __name__ == "__main__":
    unittest.main()
