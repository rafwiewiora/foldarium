from __future__ import annotations

import json
import os
import unittest
from datetime import date
from unittest.mock import Mock, patch

from foldarium_pipeline.cameo import CAMEO_SITEMAP_URL, target_url
from foldarium_pipeline.intake import WWPDB_NONPOLYMER_URL, WWPDB_SEQUENCE_URL, WeeklyPolicy
from foldarium_pipeline.supabase import SupabasePublicationError
from foldarium_pipeline.weekly import (
    WeeklyNotReady,
    build_public_weekly_plan,
    collect_public_inputs,
    collect_wwpdb_inputs,
    deployment_weekly_hook,
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
    def test_wwpdb_prerelease_is_ready_without_cameo(self) -> None:
        mapping = sources()
        del mapping[CAMEO_SITEMAP_URL]
        result = collect_wwpdb_inputs(RELEASE, fetcher=mapping.__getitem__)
        self.assertEqual(result["availability"]["wwpdb_entry_count"], 1)
        self.assertEqual(set(result["source_files"]), {"wwpdb_sequence", "wwpdb_nonpolymer"})

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
        with self.assertRaisesRegex(WeeklyNotReady, "not advertised") as raised:
            collect_public_inputs(
                date(2026, 8, 15), fetcher=mapping.__getitem__, fetch_workers=1
            )
        self.assertEqual(raised.exception.availability["wwpdb_sequence_rows"], 1)
        self.assertEqual(raised.exception.availability["wwpdb_nonpolymer_rows"], 1)
        self.assertEqual(raised.exception.availability["cameo_target_pages"], 0)


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
        self.assertEqual(replay["availability"]["wwpdb_entry_count"], 1)
        self.assertNotIn("cameo_target_pages", replay["source_files"])


class DeploymentWeeklyHookTests(unittest.TestCase):
    @staticmethod
    def _registration_conflict_coordinator(*, campaign_exists_after: bool) -> Mock:
        coordinator = Mock()
        coordinator.weekly_campaign_exists.side_effect = [
            False,
            campaign_exists_after,
        ]
        coordinator.register_weekly_plan.side_effect = SupabasePublicationError(
            "register_weekly_prediction_plan failed with HTTP 409",
            http_status=409,
        )
        return coordinator

    def test_registration_conflict_waits_for_next_tick_without_tasks(self) -> None:
        coordinator = self._registration_conflict_coordinator(
            campaign_exists_after=False
        )
        plan = {"tasks": [{"task_id": "run_fixture"}]}
        replay = {"source_files": {"fixture": (b"fixture", "text/plain")}}

        with (
            patch.dict(
                os.environ,
                {
                    "FOLDARIUM_RELEASE_DATE": RELEASE.isoformat(),
                    "FOLDARIUM_WEEKLY_REGISTER": "1",
                },
            ),
            patch(
                "foldarium_pipeline.weekly.SupabaseCoordinator.from_env",
                return_value=coordinator,
            ),
            patch(
                "foldarium_pipeline.weekly.build_public_weekly_plan",
                return_value=(plan, replay),
            ),
        ):
            result = deployment_weekly_hook()

        self.assertEqual(result["status"], "waiting-for-registration")
        self.assertEqual(result["tasks"], [])
        self.assertEqual(
            result["registration"],
            {"status": "conflict-retry", "http_status": 409},
        )

    def test_registration_race_resolves_as_already_registered(self) -> None:
        coordinator = self._registration_conflict_coordinator(
            campaign_exists_after=True
        )
        plan = {"tasks": [{"task_id": "run_fixture"}]}
        replay = {"source_files": {"fixture": (b"fixture", "text/plain")}}

        with (
            patch.dict(
                os.environ,
                {
                    "FOLDARIUM_RELEASE_DATE": RELEASE.isoformat(),
                    "FOLDARIUM_WEEKLY_REGISTER": "1",
                },
            ),
            patch(
                "foldarium_pipeline.weekly.SupabaseCoordinator.from_env",
                return_value=coordinator,
            ),
            patch(
                "foldarium_pipeline.weekly.build_public_weekly_plan",
                return_value=(plan, replay),
            ),
        ):
            result = deployment_weekly_hook()

        self.assertEqual(result["status"], "already-registered")
        self.assertEqual(result["tasks"], [])

    def test_non_conflict_registration_error_remains_fatal(self) -> None:
        coordinator = Mock()
        coordinator.weekly_campaign_exists.return_value = False
        coordinator.register_weekly_plan.side_effect = SupabasePublicationError(
            "register_weekly_prediction_plan failed with HTTP 503",
            http_status=503,
        )
        plan = {"tasks": [{"task_id": "run_fixture"}]}
        replay = {"source_files": {"fixture": (b"fixture", "text/plain")}}

        with (
            patch.dict(
                os.environ,
                {
                    "FOLDARIUM_RELEASE_DATE": RELEASE.isoformat(),
                    "FOLDARIUM_WEEKLY_REGISTER": "1",
                },
            ),
            patch(
                "foldarium_pipeline.weekly.SupabaseCoordinator.from_env",
                return_value=coordinator,
            ),
            patch(
                "foldarium_pipeline.weekly.build_public_weekly_plan",
                return_value=(plan, replay),
            ),
            self.assertRaisesRegex(SupabasePublicationError, "HTTP 503"),
        ):
            deployment_weekly_hook()


if __name__ == "__main__":
    unittest.main()
