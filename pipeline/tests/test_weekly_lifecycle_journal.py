from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foldarium_pipeline.weekly_lifecycle_journal import (
    bound_event_payload,
    commit_journal_volume,
    dumps_journal_json,
    format_failure_record,
    read_lifecycle_journal_events,
    redact_freeform_text,
    run_with_lifecycle_journal,
    sanitize_for_journal,
    write_lifecycle_journal_event,
)


class WeeklyLifecycleJournalTests(unittest.TestCase):
    def test_sanitize_redacts_sensitive_keys_recursively(self) -> None:
        payload = {
            "api_token": "abc",
            "nested": {"user_password": "secret"},
            "safe": "visible",
        }
        sanitized = sanitize_for_journal(payload)
        self.assertEqual(sanitized["api_token"], "<redacted>")
        self.assertEqual(sanitized["nested"]["user_password"], "<redacted>")
        self.assertEqual(sanitized["safe"], "visible")

    def test_redact_freeform_bearer_jwt_and_key_value_patterns(self) -> None:
        sample = (
            "Authorization failed Bearer abc.def.ghi "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature "
            "sb_secret_abc123 api_key=supersecret"
        )
        redacted = redact_freeform_text(sample)
        self.assertNotIn("supersecret", redacted)
        self.assertIn("Bearer <redacted>", redacted)
        self.assertIn("<jwt-redacted>", redacted)
        self.assertIn("<supabase-key-redacted>", redacted)

    def test_format_failure_record_redacts_exception_message_and_traceback(self) -> None:
        token = "Bearer deadbeef"
        try:
            raise RuntimeError(f"upstream rejected {token}")
        except RuntimeError as exc:
            record = format_failure_record(
                exc,
                correlation_id="corr",
                operation="weekly_tick",
                started_at="2026-09-06T00:00:00Z",
            )
        self.assertNotIn("deadbeef", record["exception_message"])
        self.assertIn("<redacted>", record["exception_message"])

    def test_non_finite_floats_normalize_to_strict_json(self) -> None:
        sanitized = sanitize_for_journal({"score": float("nan"), "ok": 1.5})
        self.assertEqual(sanitized["score"], "<non-finite-float>")
        payload = bound_event_payload({"value": float("inf")})
        serialized = dumps_journal_json(payload)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["value"], "<non-finite-float>")
        with self.assertRaises(ValueError):
            json.dumps({"bad": float("nan")}, allow_nan=False)

    def test_bound_event_payload_truncates_oversized_records(self) -> None:
        huge = {
            "operation": "weekly_tick",
            "phase": "started",
            "left": [{"payload": "x" * 3000} for _ in range(64)],
            "right": [{"payload": "y" * 3000} for _ in range(64)],
        }
        bounded = bound_event_payload(huge)
        encoded = dumps_journal_json(bounded).encode("utf-8")
        self.assertLessEqual(len(encoded), 256 * 1024)
        self.assertTrue(bounded.get("truncated"))

    def test_write_uses_exclusive_create_without_overwriting(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-exclusive-"))
        event = {
            "schema": "foldarium.weekly-lifecycle-journal/v1",
            "phase": "started",
            "operation": "weekly_tick",
            "correlation_id": "fixed",
            "event_id": "fixed",
            "recorded_at": "2026-09-06T12:00:00Z",
        }
        first = write_lifecycle_journal_event(root, event)
        second = write_lifecycle_journal_event(root, event)
        assert first is not None and second is not None
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.read_text(encoding="utf-8"), second.read_text(encoding="utf-8"))

    def test_commit_retries_transient_errors_without_sleep(self) -> None:
        attempts = {"count": 0}

        def flaky_commit() -> None:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OSError("transient volume commit")

        commit_journal_volume(flaky_commit)
        self.assertEqual(attempts["count"], 3)

    def test_fast_success_leaves_started_and_terminal_records(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-fast-"))
        commits: list[str] = []

        def track_commit() -> None:
            commits.append("commit")

        outcome = run_with_lifecycle_journal(
            "weekly_tick",
            root,
            lambda: {"status": "ok"},
            commit=track_commit,
        )
        self.assertEqual(outcome["status"], "ok")
        events = read_lifecycle_journal_events(root, limit=10)
        phases = [event["phase"] for event in events]
        self.assertEqual(phases, ["started", "succeeded"])
        self.assertGreaterEqual(len(commits), 2)

    def test_failure_passthrough_when_journal_write_fails(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-"))
        messages: list[str] = []

        def fail_commit() -> None:
            raise OSError("commit failed")

        def boom() -> None:
            raise RuntimeError("lifecycle boom")

        with self.assertRaisesRegex(RuntimeError, "lifecycle boom"):
            run_with_lifecycle_journal(
                "weekly_tick",
                root,
                boom,
                commit=fail_commit,
                on_journal_error=messages.append,
            )
        self.assertTrue(any("write-failed" in message for message in messages))
        events = read_lifecycle_journal_events(root, limit=10)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["phase"], "started")

    def test_started_record_written_before_body_runs(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-started-"))
        seen: list[str] = []

        def body() -> dict[str, str]:
            events = read_lifecycle_journal_events(root, phase="started", limit=5)
            seen.append(events[0]["operation"])
            return {"status": "ok"}

        outcome = run_with_lifecycle_journal("nextweekly_tick", root, body)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(seen, ["nextweekly_tick"])

    def test_read_calls_reload_hook_before_scanning(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-reload-"))
        write_lifecycle_journal_event(
            root,
            {
                "schema": "foldarium.weekly-lifecycle-journal/v1",
                "phase": "succeeded",
                "operation": "probe",
                "correlation_id": "abc",
                "event_id": "evt1",
                "recorded_at": "2026-09-06T12:00:00Z",
            },
        )
        reloaded = {"count": 0}

        def reload() -> None:
            reloaded["count"] += 1

        events = read_lifecycle_journal_events(root, reload=reload, limit=5)
        self.assertEqual(reloaded["count"], 1)
        self.assertEqual(len(events), 1)

    def test_commit_retry_on_terminal_record(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="foldarium-journal-retry-"))
        state = {"calls": 0}

        def flaky_commit() -> None:
            state["calls"] += 1
            if state["calls"] in {2, 4}:
                raise OSError("transient")

        run_with_lifecycle_journal(
            "weekly_tick",
            root,
            lambda: {"status": "done"},
            commit=flaky_commit,
        )
        events = read_lifecycle_journal_events(root, limit=10)
        self.assertEqual([event["phase"] for event in events], ["started", "succeeded"])
        self.assertGreaterEqual(state["calls"], 3)


if __name__ == "__main__":
    unittest.main()
