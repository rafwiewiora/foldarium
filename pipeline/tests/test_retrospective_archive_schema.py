from __future__ import annotations

import unittest
from pathlib import Path


class RetrospectiveArchiveSchemaTests(unittest.TestCase):
    @classmethod
    def migration(cls) -> str:
        repository = Path(__file__).resolve().parents[2]
        return (
            repository
            / "supabase"
            / "migrations"
            / "20260826003000_add_weekly_retrospective_publications.sql"
        ).read_text(encoding="utf-8").lower()

    def test_browser_code_does_not_reference_archive_catalog_or_service_rpc(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        paths = [
            repository / "app.js",
            repository / "quiz-backend.js",
            *(
                path
                for path in (repository / "api").glob("*.js")
                if path.name != "weekly-retrospectives.js"
            ),
        ]
        for path in paths:
            content = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(repository)):
                self.assertNotIn("weekly_retrospective_publications", content)
                self.assertNotIn(
                    "register_weekly_retrospective_publication", content
                )
                self.assertNotIn(
                    "list_missing_weekly_retrospective_publications", content
                )
                self.assertNotIn(
                    "weekly_retrospective_automated_identities", content
                )
        server_api = (
            repository / "api" / "weekly-retrospectives.js"
        ).read_text(encoding="utf-8")
        self.assertIn("weekly_retrospective_publications", server_api)
        self.assertNotIn("register_weekly_retrospective_publication", server_api)
        self.assertNotIn("list_missing_weekly_retrospective_publications", server_api)
        self.assertNotIn("weekly_retrospective_automated_identities", server_api)

    def test_automated_identity_registry_is_seeded_append_only_and_service_only(
        self,
    ) -> None:
        migration = self.migration()
        normalized = " ".join(migration.split())
        self.assertIn(
            "create table public.weekly_retrospective_automated_identities",
            normalized,
        )
        self.assertIn("user_id uuid primary key references auth.users(id)", normalized)
        self.assertIn("participant_kind = 'llm'", normalized)
        self.assertIn(
            "display_name in ('claude opus', 'codex gpt-5.6')", normalized
        )
        self.assertIn(
            "having count(distinct session.display_name) <> 1", normalized
        )
        self.assertIn(
            "legacy retrospective automated identity is ambiguous", normalized
        )
        self.assertIn(
            "where session.round_id = 'weekly-2026-08-08-beta-v4'",
            normalized,
        )
        self.assertIn(
            "min(session.started_at)", normalized
        )
        self.assertIn(
            "before update or delete on "
            "public.weekly_retrospective_automated_identities",
            normalized,
        )
        self.assertIn(
            "automated identity rows are immutable", normalized
        )
        self.assertIn(
            "alter table public.weekly_retrospective_automated_identities "
            "enable row level security",
            normalized,
        )
        self.assertIn(
            "create or replace function "
            "public.register_weekly_retrospective_automated_identity",
            normalized,
        )
        self.assertIn(
            "not exists (select 1 from auth.users where id = p_user_id)",
            normalized,
        )
        self.assertIn(
            "on conflict (user_id) do nothing", normalized
        )
        for role in ("public", "anon", "authenticated"):
            self.assertIn(
                "revoke all on table "
                "public.weekly_retrospective_automated_identities "
                f"from {role}",
                normalized,
            )
            self.assertIn(
                "public.register_weekly_retrospective_automated_identity"
                "(uuid, text, text) "
                f"from {role}",
                normalized,
            )
        self.assertIn(
            "grant select on table "
            "public.weekly_retrospective_automated_identities to service_role",
            normalized,
        )
        self.assertNotIn(
            "grant insert on table "
            "public.weekly_retrospective_automated_identities",
            normalized,
        )
        self.assertIn(
            "grant execute on function "
            "public.register_weekly_retrospective_automated_identity"
            "(uuid, text, text) to service_role",
            normalized,
        )

    def test_expected_source_joins_registry_instead_of_legacy_sessions(self) -> None:
        migration = self.migration()
        source_function = migration.split(
            "create or replace function "
            "private.foldarium_expected_weekly_retrospective_source",
            1,
        )[1].split("$$;", 1)[0]
        self.assertIn(
            "left join public.weekly_retrospective_automated_identities identity",
            source_function,
        )
        self.assertIn("identity.participant_kind = 'llm'", source_function)
        self.assertNotIn("weekly-2026-08-08-beta-v4", source_function)

    def test_legacy_anonymous_fallback_is_exactly_scoped_and_private(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260826180000_allow_legacy_anonymous_retrospective.sql"
        ).read_text(encoding="utf-8").lower()
        normalized = " ".join(migration.split())

        self.assertIn(
            "p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'",
            normalized,
        )
        self.assertIn("then 'anonymous'", normalized)
        self.assertIn("validation.maximum_display_name_count <= 1", normalized)
        self.assertIn(
            "or p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29'",
            normalized,
        )
        self.assertNotIn("weekly-2026-08-08-beta-v4", normalized)
        self.assertNotIn("grant ", normalized)
        for role in ("public", "anon", "authenticated", "service_role"):
            self.assertIn(
                "revoke all on function "
                "private.foldarium_expected_weekly_retrospective_source(text) "
                f"from {role}",
                normalized,
            )

    def test_publication_identity_hash_closes_canonical_json_string(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260826184000_fix_retrospective_publication_identity.sql"
        ).read_text(encoding="utf-8").lower()
        normalized = " ".join(migration.split())

        self.assertIn(
            "|| (p_publication ->> 'source_snapshot_sha256') || '\"}'",
            normalized,
        )
        self.assertNotIn(
            "|| (p_publication ->> 'source_snapshot_sha256') || '}',",
            normalized,
        )
        for role in ("public", "anon", "authenticated"):
            self.assertIn(
                "revoke all on function "
                "public.register_weekly_retrospective_publication(jsonb, text) "
                f"from {role}",
                normalized,
            )
        self.assertIn(
            "grant execute on function "
            "public.register_weekly_retrospective_publication(jsonb, text) "
            "to service_role",
            normalized,
        )

    def test_vote_scope_is_exact_for_legacy_and_required_afterward(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260826190000_require_retrospective_vote_scope.sql"
        ).read_text(encoding="utf-8").lower()
        normalized = " ".join(migration.split())

        self.assertIn(
            "when p_round_id = 'weekly-2026-08-08-beta-v5-global-tm-29' "
            "then 'exact' else null",
            normalized,
        )
        self.assertIn(
            "where not vote.picked_none and vote.selection_kind is null",
            normalized,
        )
        self.assertNotIn("else 'unknown'", normalized)
        for role in ("public", "anon", "authenticated", "service_role"):
            self.assertIn(
                "revoke all on function "
                "private.foldarium_expected_weekly_retrospective_source(text) "
                f"from {role}",
                normalized,
            )

    def test_catalog_is_separate_append_only_and_service_only(self) -> None:
        normalized = " ".join(self.migration().split())
        self.assertIn(
            "create table public.weekly_retrospective_publications", normalized
        )
        self.assertIn("unique (round_id)", normalized)
        self.assertIn("unique (evaluation_id)", normalized)
        self.assertIn("before update or delete", normalized)
        self.assertIn("publication rows are immutable", normalized)
        self.assertIn(
            "alter table public.weekly_retrospective_publications enable row level security",
            normalized,
        )
        for role in ("public", "anon", "authenticated"):
            self.assertIn(
                "revoke all on table public.weekly_retrospective_publications "
                f"from {role}",
                normalized,
            )
        self.assertIn(
            "grant select on table public.weekly_retrospective_publications "
            "to service_role",
            normalized,
        )
        self.assertNotIn(
            "grant insert on table public.weekly_retrospective_publications",
            normalized,
        )
        self.assertNotIn("create policy", normalized)
        self.assertNotIn("create view", normalized)

    def test_trigger_binds_revealed_round_and_exact_v5_evaluation(self) -> None:
        normalized = " ".join(self.migration().split())
        for clause in (
            "v_round.environment <> 'production'",
            "v_round.status <> 'revealed'",
            "new.round_revealed_at <> v_round.revealed_at",
            "new.reveal_manifest_sha256 <> v_round.reveal_manifest_sha256",
            "v_evaluation.format_version <> 'foldarium.weekly-private-evaluation/v5'",
            "new.evaluation_artifact_sha256 <> v_evaluation.artifact_sha256",
            "new.reference_set_sha256 <> v_evaluation.reference_set_sha256",
            "new.prediction_set_sha256 <> v_evaluation.prediction_set_sha256",
            "split_part(new.admin_artifact_object_uri, '/', 3) "
            "<> split_part(v_evaluation.artifact_object_uri, '/', 3)",
        ):
            self.assertIn(clause, normalized)
        self.assertNotIn("update public.weekly_quiz_rounds", normalized)
        self.assertNotIn("delete from public.weekly_quiz_rounds", normalized)

    def test_registration_rpc_recomputes_source_and_is_not_browser_executable(self) -> None:
        normalized = " ".join(self.migration().split())
        self.assertIn(
            "private.foldarium_expected_weekly_retrospective_source", normalized
        )
        self.assertIn(
            "v_source is distinct from v_expected_source", normalized
        )
        self.assertIn(
            "extensions.digest(convert_to(p_source_snapshot_canonical, 'utf8'), "
            "'sha256')",
            normalized,
        )
        for table in (
            "public.weekly_quiz_votes",
            "public.weekly_quiz_vote_attempts",
            "public.weekly_quiz_sessions",
            "public.weekly_retrospective_automated_identities",
        ):
            self.assertIn(f"lock table {table} in share mode", normalized)
        self.assertIn(
            "retrospective publication_id is not deterministic", normalized
        )
        for role in ("public", "anon", "authenticated"):
            self.assertIn(
                "revoke all on function "
                "public.register_weekly_retrospective_publication(jsonb, text) "
                f"from {role}",
                normalized,
            )
            self.assertIn(
                "revoke all on function "
                "public.list_missing_weekly_retrospective_publications() "
                f"from {role}",
                normalized,
            )
        self.assertIn(
            "grant execute on function "
            "public.register_weekly_retrospective_publication(jsonb, text) "
            "to service_role",
            normalized,
        )

    def test_catalog_has_separate_source_public_and_admin_descriptors(self) -> None:
        migration = self.migration()
        table = migration.split(
            "create table public.weekly_retrospective_publications (", 1
        )[1].split(
            "create index weekly_retrospective_publications_created_idx", 1
        )[0]
        for prefix in ("source_snapshot", "public_artifact", "admin_artifact"):
            for suffix in (
                "object_uri",
                "sha256",
                "size_bytes",
                "media_type",
            ):
                self.assertIn(f"{prefix}_{suffix}", table)
        self.assertNotIn("reveal_manifest jsonb", table)
        self.assertNotIn("source_snapshot jsonb", table)

    def test_retrospective_source_includes_active_post_close_benchmarks(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260826233000_add_retrospective_post_close_benchmarks.sql"
        ).read_text(encoding="utf-8").lower()
        normalized = " ".join(migration.split())

        self.assertIn("active_benchmarks as (", normalized)
        self.assertIn("benchmark_vote_rows as (", normalized)
        self.assertIn("item.value -> 'unclustered' ->> 'selection_kind'", normalized)
        self.assertIn("'gpt-5.6 sol'", normalized)
        self.assertIn("successor.supersedes_execution_id = benchmark.execution_id", normalized)
        self.assertIn("combined_participants as (", normalized)
        self.assertIn("combined_votes as (", normalized)
        self.assertIn(
            "lock table public.weekly_selector_post_close_benchmarks_v1 in share mode",
            normalized,
        )
        self.assertIn("benchmark_validation.unknown_name_count = 0", normalized)
        self.assertIn("benchmark_validation.distinct_name_count", normalized)

    def test_benchmark_projection_returns_only_active_executions(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        migration = (
            repository
            / "supabase"
            / "migrations"
            / "20260826233000_add_retrospective_post_close_benchmarks.sql"
        ).read_text(encoding="utf-8").lower()
        projection = migration.split(
            "create or replace function public.get_weekly_selector_benchmarks_v1",
            1,
        )[1].split("create or replace function public.register_weekly_retrospective_publication", 1)[0]
        self.assertIn("successor.supersedes_execution_id = benchmark.execution_id", projection)
        self.assertGreaterEqual(projection.count("successor.supersedes_execution_id"), 1)


if __name__ == "__main__":
    unittest.main()
