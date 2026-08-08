from __future__ import annotations

import hashlib
import unittest

from foldarium_pipeline.contracts import (
    SCHEMA_VERSION,
    canonical_json,
    make_prediction_task,
)
from foldarium_pipeline.staging import (
    StagingError,
    build_run_row,
    build_staging_plan,
    render_staging_sql,
)


TARGET = {
    "schema_version": SCHEMA_VERSION,
    "target_id": "foldarium-smoke-001",
    "entities": [
        {"type": "protein", "chain_ids": ["A"], "sequence": "ACDEFGHIK"},
        {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
    ],
}


def make_task(method: str, config: dict, *, target: dict | None = None) -> dict:
    return make_prediction_task(
        campaign_id="foldarium-smoke",
        target=target or TARGET,
        method=method,
        method_version="2.2.1" if method == "boltz2" else "0.4.4",
        container_image="registry.example/foldarium/test@sha256:" + "a" * 64,
        config=config,
        output_uri_prefix="supabase://foldarium-predictions-test/runs",
        resources={"timeout_seconds": 900},
    )


class RunRowTests(unittest.TestCase):
    def test_row_mirrors_the_task_payload(self) -> None:
        task = make_task("boltz2", {"seed": 0})
        row = build_run_row(task, adapter_version="0.1.0+abc1234")

        # The schema rejects drift between the payload and searchable columns.
        payload = row["task_payload"]
        self.assertEqual(row["run_id"], payload["task_id"])
        self.assertEqual(row["target_id"], payload["target"]["target_id"])
        self.assertEqual(row["method"], payload["method"])
        self.assertEqual(row["method_version"], payload["method_version"])
        self.assertEqual(row["image_ref"], payload["container_image"])
        self.assertEqual(row["output_prefix"], payload["output_uri_prefix"])
        self.assertEqual(row["method_configuration"], payload["config"])

    def test_digests_cover_canonical_json(self) -> None:
        task = make_task("boltz2", {"seed": 0})
        row = build_run_row(task, adapter_version="0.1.0")

        self.assertEqual(
            row["task_sha256"],
            hashlib.sha256(canonical_json(row["task_payload"]).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            row["method_config_sha256"],
            hashlib.sha256(
                canonical_json(row["method_configuration"]).encode("utf-8")
            ).hexdigest(),
        )
        self.assertRegex(row["input_sha256"], r"^[0-9a-f]{64}$")

    def test_checkpoint_reference_is_derived_per_method(self) -> None:
        openfold = build_run_row(
            make_task("openfold3", {"checkpoint": "openfold3-p2-155k"}),
            adapter_version="0.1.0",
        )
        boltz = build_run_row(make_task("boltz2", {"seed": 0}), adapter_version="0.1.0")

        self.assertEqual(openfold["checkpoint_ref"], "openfold3-p2-155k")
        self.assertEqual(boltz["checkpoint_ref"], "boltz2-2.2.1")

    def test_smoke_input_uri_is_not_mistakable_for_stored_package(self) -> None:
        row = build_run_row(make_task("boltz2", {"seed": 0}), adapter_version="0.1.0")
        self.assertTrue(row["input_uri"].startswith("foldarium-inline://target/"))
        self.assertIn(row["input_sha256"], row["input_uri"])

    def test_staged_runs_default_to_a_single_attempt(self) -> None:
        row = build_run_row(make_task("boltz2", {"seed": 0}), adapter_version="0.1.0")
        self.assertEqual(row["max_attempts"], 1)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["execution_backend"], "modal")

    def test_tampered_task_is_rejected(self) -> None:
        task = make_task("boltz2", {"seed": 0})
        task["config"] = {"seed": 1}
        with self.assertRaises(Exception):
            build_run_row(task, adapter_version="0.1.0")

    def test_terminal_status_cannot_be_staged(self) -> None:
        with self.assertRaises(StagingError):
            build_run_row(
                make_task("boltz2", {"seed": 0}),
                adapter_version="0.1.0",
                status="succeeded",
            )

    def test_adapter_version_is_required(self) -> None:
        with self.assertRaises(StagingError):
            build_run_row(make_task("boltz2", {"seed": 0}), adapter_version="  ")


class StagingPlanTests(unittest.TestCase):
    def test_two_method_plan_shares_one_campaign_and_target(self) -> None:
        plan = build_staging_plan(
            [make_task("boltz2", {"seed": 0}), make_task("openfold3", {"model_seeds": 1})],
            adapter_version="0.1.0",
        )
        self.assertEqual(len(plan["campaigns"]), 1)
        self.assertEqual(len(plan["targets"]), 1)
        self.assertEqual(len(plan["runs"]), 2)
        self.assertEqual(plan["targets"][0]["campaign_id"], "foldarium-smoke")

    def test_duplicate_task_is_rejected(self) -> None:
        task = make_task("boltz2", {"seed": 0})
        with self.assertRaises(StagingError):
            build_staging_plan([task, dict(task)], adapter_version="0.1.0")

    def test_conflicting_target_definitions_are_rejected(self) -> None:
        other = dict(TARGET)
        other["entities"] = [
            {"type": "protein", "chain_ids": ["A"], "sequence": "ACDEFGHIKL"},
            {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
        ]
        with self.assertRaises(StagingError):
            build_staging_plan(
                [make_task("boltz2", {"seed": 0}), make_task("boltz2", {"seed": 1}, target=other)],
                adapter_version="0.1.0",
            )

    def test_empty_plan_is_rejected(self) -> None:
        with self.assertRaises(StagingError):
            build_staging_plan([], adapter_version="0.1.0")


class RenderTests(unittest.TestCase):
    def _sql(self) -> str:
        return render_staging_sql(
            build_staging_plan(
                [make_task("boltz2", {"seed": 0}), make_task("openfold3", {"model_seeds": 1})],
                adapter_version="0.1.0",
            )
        )

    def test_script_is_one_transaction(self) -> None:
        sql = self._sql()
        self.assertTrue(sql.rstrip().endswith("commit;"))
        self.assertEqual(sql.count("begin;"), 1)
        self.assertEqual(sql.count("commit;"), 1)

    def test_existing_runs_are_never_modified(self) -> None:
        sql = self._sql()
        self.assertIn("on conflict (run_id) do nothing", sql)
        self.assertNotIn("on conflict (run_id) do update", sql)

    def test_quotes_in_values_are_escaped(self) -> None:
        target = dict(TARGET)
        target["metadata"] = {"note": "o'brien's target"}
        sql = render_staging_sql(
            build_staging_plan([make_task("boltz2", {"seed": 0}, target=target)], adapter_version="0.1.0")
        )
        self.assertIn("o''brien''s", sql)
        self.assertNotIn("o'brien", sql)

    def test_json_columns_are_cast(self) -> None:
        self.assertIn("::jsonb", self._sql())


if __name__ == "__main__":
    unittest.main()
