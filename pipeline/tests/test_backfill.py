from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from foldarium_pipeline.backfill import BackfillError, build_backfill_plan


def candidate(week: str, suffix: str, sequence: str) -> dict:
    target_id = f"{week}_000000{suffix}"
    return {
        "target_id": target_id,
        "week": week,
        "prediction_target": {
            "target_id": target_id,
            "entities": [
                {"type": "protein", "chain_ids": ["A"], "sequence": sequence},
                {"type": "ligand", "chain_ids": ["B"], "smiles": "C" * 16},
            ],
            "source": {
                "kind": "cameo-prerelease",
                "cameo_target_id": target_id,
                "week": week,
                "pdb_id": None,
            },
            "metadata": {"cameo_label": "ligand"},
        },
    }


class BackfillPlanTests(unittest.TestCase):
    def build(self, candidates: list[dict], **overrides) -> dict:
        arguments = {
            "start_week": date(2026, 7, 11),
            "end_week": date(2026, 7, 18),
            "source_snapshot_sha256": "a" * 64,
            "output_prefix": "supabase://results/backfill",
            "generated_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
        }
        arguments.update(overrides)
        return build_backfill_plan(candidates, **arguments)

    def test_caps_each_week_and_plans_both_methods(self) -> None:
        rows = [
            candidate("2026-07-11", "01", "M" + "A" * 49),
            candidate("2026-07-11", "02", "M" + "G" * 49),
            candidate("2026-07-18", "03", "M" + "S" * 49),
        ]
        plan = self.build(rows, max_targets_per_week=1)
        self.assertEqual(plan["budget"]["selected_targets"], 2)
        self.assertEqual(plan["budget"]["gpu_tasks"], 4)
        self.assertIn("weekly-target-cap", {row["reason"] for row in plan["skipped"]})

    def test_method_subset_is_explicit(self) -> None:
        plan = self.build(
            [candidate("2026-07-11", "01", "M" + "A" * 49)], methods=["openfold3"]
        )
        self.assertEqual([task["method"] for task in plan["tasks"]], ["openfold3"])

    def test_duplicate_polymer_complexes_do_not_consume_the_cap(self) -> None:
        first = candidate("2026-07-11", "01", "M" + "A" * 49)
        duplicate = candidate("2026-07-11", "02", "M" + "A" * 49)
        plan = self.build([first, duplicate], max_targets_per_week=2)
        self.assertEqual(plan["budget"]["selected_targets"], 1)
        self.assertIn("duplicate-polymer-complex", {row["reason"] for row in plan["skipped"]})

    def test_unbounded_or_non_saturday_range_is_rejected(self) -> None:
        with self.assertRaises(BackfillError):
            self.build([], start_week=date(2026, 7, 12))


if __name__ == "__main__":
    unittest.main()
