from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "prep" / "cameo" / "score_stage_novelty.py"
SPEC = importlib.util.spec_from_file_location("score_stage_novelty", SCRIPT)
try:
    assert SPEC is not None and SPEC.loader is not None
    MODULE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(MODULE)
except ModuleNotFoundError:
    MODULE = None


@unittest.skipIf(MODULE is None, "stage novelty dependencies are optional")
class StageNoveltyReportTests(unittest.TestCase):
    def test_only_complete_boolean_results_update_the_report(self) -> None:
        report = {
            "items": [
                {"id": "1ABC", "novel": None},
                {"id": "2DEF", "novel": None},
            ]
        }
        results = {
            "1ABC": {
                "item_id": "1ABC",
                "week": "2026.01.01",
                "ligand": "DRG",
                "novel": True,
                "scorer_version": "test/v1",
            },
            "2DEF": {"item_id": "2DEF", "novel": None},
        }
        self.assertEqual(MODULE.update_report(report, results), 1)
        self.assertIs(report["items"][0]["novel"], True)
        self.assertEqual(
            report["items"][0]["novelty"], {"scorer_version": "test/v1"}
        )
        self.assertIsNone(report["items"][1]["novel"])
        self.assertEqual(report["novelty_pending_items"], 1)

    def test_completed_result_becomes_content_addressed_curation_row(self) -> None:
        result = {
            "item_id": "1ABC",
            "week": "2026.07.11",
            "ligand": "DRG",
            "protein_sha256": "a" * 64,
            "xtal_ligand_sha256": "b" * 64,
            "cutoff": "2021-09-30",
            "novel_threshold": 0.25,
            "scorer_version": "test/v1",
            "foldseek_database": "pdb100",
            "train_pdb": "2XYZ",
            "train_het": "LIG",
            "train_identity": 0.4,
            "train_max_protein_identity": 0.8,
            "train_align_rmsd": 1.2,
            "train_shape_overlap": 0.55,
            "evaluated_at": "2026-08-08T00:00:00+00:00",
            "novel": False,
        }
        rows = MODULE.curation_rows({"1ABC": result})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "familiar")
        self.assertEqual(rows[0]["release_week"], "2026-07-11")
        self.assertEqual(rows[0]["metrics"]["train_shape_overlap"], 0.55)
        self.assertRegex(rows[0]["decision_id"], r"^curation_[0-9a-f]+$")


if __name__ == "__main__":
    unittest.main()
