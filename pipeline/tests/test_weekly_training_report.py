from __future__ import annotations

import unittest

from foldarium_pipeline.weekly_training_audit import AUDIT_FORMAT
from foldarium_pipeline.weekly_training_report import build_report, roc_auc


def _audit(mode: str, records: list[dict]) -> dict:
    return {
        "format_version": AUDIT_FORMAT,
        "mode": mode,
        "records": records,
    }


class WeeklyTrainingReportTests(unittest.TestCase):
    def test_roc_auc_handles_ties_and_missing_classes(self) -> None:
        self.assertEqual(roc_auc([(True, 1.0), (False, 0.0)]), 1.0)
        self.assertEqual(roc_auc([(True, 0.5), (False, 0.5)]), 0.5)
        self.assertIsNone(roc_auc([(True, 1.0)]))

    def test_report_compares_blind_methods_with_revealed_labels(self) -> None:
        exact = _audit(
            "exact",
            [
                {
                    "status": "complete",
                    "item_id": "1ABC",
                    "blind_week": "2026-01-01",
                    "classification": "familiar",
                    "correct_choice_ids": ["pose-a"],
                    "has_correct_pose": True,
                    "automated_correct": {"Method": True},
                    "train_pdb": "2ABC",
                    "train_shape_overlap": 0.8,
                },
                {
                    "status": "complete",
                    "item_id": "2DEF",
                    "blind_week": "2026-01-01",
                    "classification": "novel",
                    "correct_choice_ids": [],
                    "has_correct_pose": False,
                    "automated_correct": {"Method": False},
                    "train_pdb": "3DEF",
                    "train_shape_overlap": 0.1,
                },
            ],
        )
        blind = _audit(
            "blind",
            [
                {
                    "status": "complete",
                    "item_id": "1ABC",
                    "nearest_training_system": {
                        "classification": "familiar",
                        "score": 0.7,
                        "choice_id": "pose-a",
                        "predict_none": False,
                    },
                    "pocket_aware": {
                        "classification": "familiar",
                        "score": 0.8,
                        "choice_id": "pose-a",
                        "predict_none": False,
                    },
                },
                {
                    "status": "complete",
                    "item_id": "2DEF",
                    "nearest_training_system": {
                        "classification": "novel",
                        "score": 0.1,
                        "choice_id": "pose-b",
                        "predict_none": True,
                    },
                    "pocket_aware": {
                        "classification": "novel",
                        "score": 0.2,
                        "choice_id": "pose-b",
                        "predict_none": True,
                    },
                },
            ],
        )
        report = build_report(exact, blind)

        self.assertEqual(report["counts"]["classification"], {"familiar": 1, "novel": 1})
        nearest = report["blind_estimators"]["nearest_training_system"]
        self.assertEqual(nearest["classification_accuracy"], 1.0)
        self.assertEqual(nearest["auroc"], 1.0)
        self.assertEqual(nearest["correct_pose_pick_rate"], 0.5)
        self.assertEqual(nearest["pose_or_none_accuracy"], 1.0)
        self.assertEqual(
            report["automated_correctness"]["Method"]["all"]["correct_rate"],
            0.5,
        )


if __name__ == "__main__":
    unittest.main()
