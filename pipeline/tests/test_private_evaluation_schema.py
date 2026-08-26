from __future__ import annotations

import re
import unittest
from pathlib import Path


def _normalized(value: str) -> str:
    return " ".join(value.lower().split())


def _trigger_function_body(migration: str, function_name: str) -> str:
    match = re.search(
        rf"create\s+or\s+replace\s+function\s+{re.escape(function_name)}\(\)"
        r".*?\bas\s+\$\$(.*?)\$\$;",
        migration,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"trigger function {function_name} is missing")
    return _normalized(match.group(1))


def _reveal_binding_guard(body: str) -> str:
    match = re.search(
        r"v_private_index_sha256\s*:=.*?;\s*if\s+(.*?)\s+then\s+raise exception",
        body,
    )
    if match is None:
        raise AssertionError("reveal binding rejection guard is missing")
    return match.group(1)


def _guard_rejects(guard: str, values: dict[str, object]) -> bool:
    rejected = False
    for clause in guard.split(" or "):
        if " is distinct from " in clause:
            left, right = clause.split(" is distinct from ", 1)
            rejected = rejected or values[left] != values[right]
        elif clause.endswith(" is null"):
            rejected = rejected or values[clause.removesuffix(" is null")] is None
        elif clause == "clock_timestamp() < new.closes_at":
            rejected = rejected or (
                values["clock_timestamp()"] < values["new.closes_at"]  # type: ignore[operator]
            )
        else:
            raise AssertionError(f"unsupported reveal binding clause: {clause}")
    return rejected


class PrivateEvaluationSchemaTests(unittest.TestCase):
    @staticmethod
    def _migration(name: str) -> str:
        repository = Path(__file__).resolve().parents[2]
        return (
            repository / "supabase" / "migrations" / name
        ).read_text(encoding="utf-8")

    def test_browser_and_public_api_code_do_not_reference_private_catalog(self) -> None:
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
            with self.subTest(path=path.relative_to(repository)):
                self.assertNotIn(
                    "weekly_quiz_evaluations",
                    path.read_text(encoding="utf-8"),
                )

    def test_catalog_is_additive_append_only_and_browser_inaccessible(self) -> None:
        migration = self._migration(
            "20260815020000_add_private_weekly_evaluations.sql"
        )
        normalized = _normalized(migration)

        self.assertIn("create table public.weekly_quiz_evaluations", normalized)
        self.assertIn(
            "alter table public.weekly_quiz_evaluations enable row level security",
            normalized,
        )
        self.assertIn(
            "revoke all on table public.weekly_quiz_evaluations from public",
            normalized,
        )
        self.assertIn(
            "revoke all on table public.weekly_quiz_evaluations from anon",
            normalized,
        )
        self.assertIn(
            "revoke all on table public.weekly_quiz_evaluations from authenticated",
            normalized,
        )
        self.assertIn(
            "grant select, insert on table public.weekly_quiz_evaluations to service_role",
            normalized,
        )
        self.assertNotIn(
            "grant update on table public.weekly_quiz_evaluations", normalized
        )
        self.assertNotIn(
            "grant delete on table public.weekly_quiz_evaluations", normalized
        )
        self.assertNotIn("create policy", normalized)
        self.assertNotIn("create view", normalized)
        self.assertNotIn("create materialized view", normalized)
        self.assertNotIn("function public.", normalized)
        self.assertIn("unique (round_id)", normalized)

    def test_trigger_atomically_binds_without_mutating_or_revealing_round(self) -> None:
        migration = self._migration(
            "20260815020000_add_private_weekly_evaluations.sql"
        )
        normalized = _normalized(migration)

        self.assertIn("from public.weekly_quiz_rounds", normalized)
        self.assertIn("for update", normalized)
        for clause in (
            "v_round.environment <> 'production'",
            "v_round.status <> 'open'",
            "v_round.reveal_manifest is not null",
            "v_round.reveal_manifest_sha256 is not null",
            "v_round.revealed_at is not null",
            "clock_timestamp() < v_round.opens_at",
            "clock_timestamp() >= v_round.closes_at",
            "new.blind_manifest_sha256 <> v_round.blind_manifest_sha256",
            "new.private_index_sha256 <> v_private_index_sha256",
        ):
            self.assertIn(clause, normalized)
        self.assertIn("before update or delete", normalized)
        self.assertIn("catalog rows are immutable", normalized)
        self.assertNotIn("update public.weekly_quiz_rounds", normalized)
        self.assertNotIn("delete from public.weekly_quiz_rounds", normalized)
        self.assertNotIn("alter table public.weekly_quiz_rounds", normalized)
        self.assertNotIn("reveal_weekly_quiz_round", normalized)

    def test_later_reveal_is_exactly_bound_to_an_existing_catalog_row(self) -> None:
        migration = self._migration(
            "20260815020000_add_private_weekly_evaluations.sql"
        )
        normalized = _normalized(migration)
        body = _trigger_function_body(
            migration,
            "private.foldarium_validate_weekly_evaluation_reveal_binding",
        )

        # No catalog row means retrospective failure cannot block the independent reveal.
        self.assertIn(
            "select * into v_evaluation from public.weekly_quiz_evaluations "
            "where round_id = new.round_id; if not found then return new; end if;",
            body,
        )
        self.assertIn(
            "if new.status <> 'revealed' then return new; end if;",
            body,
        )
        guard = _reveal_binding_guard(body)
        matching = {
            "new.environment": "production",
            "v_evaluation.environment": "production",
            "new.campaign_id": "campaign-a",
            "v_evaluation.campaign_id": "campaign-a",
            "new.opens_at": 10,
            "v_evaluation.round_opens_at": 10,
            "new.closes_at": 20,
            "v_evaluation.round_closes_at": 20,
            "new.blind_manifest_sha256": "b" * 64,
            "v_evaluation.blind_manifest_sha256": "b" * 64,
            "v_private_index_sha256": "p" * 64,
            "v_evaluation.private_index_sha256": "p" * 64,
            "clock_timestamp()": 21,
            "new.reveal_manifest": {"schema_version": 1},
            "new.reveal_manifest_sha256": "r" * 64,
            "v_evaluation.reveal_manifest_sha256": "r" * 64,
            "new.revealed_at": 21,
        }
        self.assertFalse(_guard_rejects(guard, matching))

        for field in (
            "new.environment",
            "new.campaign_id",
            "new.opens_at",
            "new.closes_at",
            "new.blind_manifest_sha256",
            "v_private_index_sha256",
        ):
            mismatching = dict(matching)
            mismatching[field] = None
            with self.subTest(mismatching_field=field):
                self.assertTrue(_guard_rejects(guard, mismatching))
        for digest in ("x" * 64, None):
            mismatching_digest = dict(matching)
            mismatching_digest["new.reveal_manifest_sha256"] = digest
            with self.subTest(reveal_digest=digest):
                self.assertTrue(_guard_rejects(guard, mismatching_digest))
        for field in ("new.reveal_manifest", "new.revealed_at"):
            incomplete = dict(matching)
            incomplete[field] = None
            with self.subTest(missing_state=field):
                self.assertTrue(_guard_rejects(guard, incomplete))
        premature = dict(matching)
        premature["clock_timestamp()"] = 19
        self.assertTrue(_guard_rejects(guard, premature))

        self.assertIn(
            "raise exception "
            "'weekly reveal is not bound to its immutable evaluation catalog row'",
            body,
        )
        self.assertNotIn("update public.weekly_quiz_evaluations", body)
        self.assertNotIn("delete from public.weekly_quiz_evaluations", body)

        self.assertIn(
            "create trigger weekly_quiz_rounds_validate_evaluation_reveal "
            "after update of status, reveal_manifest, reveal_manifest_sha256, "
            "revealed_at on public.weekly_quiz_rounds for each row execute function "
            "private.foldarium_validate_weekly_evaluation_reveal_binding()",
            normalized,
        )
        for role in ("public", "anon", "authenticated", "service_role"):
            self.assertIn(
                "revoke all on function "
                "private.foldarium_validate_weekly_evaluation_reveal_binding() "
                f"from {role}",
                normalized,
            )

    def test_v5_upgrade_requires_postclose_state_on_either_side_of_reveal(self) -> None:
        migration = self._migration(
            "20260825235500_upgrade_private_weekly_evaluations_v5.sql"
        )
        normalized = _normalized(migration)

        for clause in (
            "format_version = 'foldarium.weekly-private-evaluation/v5'",
            "v_round.status not in ('open', 'revealed')",
            "clock_timestamp() < v_round.closes_at",
            "v_round.status = 'open'",
            "v_round.status = 'revealed'",
            "new.reveal_manifest_sha256 <> v_round.reveal_manifest_sha256",
        ):
            self.assertIn(clause, normalized)
        self.assertNotIn("grant ", normalized)
        self.assertNotIn("update public.weekly_quiz_rounds", normalized)
        self.assertNotIn("reveal_weekly_quiz_round", normalized)
        self.assertNotIn(
            "drop trigger weekly_quiz_rounds_validate_evaluation_reveal",
            normalized,
        )
        self.assertNotIn(
            "drop function "
            "private.foldarium_validate_weekly_evaluation_reveal_binding",
            normalized,
        )

    def test_catalog_contains_only_descriptor_not_result_payload(self) -> None:
        migration = self._migration(
            "20260815020000_add_private_weekly_evaluations.sql"
        ).lower()
        table_body = migration.split(
            "create table public.weekly_quiz_evaluations (", 1
        )[1].split(
            "create index weekly_quiz_evaluations_round_created_idx", 1
        )[0]
        for required in (
            "blind_manifest_sha256",
            "private_index_sha256",
            "reveal_manifest_sha256",
            "reference_set_sha256",
            "prediction_set_sha256",
            "artifact_object_uri",
            "artifact_sha256",
        ):
            self.assertIn(required, table_body)
        self.assertNotIn("reveal_manifest jsonb", table_body)
        self.assertNotIn("correct boolean", table_body)
        self.assertNotIn("rmsd jsonb", table_body)


if __name__ == "__main__":
    unittest.main()
