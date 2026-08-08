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


if __name__ == "__main__":
    unittest.main()
