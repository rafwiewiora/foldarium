"""Prepared metadata must not erase a concurrent activation or another artifact."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from test_supabase import FakeResponse


class PreparedEvaluationCASTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "round_id": "weekly-2026-08-29-beta-v2",
            "environment": "production",
            "status": "open",
            "closes_at": "2026-09-09T00:00:00+00:00",
            "reveal_manifest": None,
            "revealed_at": None,
            "metadata": {
                "retrospective_release": {
                    "policy": "next-weekly-activation",
                    "original_closes_at": "2026-09-02T00:00:00+00:00",
                    "safety_closes_at": "2026-09-09T00:00:00+00:00",
                    "configured_at": "2026-09-01T19:00:00+00:00",
                }
            },
        }
        self.report = {
            "evaluation_id": "evaluation-test",
            "blind_manifest_sha256": "a" * 64,
            "private_index_sha256": "b" * 64,
            "reveal_manifest_sha256": "c" * 64,
            "item_count": 1,
            "choice_count": 10,
            "artifact": {
                "sha256": "d" * 64,
                "size_bytes": 50,
                "media_type": "application/json",
                "object_uri": "supabase://results/sha256/dd/" + "d" * 64,
            },
        }
        self.row["metadata"]["escaping_fixture"] = 'quotes"; & + eq.? # / \\ \n λ'
        self.now = datetime(2026, 9, 9, 19, 45, tzinfo=timezone.utc)
        self.patches = 0
        self.race = None
        self.lose_ack = False

    def opener(self, request, *, timeout):
        if request.get_method() == "GET":
            if parse_qs(urlsplit(request.full_url).query).get("round_id") == [
                "eq.weekly-2026-09-05-beta-v2"
            ]:
                return FakeResponse(
                    json.dumps(
                        [
                            {
                                "round_id": "weekly-2026-09-05-beta-v2",
                                "environment": "production",
                                "status": "open",
                                "opens_at": self.now.isoformat(),
                                "opened_at": self.now.isoformat(),
                                "metadata": {},
                            }
                        ]
                    ).encode()
                )
            stale = deepcopy(self.row)
            if self.race:
                self.row["metadata"]["retrospective_release"].update(self.race)
                self.race = None
            return FakeResponse(json.dumps([stale]).encode())
        self.patches += 1
        query = parse_qs(urlsplit(request.full_url).query)
        # Apply the actual emitted PostgREST null predicates to authoritative state.
        release = self.row["metadata"]["retrospective_release"]
        if "metadata" in query:
            self.assertTrue(query["metadata"][0].startswith("eq."))
            self.assertEqual(
                json.loads(query["metadata"][0][3:])["escaping_fixture"],
                self.row["metadata"]["escaping_fixture"],
            )
        if (
            "metadata" in query
            and json.loads(query["metadata"][0].removeprefix("eq.")) != self.row["metadata"]
        ):
            return FakeResponse(b"[]")
        for key, values in query.items():
            if key.startswith("metadata->retrospective_release->>") and values == ["is.null"]:
                if release.get(key.split("->>")[-1]) is not None:
                    return FakeResponse(b"[]")
        self.row.update(json.loads(request.data))
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("synthetic lost PATCH acknowledgement")
        return FakeResponse(json.dumps([self.row]).encode())

    def record(self):
        return SupabaseCoordinator(
            "https://fixture.invalid", "fixture-key", "results", opener=self.opener
        ).record_prepared_weekly_evaluation(self.row["round_id"], self.report, prepared_at=self.now)

    def test_late_activation_cannot_be_erased_by_stale_preparation(self):
        self.race = {
            "activated_by_round_id": "weekly-2026-09-05-beta-v2",
            "activated_at": self.now.isoformat(),
            "effective_closes_at": self.row["closes_at"],
        }
        with self.assertRaisesRegex(SupabasePublicationError, "updated no exact round"):
            self.record()
        release = self.row["metadata"]["retrospective_release"]
        self.assertEqual(release["activated_by_round_id"], "weekly-2026-09-05-beta-v2")
        self.assertNotIn("prepared_evaluation", release)

    def test_concurrent_prepared_artifact_remains_first_writer(self):
        self.race = {"prepared_evaluation": {"evaluation_id": "already-prepared"}}
        with self.assertRaisesRegex(SupabasePublicationError, "updated no exact round"):
            self.record()
        self.assertEqual(
            self.row["metadata"]["retrospective_release"]["prepared_evaluation"],
            {"evaluation_id": "already-prepared"},
        )

    def test_lost_ack_replay_after_activation_returns_existing_exact_artifact(self):
        self.lose_ack = True
        with self.assertRaises(Exception):
            self.record()
        self.row["metadata"]["retrospective_release"][
            "activated_by_round_id"
        ] = "weekly-2026-09-05-beta-v2"
        result = self.record()
        self.assertEqual(self.patches, 1)
        self.assertEqual(
            result["metadata"]["retrospective_release"]["activated_by_round_id"],
            "weekly-2026-09-05-beta-v2",
        )

    def extend(self):
        return SupabaseCoordinator(
            "https://fixture.invalid", "fixture-key", "results", opener=self.opener
        ).extend_delayed_weekly_voting_window(
            self.row["round_id"],
            expected_safety_closes_at="2026-09-09T00:00:00+00:00",
            new_safety_closes_at="2026-09-16T00:00:00+00:00",
            now=self.now,
        )

    def activate(self):
        return SupabaseCoordinator(
            "https://fixture.invalid", "fixture-key", "results", opener=self.opener
        ).close_delayed_weekly_round_for_successor(
            self.row["round_id"], "weekly-2026-09-05-beta-v2", activated_at=self.now
        )

    def test_extension_cannot_erase_late_activation(self):
        original_close = self.row["closes_at"]
        self.race = {"activated_by_round_id": "weekly-2026-09-05-beta-v2"}
        with self.assertRaisesRegex(SupabasePublicationError, "updated no exact round"):
            self.extend()
        self.assertEqual(self.row["closes_at"], original_close)
        self.assertIn("activated_by_round_id", self.row["metadata"]["retrospective_release"])

    def test_activation_and_extension_cannot_erase_new_preparation(self):
        for method in (self.activate, self.extend):
            with self.subTest(method=method.__name__):
                self.setUp()
                self.race = {"prepared_evaluation": {"evaluation_id": "concurrent-winner"}}
                with self.assertRaisesRegex(SupabasePublicationError, "updated no exact round"):
                    method()
                self.assertEqual(
                    self.row["metadata"]["retrospective_release"]["prepared_evaluation"],
                    {"evaluation_id": "concurrent-winner"},
                )

    def test_activation_and_extension_replay_lost_ack_without_second_patch(self):
        for method in (self.activate, self.extend):
            with self.subTest(method=method.__name__):
                self.setUp()
                self.lose_ack = True
                with self.assertRaises(Exception):
                    method()
                expected = deepcopy(self.row)
                self.assertEqual(method(), expected)
                self.assertEqual(self.patches, 1)
