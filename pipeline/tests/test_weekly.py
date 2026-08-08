from __future__ import annotations

import json
import unittest
from datetime import date

from foldarium_pipeline.cameo import CAMEO_SITEMAP_URL, target_url
from foldarium_pipeline.intake import WWPDB_NONPOLYMER_URL, WWPDB_SEQUENCE_URL, WeeklyPolicy
from foldarium_pipeline.weekly import (
    WeeklyNotReady,
    build_public_weekly_plan,
    collect_public_inputs,
)


RELEASE = date(2026, 8, 8)


def target_payload(target_id: str) -> dict:
    return {
        "target": {
            "id": target_id,
            "week_id": RELEASE.isoformat(),
            "labels_submission_3d": "hard",
        },
        "entities": [
            {
                "id": target_id + "_1",
                "entity_type": "protein",
                "canonical_sequence": "M" + "A" * 49,
            },
            {
                "id": target_id + "_np_1",
                "entity_type": "non_polymer",
                "component_id": "DRG",
                "smiles": "CCCCCCCCCCCCCCCC",
                "inchi": "InChI=fixture",
            },
        ],
        "biounits": [],
        "predictions": [],
    }


def page(payload: dict) -> bytes:
    inner = "f:" + json.dumps(["$", "$L1", None, payload], separators=(",", ":"))
    argument = json.dumps([1, inner], separators=(",", ":"))
    return f"<script>self.__next_f.push({argument})</script>".encode()


def sources() -> dict[str, bytes]:
    ids = ["2026-08-08_00000001", "2026-08-08_00000002"]
    sitemap = (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{target_url(target_id)}</loc></url>" for target_id in ids)
        + "</urlset>"
    ).encode()
    return {
        WWPDB_SEQUENCE_URL: (
            b"PDB_ID\tSequence_Count\tSequence\n"
            b"10AA\t1\tMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        ),
        WWPDB_NONPOLYMER_URL: (
            b"PDB_ID\tComponent_ID\tInChI\tSMILES string\n"
            b"10AA\tDRG\tInChI=fixture\tCCCCCCCCCCCCCCCC\n"
        ),
        CAMEO_SITEMAP_URL: sitemap,
        **{target_url(target_id): page(target_payload(target_id)) for target_id in ids},
    }


class PublicCollectionTests(unittest.TestCase):
    def test_collects_complete_replay_bundle(self) -> None:
        mapping = sources()
        result = collect_public_inputs(
            RELEASE, fetcher=mapping.__getitem__, fetch_workers=1
        )
        self.assertEqual(len(result["payloads"]), 2)
        self.assertEqual(result["availability"]["target_pages"], 2)
        self.assertIn("cameo_target_pages", result["source_files"])
        bundle, media_type = result["source_files"]["cameo_target_pages"]
        self.assertTrue(bundle.startswith(b"\x1f\x8b"))
        self.assertEqual(media_type, "application/gzip")

    def test_partial_cameo_pages_fail_before_planning(self) -> None:
        mapping = sources()
        del mapping[target_url("2026-08-08_00000002")]
        with self.assertRaisesRegex(WeeklyNotReady, "incomplete"):
            collect_public_inputs(RELEASE, fetcher=mapping.__getitem__, fetch_workers=1)

    def test_missing_week_fails_as_not_ready(self) -> None:
        mapping = sources()
        with self.assertRaisesRegex(WeeklyNotReady, "not advertised"):
            collect_public_inputs(
                date(2026, 8, 15), fetcher=mapping.__getitem__, fetch_workers=1
            )


class PublicPlanTests(unittest.TestCase):
    def test_plans_but_does_not_register_or_submit(self) -> None:
        mapping = sources()
        plan, replay = build_public_weekly_plan(
            RELEASE,
            policy=WeeklyPolicy(max_targets=1),
            output_prefix="supabase://test/runs",
            fetcher=mapping.__getitem__,
            fetch_workers=1,
        )
        self.assertEqual(plan["budget"]["selected_targets"], 1)
        self.assertEqual(plan["budget"]["gpu_tasks"], 2)
        self.assertEqual(replay["availability"]["target_pages"], 2)


if __name__ == "__main__":
    unittest.main()
