from __future__ import annotations

import unittest

from foldarium_pipeline.weekly_training_audit import AUDIT_FORMAT
from foldarium_pipeline.weekly_training_report import (
    build_report,
    pearson_correlation,
    render_markdown,
    roc_auc,
    spearman_correlation,
)


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

    def test_correlations_handle_ties_and_constant_inputs(self) -> None:
        pairs = [(1.0, 4.0), (2.0, 5.0), (2.0, 5.0), (3.0, 6.0)]
        self.assertEqual(pearson_correlation(pairs), 1.0)
        self.assertEqual(spearman_correlation(pairs), 1.0)
        self.assertIsNone(pearson_correlation([(1.0, 2.0), (1.0, 3.0)]))

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

    def test_report_compares_metrics_on_same_pairs_and_recovers_winners(
        self,
    ) -> None:
        exact_rows = []
        blind_rows = []
        fixtures = [
            (
                "1ABC",
                0.8,
                "familiar",
                "P001",
                "L1",
                0.7,
                "familiar",
                "R001",
                "A1",
                "P001",
                "L1",
                "R001",
                "DIFF",
            ),
            (
                "2DEF",
                0.1,
                "novel",
                "P002",
                "L2",
                0.05,
                "novel",
                "R002",
                "A2",
                "MISS",
                "L2",
                "R002",
                "A2",
            ),
            (
                "3GHI",
                0.3,
                "familiar",
                "P003",
                "L3",
                0.2,
                "novel",
                "R003",
                "A3",
                "P003",
                "L3",
                "MISS",
                "A3",
            ),
        ]
        for (
            item_id,
            pocket_score,
            pocket_class,
            pocket_pdb,
            pocket_het,
            rnp_score,
            rnp_class,
            rnp_pdb,
            rnp_het,
            blind_pocket_pdb,
            blind_pocket_het,
            blind_rnp_pdb,
            blind_rnp_het,
        ) in fixtures:
            exact_rows.append(
                {
                    "status": "complete",
                    "item_id": item_id,
                    "blind_week": "2026-01-01",
                    "classification": pocket_class,
                    "train_shape_overlap": pocket_score,
                    "train_pdb": pocket_pdb,
                    "train_het": pocket_het,
                    "correct_choice_ids": ["pose"],
                    "has_correct_pose": True,
                    "automated_correct": {},
                    "rnp_style_top25": {
                        "classification": rnp_class,
                        "sucos_shape_pocket_qcov": rnp_score,
                        "train_pdb": rnp_pdb,
                        "train_het": rnp_het,
                    },
                }
            )
            blind_rows.append(
                {
                    "status": "complete",
                    "item_id": item_id,
                    "nearest_training_system": {
                        "classification": pocket_class,
                        "score": pocket_score,
                        "choice_id": "pose",
                        "predict_none": pocket_class == "novel",
                    },
                    "pocket_aware": {
                        "classification": pocket_class,
                        "score": pocket_score,
                        "choice_id": "pose",
                        "predict_none": pocket_class == "novel",
                    },
                    "rnp_style_top25": {
                        "classification": rnp_class,
                        "score": rnp_score,
                        "choice_id": "pose",
                        "predict_none": rnp_class == "novel",
                    },
                    "choices": [
                        {
                            "choice_id": "pose",
                            "pocket_aware": {
                                "train_pdb": blind_pocket_pdb,
                                "train_het": blind_pocket_het,
                            },
                            "rnp_style_top25": {
                                "train_pdb": blind_rnp_pdb,
                                "train_het": blind_rnp_het,
                            },
                        }
                    ],
                }
            )

        report = build_report(
            _audit("exact", exact_rows),
            _audit("blind", blind_rows),
        )
        comparison = report["metric_comparison"]

        self.assertEqual(comparison["paired_complete_count"], 3)
        self.assertEqual(
            comparison["thresholds"],
            {"pocket_aware": 0.25, "rnp_style_top25": 0.25},
        )
        self.assertEqual(
            comparison["exact_score_correlation"]["spearman"], 1.0
        )
        self.assertEqual(
            comparison["exact_classification_agreement"]["agreement_rate"],
            0.6667,
        )
        self.assertEqual(
            comparison["metrics"]["pocket_aware"][
                "blind_classification_accuracy"
            ],
            1.0,
        )
        self.assertEqual(
            comparison["metrics"]["rnp_style_top25"]["auroc"], 1.0
        )
        self.assertEqual(
            comparison["metrics"]["pocket_aware"][
                "closest_training_system_recovery"
            ]["pdb_only_match_rate"],
            0.6667,
        )
        self.assertEqual(
            comparison["metrics"]["rnp_style_top25"][
                "closest_training_system_recovery"
            ]["pdb_and_ligand_match_rate"],
            0.3333,
        )
        record = report["records"][0]
        self.assertEqual(record["rnp_exact_train_pdb"], "R001")
        self.assertEqual(record["rnp_blind_train_pdb"], "R001")
        markdown = render_markdown(report, "exact-digest", "blind-digest")
        self.assertIn("## Parallel metric comparison", markdown)
        self.assertIn("RnP-style top 25", markdown)


if __name__ == "__main__":
    unittest.main()
