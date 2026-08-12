from __future__ import annotations

import unittest
from pathlib import Path


class TraceArchiveSchemaTests(unittest.TestCase):
    def test_archive_catalog_is_private_additive_and_non_destructive(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260812160000_add_weekly_trace_archive_catalog.sql"
        ).read_text(encoding="utf-8").lower()
        normalized = " ".join(migration.split())

        for table in (
            "private.weekly_trace_archive_jobs",
            "private.weekly_trace_archives",
            "private.weekly_trace_archive_members",
            "private.weekly_trace_archive_visits",
        ):
            self.assertIn(f"create table {table}", normalized)
            self.assertIn(f"revoke all on table {table} from authenticated", normalized)
        self.assertIn("membership_sha256", normalized)
        self.assertIn("sequence_gaps integer not null default 0 check (sequence_gaps = 0)", normalized)
        self.assertIn("primary key (archive_id, source_kind, source_id)", normalized)
        self.assertIn("archive_record_ordinal", normalized)
        self.assertIn("visit_ordinal", normalized)
        self.assertIn("accounted_omitted_sequence_count", normalized)
        self.assertIn("vote metadata and comments remain hot", normalized)
        self.assertNotIn("create function", normalized)
        self.assertNotIn("create or replace function", normalized)
        self.assertNotIn("delete from", normalized)
        self.assertNotIn("truncate table", normalized)
        self.assertNotIn("storage.objects", normalized)


if __name__ == "__main__":
    unittest.main()
