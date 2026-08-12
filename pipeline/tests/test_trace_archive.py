from __future__ import annotations

import copy
import gzip
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from foldarium_pipeline.trace_archive import (
    TraceArchiveError,
    build_archive,
    export_session_archive,
    verify_session_archive,
)


SESSION_ID = "11111111-1111-4111-8111-111111111111"
VISIT_ID = "22222222-2222-4222-8222-222222222222"
VOTE_ID = "33333333-3333-4333-8333-333333333333"
BATCH_ONE = "44444444-4444-4444-8444-444444444444"
BATCH_TWO = "55555555-5555-4555-8555-555555555555"


def fixture() -> dict:
    session = {
        "session_id": SESSION_ID,
        "round_id": "weekly-2026-08-08-beta-v4",
        "participant_hash": "a" * 64,
        "display_name_hash": "b" * 64,
        "started_at": "2026-08-08T17:00:00+00:00",
        "completed_at": None,
    }
    vote = {
        "vote_attempt_id": VOTE_ID,
        "session_id": SESSION_ID,
        "round_id": session["round_id"],
        "item_id": "A1DI6",
        "question_index": 0,
        "choice_id": "choice-a",
        "picked_none": False,
        "viewer_trace": None,
        "app_state": {
            "trace_visit_id": VISIT_ID,
            "trace_through_sequence": 3,
            "selected_choice_id": "choice-a",
            "rejected_choice_ids": ["choice-b"],
        },
        "active_pane_id": "pane-a",
        "vote_comment": "The hydrogen-bond network looks plausible.",
        "submitted_at": "2026-08-08T17:00:09+00:00",
        "created_at": "2026-08-08T17:00:09+00:00",
    }

    def batch(batch_id: str, first: int, last: int, submitted_at: str) -> dict:
        entries = [
            {"kind": "app", "seq": sequence, "t_ms": sequence * 100, "action": "camera_moved"}
            for sequence in range(first, last + 1)
        ]
        return {
            "trace_batch_id": batch_id,
            "session_id": SESSION_ID,
            "round_id": session["round_id"],
            "item_id": "A1DI6",
            "question_index": 0,
            "visit_id": VISIT_ID,
            "first_sequence": first,
            "last_sequence": last,
            "flush_reason": "interval" if first == 0 else "vote",
            "trace": {"version": 1, "visit_id": VISIT_ID, "entries": entries},
            "app_state": {"active_pane_id": "pane-a"},
            "submitted_at": submitted_at,
            "created_at": submitted_at,
        }

    return {
        "session": session,
        # Intentionally non-canonical source order.
        "vote_attempts": [vote],
        "trace_batches": [
            batch(BATCH_TWO, 2, 3, "2026-08-08T17:00:08+00:00"),
            batch(BATCH_ONE, 0, 1, "2026-08-08T17:00:04+00:00"),
        ],
    }


class TraceArchiveTests(unittest.TestCase):
    def test_export_is_deterministic_lossless_and_comment_preserving(self) -> None:
        source = fixture()
        first_bytes, first_manifest = build_archive(source)
        second_bytes, second_manifest = build_archive(copy.deepcopy(source))
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_manifest["vote_attempt_count"], 1)
        self.assertEqual(first_manifest["trace_batch_count"], 2)
        self.assertEqual(first_manifest["trace_entry_count"], 4)
        self.assertEqual(first_manifest["omitted_entry_count"], 0)
        self.assertEqual(first_manifest["dead_letter_entry_count"], 0)
        self.assertEqual(first_manifest["sequence_gaps"], 0)
        self.assertEqual(first_manifest["visits"][0]["visit_ordinal"], 0)
        self.assertEqual(
            [member["archive_record_ordinal"] for member in first_manifest["members"]],
            [1, 2, 0],
        )
        records = [json.loads(line) for line in gzip.decompress(first_bytes).splitlines()]
        self.assertEqual(records[0]["record_type"], "session")
        self.assertEqual(records[1]["vote_comment"], source["vote_attempts"][0]["vote_comment"])
        self.assertEqual(
            [record["first_sequence"] for record in records if record["record_type"] == "trace_batch"],
            [0, 2],
        )

    def test_write_verify_and_idempotent_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = export_session_archive(fixture(), temporary)
            second = export_session_archive(fixture(), temporary)
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.archive_path.read_bytes(), second.archive_path.read_bytes())
            self.assertEqual(first.manifest_path.read_bytes(), second.manifest_path.read_bytes())
            self.assertEqual(os.stat(first.archive_path).st_mode & 0o777, 0o600)
            report = verify_session_archive(
                first.archive_path, first.manifest_path, source=fixture()
            )
            self.assertTrue(report["verified"])
            self.assertEqual(report["membership_sha256"], first.manifest["membership_sha256"])

    def test_existing_different_archive_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = export_session_archive(fixture(), temporary)
            source = fixture()
            source["vote_attempts"][0]["vote_comment"] = "A revised comment"
            with self.assertRaisesRegex(TraceArchiveError, "refusing overwrite"):
                export_session_archive(source, temporary)
            self.assertEqual(
                json.loads(first.manifest_path.read_text())["content_sha256"],
                first.manifest["content_sha256"],
            )

    def test_corrupt_archive_is_detected_before_decompression(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = export_session_archive(fixture(), temporary)
            corrupted = bytearray(result.archive_path.read_bytes())
            corrupted[len(corrupted) // 2] ^= 0x01
            result.archive_path.write_bytes(corrupted)
            with self.assertRaisesRegex(TraceArchiveError, "checksum"):
                verify_session_archive(result.archive_path, result.manifest_path)

    def test_source_drift_and_missing_membership_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = export_session_archive(fixture(), temporary)
            drifted = fixture()
            drifted["vote_attempts"][0]["vote_comment"] = "Changed after export"
            with self.assertRaisesRegex(TraceArchiveError, "drifted"):
                verify_session_archive(
                    result.archive_path, result.manifest_path, source=drifted
                )

    def test_gap_overlap_and_cross_question_visit_are_rejected(self) -> None:
        gap = fixture()
        gap["trace_batches"][0]["first_sequence"] = 3
        gap["trace_batches"][0]["trace"]["entries"] = gap["trace_batches"][0]["trace"][
            "entries"
        ][1:]
        with self.assertRaisesRegex(TraceArchiveError, "gap or overlap"):
            build_archive(gap)

        crossed = fixture()
        crossed["trace_batches"][0]["question_index"] = 1
        with self.assertRaisesRegex(TraceArchiveError, "crosses quiz questions"):
            build_archive(crossed)

    def test_plaintext_identity_or_unknown_fields_fail_closed(self) -> None:
        identity = fixture()
        identity["trace_batches"][0]["app_state"]["display_name"] = "Rafal"
        with self.assertRaisesRegex(TraceArchiveError, "forbidden identity"):
            build_archive(identity)

        unexpected = fixture()
        unexpected["session"]["display_name"] = "Rafal"
        with self.assertRaisesRegex(TraceArchiveError, "unsupported fields"):
            build_archive(unexpected)

        leaked_user = fixture()
        leaked_user["vote_attempts"][0]["user_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(TraceArchiveError, "forbidden identity"):
            build_archive(leaked_user)

    def test_legacy_vote_trace_is_preserved(self) -> None:
        source = fixture()
        source["vote_attempts"][0]["viewer_trace"] = {
            "version": 1,
            "snapshots": [{"seq": 0, "kind": "camera"}],
        }
        archive, _ = build_archive(source)
        vote = [
            json.loads(line)
            for line in gzip.decompress(archive).splitlines()
            if json.loads(line).get("record_type") == "vote_attempt"
        ][0]
        self.assertEqual(vote["viewer_trace"], source["vote_attempts"][0]["viewer_trace"])

    def test_explicit_omission_range_accounts_for_sequences_but_silent_gap_fails(self) -> None:
        source = fixture()
        second = source["trace_batches"][0]
        second["last_sequence"] = 4
        second["trace"]["entries"] = [
            {
                "kind": "omitted",
                "seq": 2,
                "accounted_first_sequence": 2,
                "accounted_last_sequence": 3,
                "reason": "single_entry_byte_budget",
                "omitted_bytes": 900000,
            },
            {"kind": "app", "seq": 4, "t_ms": 400, "action": "camera_moved"},
        ]
        _, manifest = build_archive(source)
        self.assertEqual(manifest["omitted_entry_count"], 1)
        self.assertEqual(manifest["accounted_omitted_sequence_count"], 2)
        self.assertEqual(manifest["sequence_gaps"], 0)

        del second["trace"]["entries"][0]["accounted_last_sequence"]
        with self.assertRaisesRegex(TraceArchiveError, "unexplained sequence gap"):
            build_archive(source)

    def test_vote_comment_cannot_be_stranded_inside_app_state(self) -> None:
        source = fixture()
        source["vote_attempts"][0]["app_state"]["vote_comment"] = "duplicate"
        with self.assertRaisesRegex(TraceArchiveError, "forbidden nested vote_comment"):
            build_archive(source)


if __name__ == "__main__":
    unittest.main()
