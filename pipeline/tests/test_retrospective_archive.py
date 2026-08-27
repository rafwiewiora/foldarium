from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import manifest_sha256
from foldarium_pipeline.retrospective_archive import (
    RETROSPECTIVE_ADMIN_FORMAT_VERSION,
    RETROSPECTIVE_PUBLIC_FORMAT_VERSION,
    RetrospectiveArchiveError,
    build_retrospective_artifacts,
    build_retrospective_source_snapshot,
    materialize_retrospective_publication,
    publish_missing_retrospectives,
)


ROUND_ID = "weekly-2026-08-08-beta-v5-global-tm-29"
HUMAN_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CLAUDE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def reveal_manifest() -> dict:
    return {
        "schema_version": 1,
        "round_id": ROUND_ID,
        "blind_manifest_sha256": "a" * 64,
        "items": [
            {
                "id": "item-1",
                "choices": [
                    {
                        "id": "choice-a",
                        "accepted_correct": True,
                        "correct": True,
                    },
                    {
                        "id": "choice-b",
                        "accepted_correct": False,
                        "correct": False,
                    },
                ],
            }
        ],
    }


def blind_manifest() -> dict:
    score = {
        "metric": "smina_affinity",
        "protocol": "score_only",
        "scoring_function": "vina",
        "units": "kcal/mol",
    }
    return {
        "schema_version": 1,
        "round_id": ROUND_ID,
        "items": [
            {
                "id": "item-1",
                "choices": [
                    {"id": "choice-a", "smina_score": {**score, "value": -7.0}},
                    {"id": "choice-b", "smina_score": {**score, "value": -6.0}},
                ],
            }
        ],
    }


def round_record() -> dict:
    reveal = reveal_manifest()
    return {
        "round_id": ROUND_ID,
        "campaign_id": "wwpdb-2026-08-08",
        "environment": "production",
        "status": "revealed",
        "opens_at": "2026-08-14T20:05:00Z",
        "closes_at": "2026-08-17T20:00:00Z",
        "revealed_at": "2026-08-18T01:00:00Z",
        "blind_manifest_sha256": "a" * 64,
        "reveal_manifest": reveal,
        "reveal_manifest_sha256": manifest_sha256(reveal),
    }


def evaluation_descriptor() -> dict:
    return {
        "evaluation_id": "weekly_eval_" + "e" * 32,
        "round_id": ROUND_ID,
        "campaign_id": "wwpdb-2026-08-08",
        "environment": "production",
        "round_opens_at": "2026-08-14T20:05:00Z",
        "round_closes_at": "2026-08-17T20:00:00Z",
        "blind_manifest_sha256": "a" * 64,
        "private_index_sha256": "b" * 64,
        "reveal_manifest_sha256": round_record()["reveal_manifest_sha256"],
        "reference_set_sha256": "c" * 64,
        "prediction_set_sha256": "d" * 64,
        "format_version": "foldarium.weekly-private-evaluation/v5",
        "item_count": 1,
        "choice_count": 2,
        "artifact_sha256": hashlib.sha256(b"evaluation").hexdigest(),
        "artifact_object_uri": "supabase://private/sha256/00/" + "0" * 64,
    }


def source_rows() -> dict:
    votes = [
        {
            "round_id": ROUND_ID,
            "user_id": HUMAN_ID,
            "item_id": "item-1",
            "choice_id": "choice-b",
            "picked_none": False,
        },
        {
            "round_id": ROUND_ID,
            "user_id": CLAUDE_ID,
            "item_id": "item-1",
            "choice_id": "choice-a",
            "picked_none": False,
        },
    ]
    return {
        "votes": votes,
        "vote_attempts": [
            {
                **votes[0],
                "vote_attempt_id": "11111111-1111-4111-8111-111111111111",
                "app_state": {
                    "selection_kind": "exact",
                    "private_ui_state": "must-not-survive",
                },
                "submitted_at": "2026-08-17T19:00:00Z",
            }
        ],
        "current_sessions": [
            {
                "round_id": ROUND_ID,
                "user_id": HUMAN_ID,
                "display_name": "PocketFox",
            },
            {
                "round_id": ROUND_ID,
                "user_id": CLAUDE_ID,
                "display_name": "LegacyAgent",
            },
        ],
        "automated_identities": [
            {
                "user_id": CLAUDE_ID,
                "display_name": "Claude Opus",
                "participant_kind": "llm",
            }
        ],
    }


BENCHMARK_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def benchmark_payload(
    *,
    round_id: str = ROUND_ID,
    submission_id: str = BENCHMARK_ID,
    unclustered_kind: str = "exact",
    choice_id: str = "choice-a",
) -> dict:
    if unclustered_kind == "none":
        unclustered = {"selection_kind": "none"}
    else:
        unclustered = {"selection_kind": "exact", "choice_id": choice_id}
    return {
        "schema_version": "foldarium.weekly-selector-submission/v2",
        "submission_id": submission_id,
        "environment": "production",
        "round_id": round_id,
        "blind_manifest_sha256": "a" * 64,
        "kit_sha256": "b" * 64,
        "items": [
            {
                "item_id": "item-1",
                "clustered": {"selection_kind": "exact", "choice_id": "choice-b"},
                "unclustered": unclustered,
            }
        ],
    }


def post_close_benchmark_row(
    *,
    display_name: str = "GPT-5.6 Sol",
    payload: dict | None = None,
) -> dict:
    benchmark_payload_value = payload or benchmark_payload()
    return {
        "run_class": "post_close_benchmark",
        "display_name": display_name,
        "payload": benchmark_payload_value,
    }


def source_rows_with_human_count(count: int) -> dict:
    rows = source_rows()
    human_ids = [HUMAN_ID] + [
        f"00000000-0000-4000-8000-{index:012d}"
        for index in range(2, count + 1)
    ]
    if count == 0:
        rows["votes"] = [
            row for row in rows["votes"] if row["user_id"] == CLAUDE_ID
        ]
        rows["vote_attempts"] = []
        rows["current_sessions"] = [
            row
            for row in rows["current_sessions"]
            if row["user_id"] == CLAUDE_ID
        ]
        return rows
    for index, user_id in enumerate(human_ids[1:], start=2):
        rows["votes"].append(
            {
                "round_id": ROUND_ID,
                "user_id": user_id,
                "item_id": "item-1",
                "choice_id": "choice-a",
                "picked_none": False,
            }
        )
        rows["current_sessions"].append(
            {
                "round_id": ROUND_ID,
                "user_id": user_id,
                "display_name": f"PocketFox {index}",
            }
        )
    return rows


class RetrospectiveArchiveTests(unittest.TestCase):
    def snapshot(self) -> dict:
        rows = source_rows()
        return build_retrospective_source_snapshot(ROUND_ID, **rows)

    def test_source_snapshot_is_deterministic_and_keeps_linkage_private(self) -> None:
        rows = source_rows()
        first = build_retrospective_source_snapshot(ROUND_ID, **rows)
        reversed_rows = {
            key: list(reversed(value))
            for key, value in rows.items()
        }
        second = build_retrospective_source_snapshot(ROUND_ID, **reversed_rows)
        self.assertEqual(first, second)
        self.assertIn(HUMAN_ID, canonical_json(first))
        self.assertNotIn("private_ui_state", canonical_json(first))
        human_vote = next(
            vote
            for vote in first["votes"]
            if vote["participant_link"] == HUMAN_ID
        )
        claude_vote = next(
            vote
            for vote in first["votes"]
            if vote["participant_link"] == CLAUDE_ID
        )
        self.assertEqual(human_vote["selection_kind"], "exact")
        self.assertEqual(claude_vote["selection_kind"], "exact")

    def test_known_legacy_round_uses_anonymous_for_missing_human_session(self) -> None:
        rows = source_rows()
        rows["current_sessions"] = [
            row
            for row in rows["current_sessions"]
            if row["user_id"] != HUMAN_ID
        ]

        snapshot = build_retrospective_source_snapshot(ROUND_ID, **rows)
        human = next(
            participant
            for participant in snapshot["participants"]
            if participant["participant_link"] == HUMAN_ID
        )

        self.assertEqual(human["display_name"], "Anonymous")
        self.assertEqual(human["current_session_count"], 0)

    def test_future_round_still_rejects_missing_human_session(self) -> None:
        future_round_id = "weekly-2026-08-22"
        rows = source_rows()
        for collection in ("votes", "vote_attempts", "current_sessions"):
            for row in rows[collection]:
                row["round_id"] = future_round_id
        rows["vote_attempts"].append(
            {
                **rows["votes"][1],
                "vote_attempt_id": "22222222-2222-4222-8222-222222222222",
                "app_state": {"selection_kind": "exact"},
                "submitted_at": "2026-08-17T19:01:00Z",
            }
        )
        rows["current_sessions"] = [
            row
            for row in rows["current_sessions"]
            if row["user_id"] != HUMAN_ID
        ]

        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "one unambiguous pseudonym"
        ):
            build_retrospective_source_snapshot(future_round_id, **rows)

    def test_future_round_rejects_nonempty_vote_without_scope(self) -> None:
        future_round_id = "weekly-2026-08-22"
        rows = source_rows()
        for collection in ("votes", "vote_attempts", "current_sessions"):
            for row in rows[collection]:
                row["round_id"] = future_round_id

        with self.assertRaisesRegex(
            RetrospectiveArchiveError,
            "missing exact-or-cluster scope",
        ):
            build_retrospective_source_snapshot(future_round_id, **rows)

    def test_known_legacy_round_still_rejects_ambiguous_human_name(self) -> None:
        rows = source_rows()
        rows["current_sessions"].append(
            {
                "round_id": ROUND_ID,
                "user_id": HUMAN_ID,
                "display_name": "Another Name",
            }
        )

        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "one unambiguous pseudonym"
        ):
            build_retrospective_source_snapshot(ROUND_ID, **rows)

    def test_source_snapshot_requires_exact_code_approved_registry_rows(self) -> None:
        for mutation, expected in (
            (
                {"display_name": "Unreviewed Model"},
                "not code-approved",
            ),
            (
                {"participant_kind": "baseline"},
                "participant_kind is invalid",
            ),
        ):
            with self.subTest(expected=expected):
                rows = source_rows()
                rows["automated_identities"][0].update(mutation)
                with self.assertRaisesRegex(RetrospectiveArchiveError, expected):
                    build_retrospective_source_snapshot(ROUND_ID, **rows)

        rows = source_rows()
        rows["automated_identities"].append(
            deepcopy(rows["automated_identities"][0])
        )
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "duplicate participant"
        ):
            build_retrospective_source_snapshot(ROUND_ID, **rows)

    def test_source_snapshot_rejects_two_active_credentials_for_one_llm(self) -> None:
        rows = source_rows()
        rotated_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        rows["automated_identities"].append(
            {
                "user_id": rotated_id,
                "display_name": "Claude Opus",
                "participant_kind": "llm",
            }
        )
        rows["votes"].append(
            {
                "round_id": ROUND_ID,
                "user_id": rotated_id,
                "item_id": "item-1",
                "choice_id": "choice-a",
                "picked_none": False,
            }
        )
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "multiple credentials"
        ):
            build_retrospective_source_snapshot(ROUND_ID, **rows)

    def test_aug_8_snapshot_ignores_empty_benchmark_list(self) -> None:
        rows = source_rows()
        rows["post_close_benchmarks"] = []
        baseline = build_retrospective_source_snapshot(ROUND_ID, **rows)
        unchanged = build_retrospective_source_snapshot(
            ROUND_ID,
            votes=rows["votes"],
            vote_attempts=rows["vote_attempts"],
            current_sessions=rows["current_sessions"],
            automated_identities=rows["automated_identities"],
        )
        self.assertEqual(baseline, unchanged)

    def test_post_close_benchmark_exact_and_none_decisions_are_included(self) -> None:
        none_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        rows = source_rows()
        rows["post_close_benchmarks"] = [
            post_close_benchmark_row(),
            post_close_benchmark_row(
                display_name="Codex GPT-5.6",
                payload=benchmark_payload(
                    submission_id=none_id,
                    unclustered_kind="none",
                ),
            ),
        ]
        snapshot = build_retrospective_source_snapshot(
            ROUND_ID,
            item_count=1,
            **rows,
        )
        benchmark_participants = [
            participant
            for participant in snapshot["participants"]
            if participant["participant_link"] in {BENCHMARK_ID, none_id}
        ]
        self.assertEqual(
            sorted(row["automated_identity"] for row in benchmark_participants),
            ["Codex GPT-5.6", "GPT-5.6 Sol"],
        )
        for participant in benchmark_participants:
            self.assertEqual(participant["participant_kind"], "automated")
            self.assertIsNone(participant["display_name"])
            self.assertEqual(participant["current_session_count"], 0)
        exact_vote = next(
            vote
            for vote in snapshot["votes"]
            if vote["participant_link"] == BENCHMARK_ID
        )
        none_vote = next(
            vote for vote in snapshot["votes"] if vote["participant_link"] == none_id
        )
        self.assertEqual(exact_vote["selection_kind"], "exact")
        self.assertEqual(exact_vote["choice_id"], "choice-a")
        self.assertFalse(exact_vote["picked_none"])
        self.assertEqual(none_vote["selection_kind"], "none")
        self.assertIsNone(none_vote["choice_id"])
        self.assertTrue(none_vote["picked_none"])

    def test_post_close_benchmark_clustered_decisions_are_ignored(self) -> None:
        rows = source_rows()
        payload = benchmark_payload()
        payload["items"][0]["unclustered"] = {"selection_kind": "none"}
        payload["items"][0]["clustered"] = {
            "selection_kind": "exact",
            "choice_id": "choice-b",
        }
        rows["post_close_benchmarks"] = [post_close_benchmark_row(payload=payload)]
        snapshot = build_retrospective_source_snapshot(
            ROUND_ID,
            item_count=1,
            **rows,
        )
        vote = next(
            vote
            for vote in snapshot["votes"]
            if vote["participant_link"] == BENCHMARK_ID
        )
        self.assertEqual(vote["selection_kind"], "none")
        self.assertTrue(vote["picked_none"])

    def test_post_close_benchmark_rejects_unknown_duplicate_and_malformed_rows(
        self,
    ) -> None:
        rows = source_rows()
        rows["post_close_benchmarks"] = [
            post_close_benchmark_row(display_name="Unreviewed Model")
        ]
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "not code-approved"
        ):
            build_retrospective_source_snapshot(ROUND_ID, item_count=1, **rows)

        rows = source_rows()
        rows["post_close_benchmarks"] = [
            post_close_benchmark_row(),
            post_close_benchmark_row(
                payload=benchmark_payload(
                    submission_id="ffffffff-ffff-4fff-8fff-ffffffffffff"
                ),
            ),
        ]
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "duplicate automated identity"
        ):
            build_retrospective_source_snapshot(ROUND_ID, item_count=1, **rows)

        rows = source_rows()
        malformed = benchmark_payload()
        malformed["submission_id"] = "not-a-uuid"
        rows["post_close_benchmarks"] = [post_close_benchmark_row(payload=malformed)]
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "must be a UUID"
        ):
            build_retrospective_source_snapshot(ROUND_ID, item_count=1, **rows)

        rows = source_rows()
        incomplete = benchmark_payload()
        incomplete["items"] = []
        rows["post_close_benchmarks"] = [post_close_benchmark_row(payload=incomplete)]
        with self.assertRaisesRegex(
            RetrospectiveArchiveError, "items are incomplete"
        ):
            build_retrospective_source_snapshot(ROUND_ID, item_count=1, **rows)

    def test_post_close_benchmark_artifacts_exclude_runtime_and_provenance_fields(
        self,
    ) -> None:
        rows = source_rows()
        rows["post_close_benchmarks"] = [post_close_benchmark_row()]
        snapshot = build_retrospective_source_snapshot(
            ROUND_ID,
            item_count=1,
            **rows,
        )
        evaluation = evaluation_descriptor()
        artifact = {
            "blind_manifest": blind_manifest(),
            "reveal_manifest": reveal_manifest(),
        }
        with patch(
            "foldarium_pipeline.retrospective_archive._verify_evaluation",
            return_value=(evaluation, artifact),
        ):
            public_bytes, admin_bytes, _ = build_retrospective_artifacts(
                round_record(),
                evaluation,
                b"evaluation",
                snapshot,
            )
        combined = (public_bytes + admin_bytes).decode()
        for forbidden in (
            BENCHMARK_ID,
            "runtime_sha256",
            "execution_sha256",
            "payload_digest",
            "blindness_attestation",
            "clustered",
            "submission_id",
        ):
            self.assertNotIn(forbidden, combined)
        public = json.loads(public_bytes)
        self.assertIn("GPT-5.6 Sol", [row["participant"] for row in public["automated_entries"]])

    def test_public_and_admin_artifacts_are_sanitized_and_separately_scoped(self) -> None:
        evaluation = evaluation_descriptor()
        artifact = {
            "blind_manifest": blind_manifest(),
            "reveal_manifest": reveal_manifest(),
        }
        with patch(
            "foldarium_pipeline.retrospective_archive._verify_evaluation",
            return_value=(evaluation, artifact),
        ):
            public_bytes, admin_bytes, summary = build_retrospective_artifacts(
                round_record(),
                evaluation,
                b"evaluation",
                self.snapshot(),
            )
        public = json.loads(public_bytes)
        admin = json.loads(admin_bytes)
        self.assertEqual(public["format_version"], RETROSPECTIVE_PUBLIC_FORMAT_VERSION)
        self.assertEqual(admin["format_version"], RETROSPECTIVE_ADMIN_FORMAT_VERSION)
        self.assertEqual(public["human_aggregate"]["participant_count"], 1)
        self.assertFalse(public["human_aggregate"]["suppressed"])
        self.assertEqual(public["human_aggregate"]["complete_count"], 1)
        self.assertEqual(public["human_aggregate"]["partial_count"], 0)
        self.assertEqual(
            public["human_aggregate"]["score_distribution"],
            [{"correct": 0, "answered": 1, "participant_count": 1}],
        )
        self.assertFalse(public["questions"][0]["human_aggregate"]["suppressed"])
        self.assertEqual(public["questions"][0]["human_aggregate"]["correct_count"], 0)
        self.assertEqual(
            public["questions"][0]["human_aggregate"]["answers"][0]["vote_count"],
            1,
        )
        self.assertEqual(
            [row["participant"] for row in public["automated_entries"]],
            ["Claude Opus", "Smina"],
        )
        self.assertEqual(
            [row["participant"] for row in admin["participants"]],
            ["Claude Opus", "Smina", "PocketFox"],
        )
        self.assertNotIn("PocketFox", public_bytes.decode())
        self.assertIn("PocketFox", admin_bytes.decode())
        public_text = public_bytes.decode()
        admin_text = admin_bytes.decode()
        for forbidden in (
            HUMAN_ID,
            CLAUDE_ID,
            "participant_link",
            "session_id",
            "participant_hash",
            "app_state",
            "private_ui_state",
            "supabase://",
        ):
            self.assertNotIn(forbidden, public_text)
            self.assertNotIn(forbidden, admin_text)
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["choice_count"], 2)

    def test_public_human_aggregates_are_visible_for_zero_one_and_three(self) -> None:
        evaluation = evaluation_descriptor()
        artifact = {
            "blind_manifest": blind_manifest(),
            "reveal_manifest": reveal_manifest(),
        }
        for human_count in (0, 1, 3):
            with self.subTest(human_count=human_count):
                rows = source_rows_with_human_count(human_count)
                snapshot = build_retrospective_source_snapshot(ROUND_ID, **rows)
                reversed_snapshot = build_retrospective_source_snapshot(
                    ROUND_ID,
                    **{
                        key: list(reversed(value))
                        for key, value in rows.items()
                    },
                )
                with patch(
                    "foldarium_pipeline.retrospective_archive._verify_evaluation",
                    return_value=(evaluation, artifact),
                ):
                    public_bytes, admin_bytes, _ = build_retrospective_artifacts(
                        round_record(),
                        evaluation,
                        b"evaluation",
                        snapshot,
                    )
                    repeated_public, repeated_admin, _ = (
                        build_retrospective_artifacts(
                            round_record(),
                            evaluation,
                            b"evaluation",
                            reversed_snapshot,
                        )
                    )
                self.assertEqual(public_bytes, repeated_public)
                self.assertEqual(admin_bytes, repeated_admin)
                public = json.loads(public_bytes)
                admin = json.loads(admin_bytes)
                aggregate = public["human_aggregate"]
                question = public["questions"][0]["human_aggregate"]
                self.assertEqual(aggregate["participant_count"], human_count)
                self.assertEqual(question["answered_count"], human_count)
                self.assertFalse(aggregate["suppressed"])
                self.assertFalse(question["suppressed"])
                self.assertEqual(aggregate["complete_count"], human_count)
                self.assertEqual(aggregate["partial_count"], 0)
                self.assertEqual(
                    question["correct_count"],
                    0 if human_count == 0 else human_count - 1,
                )
                self.assertEqual(
                    sum(row["vote_count"] for row in question["answers"]),
                    human_count,
                )
                self.assertEqual(
                    len(
                        [
                            row
                            for row in admin["participants"]
                            if row["participant_kind"] == "human"
                        ]
                    ),
                    human_count,
                )
                for pseudonym in ("PocketFox", "PocketFox 2", "PocketFox 3"):
                    self.assertNotIn(pseudonym, public_bytes.decode())

    def test_existing_publication_recomputes_source_and_fails_closed_on_drift(self) -> None:
        evaluation = evaluation_descriptor()
        artifact = {
            "blind_manifest": blind_manifest(),
            "reveal_manifest": reveal_manifest(),
        }

        class Coordinator:
            storage_bucket = "private"

            def __init__(self):
                self.rows = source_rows()
                self.catalog = None
                self.objects = []
                self.object_by_uri = {}

            def require_private_bucket(self):
                return None

            def weekly_quiz_round(self, round_id):
                return round_record()

            def private_weekly_evaluation(self, round_id):
                return evaluation

            def download_content_object(self, object_uri, **kwargs):
                return self.object_by_uri.get(object_uri, b"evaluation")

            def weekly_retrospective_source_rows(self, round_id, *, environment="production"):
                return deepcopy(self.rows)

            def weekly_retrospective_publication(self, round_id):
                return deepcopy(self.catalog)

            def store_bytes(self, content, media_type):
                self.objects.append(content)
                digest = hashlib.sha256(content).hexdigest()
                object_uri = f"supabase://private/sha256/{digest[:2]}/{digest}"
                self.object_by_uri[object_uri] = content
                return {
                    "object_uri": object_uri,
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": media_type,
                }

            def register_weekly_retrospective_publication(
                self, descriptor, *, source_snapshot_canonical
            ):
                self.catalog = {**deepcopy(descriptor), "created_at": "2026-08-18T02:00:00Z"}
                return deepcopy(self.catalog)

        coordinator = Coordinator()
        with patch(
            "foldarium_pipeline.retrospective_archive._verify_evaluation",
            return_value=(evaluation, artifact),
        ):
            first = materialize_retrospective_publication(
                ROUND_ID, coordinator=coordinator
            )
            second = materialize_retrospective_publication(
                ROUND_ID, coordinator=coordinator
            )
            coordinator.rows["votes"][0]["choice_id"] = "choice-a"
            with self.assertRaisesRegex(
                RetrospectiveArchiveError, "source drift"
            ):
                materialize_retrospective_publication(
                    ROUND_ID, coordinator=coordinator
                )

        self.assertEqual(first["status"], "published")
        self.assertEqual(second["status"], "already-published")
        self.assertEqual(len(coordinator.objects), 3)
        self.assertTrue(first["admin_artifact"]["private"])

    def test_missing_scan_processes_every_round_in_coordinator_order(self) -> None:
        class Coordinator:
            def missing_weekly_retrospective_round_ids(self):
                return ["weekly-oldest", "weekly-middle", "weekly-newest"]

        coordinator = Coordinator()
        with patch(
            "foldarium_pipeline.retrospective_archive.materialize_retrospective_publication",
            side_effect=lambda round_id, *, coordinator: {
                "status": "published",
                "round_id": round_id,
            },
        ) as materialize:
            report = publish_missing_retrospectives(coordinator=coordinator)

        self.assertEqual(
            report["round_ids"],
            ["weekly-oldest", "weekly-middle", "weekly-newest"],
        )
        self.assertEqual(report["round_count"], 3)
        self.assertEqual(
            [call.args[0] for call in materialize.call_args_list],
            report["round_ids"],
        )


if __name__ == "__main__":
    unittest.main()
