from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class TransientMsaRetrySubmissionTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        if importlib.util.find_spec("modal") is None:
            raise unittest.SkipTest("Modal deployment dependency is not installed")
        path = Path(__file__).resolve().parents[1] / "deploy" / "modal_app.py"
        spec = importlib.util.spec_from_file_location("foldarium_test_modal_app", path)
        if spec is None or spec.loader is None:
            raise AssertionError("could not load Modal deployment adapter")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_failed_spawn_returns_exact_recoverable_authorization_report(self) -> None:
        module = self.deployment_module()
        run_id = "run_transient_fixture"
        authorization = {
            "status": "authorized",
            "requested_run_ids": [run_id],
            "authorized_run_ids": [run_id],
            "already_authorized_run_ids": [],
            "resubmission_authorized_run_ids": [],
            "approved_submission_run_ids": [run_id],
            "resubmit_already_authorized": False,
            "authorization_rows": [{"run_id": run_id, "action": "authorized"}],
            "confirmed_oom_run_ids": ["run_confirmed_oom"],
            "allowed_error_codes": [
                "msa_preprocessing_failed",
                "output_validation_failed",
            ],
            "task_payloads": {run_id: {"method": "boltz2"}},
        }

        class Coordinator:
            def authorize_transient_boltz_msa_retries(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                return authorization

        coordinator = Coordinator()

        class FailedWorker:
            @staticmethod
            def spawn(task_json: str):
                raise RuntimeError("simulated Modal acknowledgement failure")

        raw_function = module.retry_transient_boltz_msa_runs.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch.object(module, "run_transient_boltz_msa_retry", FailedWorker):
            report = raw_function(
                [run_id], ["run_confirmed_oom"], False
            )

        self.assertEqual(report["submission_status"], "submission-failed")
        self.assertEqual(report["submissions"], [])
        self.assertEqual(report["submitted_run_ids"], [])
        self.assertEqual(report["authorized_not_submitted_run_ids"], [run_id])
        self.assertEqual(
            report["submission_errors"],
            [
                {
                    "run_id": run_id,
                    "error_type": "RuntimeError",
                    "error": "Modal did not acknowledge the retry spawn",
                }
            ],
        )
        self.assertIn("resubmit_already_authorized=True", report["recovery"])
        self.assertEqual(coordinator.args, ([run_id],))
        self.assertEqual(
            coordinator.kwargs,
            {
                "confirmed_oom_run_ids": ["run_confirmed_oom"],
                "resubmit_already_authorized": False,
            },
        )

    def test_manual_weekly_assembly_accepts_only_an_explicit_safe_public_bucket(self) -> None:
        module = self.deployment_module()
        self.assertEqual(
            module._weekly_public_bucket("foldarium-weekly-quiz"),
            "foldarium-weekly-quiz",
        )
        for invalid in ("", "UPPERCASE", "../private", "bucket/name"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "safe Storage bucket"
            ):
                module._weekly_public_bucket(invalid)

    def test_bare_modal_deploy_fails_before_app_construction(self) -> None:
        path = Path(__file__).resolve().parents[1] / "deploy" / "modal_app.py"
        script = f"""
import importlib.util
import sys
sys.argv = ["modal", "deploy", {str(path)!r}]
spec = importlib.util.spec_from_file_location("bare_deploy_test", {str(path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
        environment = {
            key: value
            for key, value in os.environ.items()
            if key != "FOLDARIUM_DEPLOYMENT_CONFIG_SHA256"
        }
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("direct deployment is disabled", completed.stderr)


class NextweeklyAutomationTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        return TransientMsaRetrySubmissionTests.deployment_module()

    def test_window_is_bound_to_the_most_recent_utc_saturday(self) -> None:
        module = self.deployment_module()
        window = module._nextweekly_window(
            now=datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(window["release_date"], "2026-08-15")
        self.assertEqual(window["campaign_id"], "wwpdb-2026-08-15")
        self.assertEqual(
            window["round_id"], "preview-weekly-2026-08-15-nextweekly-v2"
        )
        self.assertEqual(window["opens_at"], "2026-08-15T03:00:00Z")
        self.assertEqual(window["closes_at"], "2026-08-19T00:00:00Z")

    def test_run_report_never_auto_retries_generic_legacy_failures(self) -> None:
        module = self.deployment_module()
        report = module._nextweekly_run_report(
            [
                {
                    "run_id": "run-msa",
                    "target_id": "target-msa",
                    "method": "boltz2",
                    "status": "failed",
                    "attempt_count": 1,
                    "max_attempts": 1,
                    "error_code": "msa_preprocessing_failed",
                    "task_payload": {
                        "resources": {
                            "gpu_class": "l4",
                            "timeout_seconds": 1800,
                        },
                        "config": {"msa_mode": "server"},
                    },
                    "result": {"duration_seconds": 10.0},
                },
                {
                    "run_id": "run-generic",
                    "method": "boltz2",
                    "status": "failed",
                    "attempt_count": 1,
                    "max_attempts": 1,
                    "error_code": "output_validation_failed",
                },
                {
                    "run_id": "run-oom",
                    "method": "openfold3",
                    "status": "failed",
                    "attempt_count": 1,
                    "max_attempts": 1,
                    "error_code": "gpu_out_of_memory",
                },
            ]
        )
        self.assertEqual(report["retryable_run_ids"], ["run-msa"])
        self.assertEqual(report["status_counts"]["failed"], 3)

    def test_run_report_exact_legacy_map_matches_authorizer_and_near_misses_fail(self) -> None:
        module = self.deployment_module()
        from foldarium_pipeline.supabase import REVIEWED_LEGACY_PREDICTION_RETRIES

        self.assertEqual(
            module.NEXTWEEKLY_REVIEWED_LEGACY_RETRIES,
            REVIEWED_LEGACY_PREDICTION_RETRIES,
        )
        specs = [
            (
                "run_ebb8012256ebff410610bbd3",
                "9S7U",
                "openfold3",
                "output_validation_failed",
                "gpu_out_of_memory",
                "a100-40gb",
                1800,
            ),
            (
                "run_fe3f5b2f13d64c508aa61f39",
                "31ZN",
                "boltz2",
                "output_validation_failed",
                "gpu_out_of_memory",
                "a100-40gb",
                1800,
            ),
            (
                "run_62f75d944367889691bfc897",
                "32QB",
                "openfold3",
                "timeout",
                "msa_generation_timeout",
                "l4",
                4500,
            ),
        ]
        rows = [
            {
                "run_id": run_id,
                "target_id": target_id,
                "method": method,
                "status": "failed",
                "attempt_count": 1,
                "max_attempts": 1,
                "error_code": source_error,
                "task_payload": {
                    "resources": {"gpu_class": "l4", "timeout_seconds": 1800},
                    "config": {"msa_mode": "server"},
                },
                "result": {"duration_seconds": 10.0},
            }
            for run_id, target_id, method, source_error, *_ in specs
        ]
        report = module._nextweekly_run_report(rows)
        self.assertEqual(
            [
                (
                    item["run_id"],
                    item["retry_kind"],
                    item["retry_gpu_class"],
                    item["retry_timeout_seconds"],
                    item["reviewed_legacy"],
                )
                for item in report["retry_candidates"]
            ],
            [
                (run_id, retry_kind, gpu, timeout, True)
                for run_id, _target, _method, _source, retry_kind, gpu, timeout in sorted(
                    specs
                )
            ],
        )
        for row_index, field, wrong in (
            (0, "target_id", "wrong-target"),
            (0, "method", "boltz2"),
            (0, "error_code", "wrong-code"),
            (1, "target_id", "wrong-target"),
            (1, "method", "openfold3"),
            (1, "error_code", "wrong-code"),
            (2, "target_id", "wrong-target"),
            (2, "method", "boltz2"),
            (2, "error_code", "wrong-code"),
        ):
            mutated = deepcopy(rows)
            mutated[row_index][field] = wrong
            rejected = module._nextweekly_run_report(mutated)
            self.assertNotIn(
                rows[row_index]["run_id"], rejected["retryable_run_ids"]
            )

        wrong_timeout = deepcopy(rows)
        wrong_timeout[0]["task_payload"]["resources"]["timeout_seconds"] = 1700
        self.assertNotIn(
            rows[0]["run_id"],
            module._nextweekly_run_report(wrong_timeout)["retryable_run_ids"],
        )

    @staticmethod
    def _terminal_rows(
        *, duration_seconds: float = 1700.0, retryable_count: int = 0
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for target_index in range(40):
            for method in ("boltz2", "openfold3"):
                retryable = method == "boltz2" and target_index < retryable_count
                rows.append(
                    {
                        "run_id": f"run-{target_index:02d}-{method}",
                        "method": method,
                        "status": "failed" if retryable else "succeeded",
                        "attempt_count": 1,
                        "max_attempts": 1,
                        "error_code": (
                            "msa_preprocessing_failed" if retryable else None
                        ),
                        "task_payload": {
                            "resources": {
                                "gpu_class": "l4",
                                "timeout_seconds": 1800,
                            },
                            "config": {"msa_mode": "server"},
                        },
                        "result": {"duration_seconds": duration_seconds},
                    }
                )
        return rows

    @staticmethod
    def _retry_candidates(count: int, *, kind: str = "msa_preprocessing_failed"):
        return [
            {
                "run_id": f"candidate-{index}",
                "retry_kind": kind,
                "retry_gpu_class": (
                    "a100-40gb" if kind == "gpu_out_of_memory" else "l4"
                ),
                "retry_timeout_seconds": (
                    4500 if kind == "msa_generation_timeout" else 1800
                ),
            }
            for index in range(count)
        ]

    def test_retry_budget_uses_exact_terminal_durations(self) -> None:
        module = self.deployment_module()
        budget = module._nextweekly_retry_budget(
            self._terminal_rows(), self._retry_candidates(6)
        )
        self.assertEqual(budget["status"], "available")
        self.assertEqual(budget["original_consumed_seconds"], 136000.0)
        self.assertEqual(budget["authorized_retry_count"], 0)
        self.assertEqual(budget["retry_reserved_seconds"], 0)
        self.assertEqual(budget["remaining_seconds"], 8000.0)
        self.assertEqual(budget["remaining_retry_slots"], 4)

    def test_retry_budget_fails_closed_on_missing_duration(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows()
        rows[17]["result"] = None
        budget = module._nextweekly_retry_budget(rows, self._retry_candidates(1))
        self.assertEqual(budget["status"], "invalid-run-accounting")
        self.assertFalse(budget["authorization_ready"])
        self.assertEqual(budget["remaining_retry_slots"], 0)
        self.assertEqual(budget["invalid_run_ids"], [rows[17]["run_id"]])

    def test_retry_budget_treats_authorized_rows_as_two_full_commands(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows(duration_seconds=1000.0)
        for row in rows[:20:2]:  # Ten Boltz rows already authorized once.
            row["max_attempts"] = 2
            row["attempt_count"] = 2
            row["result"] = {"duration_seconds": 12.0}
        budget = module._nextweekly_retry_budget(rows, self._retry_candidates(10))
        self.assertEqual(budget["authorized_retry_count"], 10)
        self.assertEqual(budget["original_consumed_seconds"], 88000.0)
        self.assertEqual(budget["retry_reserved_seconds"], 45000)
        self.assertEqual(budget["consumed_or_reserved_seconds"], 133000.0)
        self.assertEqual(budget["remaining_retry_slots"], 6)

    def test_retry_budget_does_not_recover_reserved_slots_on_later_tick(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows(duration_seconds=1700.0)
        for row in rows[:8:2]:  # The prior tick authorized its four available slots.
            row["max_attempts"] = 2
            row["attempt_count"] = 2
            row["result"] = {"duration_seconds": 1.0}
        budget = module._nextweekly_retry_budget(rows, self._retry_candidates(4))
        self.assertEqual(budget["authorized_retry_count"], 4)
        self.assertEqual(budget["consumed_or_reserved_seconds"], 154400.0)
        self.assertEqual(budget["status"], "exhausted")
        self.assertEqual(budget["remaining_retry_slots"], 0)

    def test_retry_budget_is_exhausted_at_40_command_hours(self) -> None:
        module = self.deployment_module()
        budget = module._nextweekly_retry_budget(
            self._terminal_rows(duration_seconds=1800.0),
            self._retry_candidates(1),
        )
        self.assertEqual(budget["status"], "exhausted")
        self.assertFalse(budget["authorization_ready"])
        self.assertEqual(budget["remaining_seconds"], 0.0)
        self.assertEqual(budget["remaining_retry_slots"], 0)

    def test_retry_budget_enforces_weighted_gpu_cost_not_only_seconds(self) -> None:
        module = self.deployment_module()
        self.assertEqual(module.NEXTWEEKLY_L4_RATE_USD_PER_SECOND, 0.00030992)
        self.assertEqual(
            module.NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND, 0.00075884
        )
        # Leave $0.80 of command authority. That is enough raw time and money
        # for one L4/1800 retry, but not an A100-40GB/1800 retry.
        remaining_cost = 0.80
        duration = (
            module.NEXTWEEKLY_GPU_COST_BUDGET_USD - remaining_cost
        ) / (80 * module.NEXTWEEKLY_L4_RATE_USD_PER_SECOND)
        candidates = [
            self._retry_candidates(1, kind="gpu_out_of_memory")[0],
            {
                **self._retry_candidates(1)[0],
                "run_id": "candidate-l4",
            },
        ]
        budget = module._nextweekly_retry_budget(
            self._terminal_rows(duration_seconds=duration), candidates
        )
        self.assertGreater(budget["remaining_seconds"], 1800)
        self.assertLess(
            budget["remaining_cost_usd"],
            1800 * module.NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND,
        )
        self.assertEqual(
            budget["authorized_candidate_run_ids"], ["candidate-l4"]
        )

    def test_retry_execution_task_preserves_identity_and_records_resources(self) -> None:
        module = self.deployment_module()
        from foldarium_pipeline.contracts import SCHEMA_VERSION, make_prediction_task

        task = make_prediction_task(
            campaign_id="wwpdb-2026-08-15",
            target={
                "schema_version": SCHEMA_VERSION,
                "target_id": "31ZN",
                "entities": [
                    {
                        "type": "protein",
                        "chain_ids": ["A"],
                        "sequence": "ACDEFGHIK",
                    },
                    {
                        "type": "ligand",
                        "chain_ids": ["L"],
                        "smiles": "CCO",
                    },
                ],
            },
            method="boltz2",
            method_version="2.2.1",
            container_image="registry.example/foldarium/boltz@sha256:" + "a" * 64,
            config={"msa_mode": "server"},
            output_uri_prefix="s3://foldarium/predictions",
            resources={"gpu_class": "l4", "timeout_seconds": 1800},
        )
        original = deepcopy(task)
        request = {
            "run_id": task["task_id"],
            "target_id": "31ZN",
            "method": "boltz2",
            "source_error_code": "output_validation_failed",
            "retry_kind": "gpu_out_of_memory",
            "retry_gpu_class": "a100-40gb",
            "retry_timeout_seconds": 1800,
            "reviewed_legacy": True,
        }
        retry_task = module._retry_execution_task(task, request)
        self.assertEqual(task, original)
        self.assertEqual(retry_task["task_id"], task["task_id"])
        for field in (
            "target",
            "method",
            "method_version",
            "container_image",
            "config",
            "output_uri_prefix",
        ):
            self.assertEqual(retry_task[field], task[field])
        self.assertEqual(retry_task["resources"]["gpu_class"], "a100-40gb")
        self.assertEqual(retry_task["resources"]["timeout_seconds"], 1800)
        self.assertEqual(
            retry_task["resources"]["retry_policy"],
            {
                "retry_kind": "gpu_out_of_memory",
                "source_error_code": "output_validation_failed",
                "reviewed_legacy": True,
                "original_gpu_class": "l4",
                "original_timeout_seconds": 1800,
            },
        )

    def test_retry_function_routes_gpu_and_outer_timeout(self) -> None:
        module = self.deployment_module()

        class FakeFunction:
            def __init__(self):
                self.options = []

            def with_options(self, **kwargs):
                self.options.append(kwargs)
                return self

        openfold = FakeFunction()
        boltz = FakeFunction()
        with patch.object(module, "run_openfold3_retry", openfold), patch.object(
            module, "run_boltz2_retry", boltz
        ):
            self.assertIs(
                module._retry_function(
                    {"method": "openfold3", "retry_kind": "gpu_out_of_memory"}
                ),
                openfold,
            )
            self.assertIs(
                module._retry_function(
                    {"method": "boltz2", "retry_kind": "msa_generation_timeout"}
                ),
                boltz,
            )
        self.assertEqual(openfold.options[0]["gpu"], "A100-40GB")
        self.assertEqual(openfold.options[0]["cpu"], 8.0)
        self.assertEqual(openfold.options[0]["memory"], 32768)
        self.assertEqual(openfold.options[0]["timeout"], 2100)
        self.assertEqual(openfold.options[0]["max_containers"], 1)
        self.assertEqual(boltz.options[0]["timeout"], 4800)
        self.assertEqual(boltz.options[0]["max_containers"], 1)

    def test_execute_lease_outlives_normal_and_long_retry_containers(self) -> None:
        module = self.deployment_module()
        from foldarium_pipeline.contracts import SCHEMA_VERSION, make_prediction_task

        base = make_prediction_task(
            campaign_id="wwpdb-2026-08-15",
            target={
                "schema_version": SCHEMA_VERSION,
                "target_id": "lease-test",
                "entities": [
                    {
                        "type": "protein",
                        "chain_ids": ["A"],
                        "sequence": "ACDEFGHIK",
                    },
                    {
                        "type": "ligand",
                        "chain_ids": ["L"],
                        "smiles": "CCO",
                    },
                ],
            },
            method="openfold3",
            method_version="0.4.4",
            container_image="registry.example/foldarium/of3@sha256:" + "a" * 64,
            config={"msa_mode": "server"},
            output_uri_prefix="s3://foldarium/predictions",
            resources={"gpu_class": "l4", "timeout_seconds": 1800},
        )
        long_retry = deepcopy(base)
        long_retry["resources"] = {
            "gpu_class": "l4",
            "timeout_seconds": 4500,
            "retry_policy": {
                "retry_kind": "msa_generation_timeout",
                "source_error_code": "timeout",
                "reviewed_legacy": True,
                "original_gpu_class": "l4",
                "original_timeout_seconds": 1800,
            },
        }

        class Publisher:
            def __init__(self):
                self.claims = []

            def claim_run(self, run_id, worker_id, lease_seconds):
                self.claims.append((run_id, lease_seconds))
                return True

            def publish_result(self, result, output_root, worker_id):
                return {"status": "published"}

        publisher = Publisher()
        result = {
            "status": "succeeded",
            "task_id": base["task_id"],
            "samples": [],
        }
        with patch(
            "foldarium_pipeline.supabase.SupabasePublisher.from_env",
            return_value=publisher,
        ), patch(
            "foldarium_pipeline.worker.execute_task_json",
            return_value=result,
        ):
            module._execute(base)
            module._execute(long_retry)
        self.assertEqual(
            [lease for _run_id, lease in publisher.claims], [2700, 5400]
        )
        self.assertGreater(2700, 2100)
        self.assertGreater(5400, 4800)

    def test_tick_caps_retry_batch_to_campaign_budget(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows(retryable_count=6)

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return rows

        class RetryRemote:
            calls = []

            @classmethod
            def remote(cls, retry_requests, resubmit):
                cls.calls.append((retry_requests, resubmit))
                return {
                    "submission_status": "submitted",
                    "submissions": [
                        {
                            "run_id": request["run_id"],
                            "modal_call_id": f"call-{request['run_id']}",
                        }
                        for request in retry_requests
                    ],
                }

        class ForbiddenAssembly:
            @staticmethod
            def remote(*args):
                raise AssertionError("an authorized retry batch must not assemble")

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(
            module, "retry_prediction_runs", RetryRemote
        ), patch.object(module, "assemble_weekly_quiz_round", ForbiddenAssembly):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "prediction-retries-submitted")
        self.assertEqual(len(result["retry_run_ids"]), 4)
        self.assertEqual(result["automatic_retry_budget"]["remaining_retry_slots"], 4)
        self.assertEqual(
            [request["run_id"] for request in RetryRemote.calls[0][0]],
            result["retry_run_ids"],
        )

    def test_tick_skips_retry_and_assembles_when_accounting_is_invalid(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows(retryable_count=1)
        rows[-1]["result"] = None

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return rows

        class ForbiddenRetry:
            @staticmethod
            def remote(*args):
                raise AssertionError("invalid accounting must not authorize a retry")

        class AssemblyRemote:
            @staticmethod
            def remote(*args):
                return {"status": "opened", "round_id": args[1]}

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(
            module, "retry_prediction_runs", ForbiddenRetry
        ), patch.object(
            module, "assemble_weekly_quiz_round", AssemblyRemote
        ), patch.object(module, "_weekly_public_bucket", return_value="public-weekly"):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "preview-opened")
        self.assertEqual(result["automatic_retry_status"], "skipped")
        self.assertEqual(
            result["automatic_retry_budget"]["status"],
            "invalid-run-accounting",
        )

    def test_tick_skips_retry_and_assembles_when_budget_is_exhausted(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows(duration_seconds=1800.0, retryable_count=1)

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return rows

        class ForbiddenRetry:
            @staticmethod
            def remote(*args):
                raise AssertionError("an exhausted budget must not authorize a retry")

        class AssemblyRemote:
            @staticmethod
            def remote(*args):
                return {"status": "opened", "round_id": args[1]}

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(
            module, "retry_prediction_runs", ForbiddenRetry
        ), patch.object(
            module, "assemble_weekly_quiz_round", AssemblyRemote
        ), patch.object(module, "_weekly_public_bucket", return_value="public-weekly"):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "preview-opened")
        self.assertEqual(result["automatic_retry_status"], "skipped")
        self.assertEqual(result["automatic_retry_budget"]["status"], "exhausted")
        self.assertEqual(
            result["automatic_retry_budget"]["remaining_retry_slots"], 0
        )

    def test_tick_waits_for_every_active_prediction_before_assembly(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return [
                    {
                        "run_id": "run-active",
                        "method": "openfold3",
                        "status": "running",
                        "attempt_count": 1,
                        "max_attempts": 1,
                        "error_code": None,
                    }
                ]

        class ForbiddenRemote:
            @staticmethod
            def remote(*args):
                raise AssertionError("active campaigns must not assemble or retry")

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(module, "assemble_weekly_quiz_round", ForbiddenRemote), patch.object(
            module, "retry_prediction_runs", ForbiddenRemote
        ):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "waiting-for-predictions")
        self.assertEqual(result["active_run_ids"], ["run-active"])

    def test_tick_waits_for_authorized_retry_ack_gap(self) -> None:
        module = self.deployment_module()
        rows = self._terminal_rows()
        rows[0].update(
            {
                "status": "failed",
                "attempt_count": 1,
                "max_attempts": 2,
                "error_code": "gpu_out_of_memory",
            }
        )

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return rows

        class ForbiddenRemote:
            @staticmethod
            def remote(*args):
                raise AssertionError("ack-gap rows must neither retry nor assemble")

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(module, "assemble_weekly_quiz_round", ForbiddenRemote), patch.object(
            module, "retry_prediction_runs", ForbiddenRemote
        ):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "waiting-for-authorized-retries")
        self.assertEqual(
            result["authorized_retry_pending_run_ids"], [rows[0]["run_id"]]
        )

    def test_existing_v2_is_immutable_and_short_circuits_all_work(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                self.assertEqual(
                    round_id, "preview-weekly-2026-08-15-nextweekly-v2"
                )
                return True

        self_ref = self
        Coordinator.weekly_quiz_round_exists = staticmethod(
            lambda round_id: (
                self_ref.assertEqual(
                    round_id, "preview-weekly-2026-08-15-nextweekly-v2"
                )
                or True
            )
        )
        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "preview-ready")

    def test_terminal_campaign_opens_preview_and_never_production(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            @staticmethod
            def weekly_quiz_round_exists(round_id):
                return False

            @staticmethod
            def weekly_campaign_exists(campaign_id):
                return True

            @staticmethod
            def campaign_prediction_run_statuses(campaign_id):
                return [
                    {
                        "run_id": "run-of3",
                        "method": "openfold3",
                        "status": "succeeded",
                        "attempt_count": 1,
                        "max_attempts": 1,
                        "error_code": None,
                    },
                    {
                        "run_id": "run-boltz",
                        "method": "boltz2",
                        "status": "succeeded",
                        "attempt_count": 1,
                        "max_attempts": 1,
                        "error_code": None,
                    },
                ]

        class AssemblyRemote:
            calls = []

            @classmethod
            def remote(cls, *args):
                cls.calls.append(args)
                return {"status": "opened", "round_id": args[1]}

        class ForbiddenRetry:
            @staticmethod
            def remote(*args):
                raise AssertionError("succeeded campaigns must not retry")

        raw_function = module.nextweekly_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch.object(module, "assemble_weekly_quiz_round", AssemblyRemote), patch.object(
            module, "retry_prediction_runs", ForbiddenRetry
        ), patch.object(module, "_weekly_public_bucket", return_value="public-weekly"):
            result = raw_function("2026-08-15")
        self.assertEqual(result["status"], "preview-opened")
        self.assertEqual(len(AssemblyRemote.calls), 1)
        args = AssemblyRemote.calls[0]
        self.assertEqual(args[0], "wwpdb-2026-08-15")
        self.assertEqual(args[1], "preview-weekly-2026-08-15-nextweekly-v2")
        self.assertIs(args[4], True)
        self.assertIs(args[5], module.NEXTWEEKLY_INCLUDE_POSE_METRICS)
        self.assertEqual(args[7], "preview")
        self.assertNotIn("production", args)

class WeeklyMetricReuseTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        return TransientMsaRetrySubmissionTests.deployment_module()

    @staticmethod
    def source_private_index(*, scoring):
        return {
            "items": [
                {
                    "id": "target-1",
                    "choices": [
                        {
                            "run_id": "run-existing",
                            "sample_id": "sample-existing",
                            "artifact_sha256": "a" * 64,
                            "scoring": scoring,
                        }
                    ],
                }
            ]
        }

    def run_assembly(self, module, *, include_pose_metrics, stage_quiz, scoring):
        class Coordinator:
            def __init__(self, storage_bucket):
                self.storage_bucket = storage_bucket

            def campaign_prediction_outputs(self, campaign_id):
                self.campaign_id = campaign_id
                return []

            def weekly_quiz_reveal_inputs(self, round_id):
                self.source_round_id = round_id
                return (
                    {"campaign_id": "campaign-1"},
                    json.dumps(
                        self_test.source_private_index(scoring=scoring)
                    ).encode(),
                )

            def download_content_object(self, *args, **kwargs):
                raise AssertionError("staging fixture must not download artifacts")

        self_test = self
        private = Coordinator("private-predictions")
        public = Coordinator("public-weekly-quiz")

        def from_env(environment=None):
            return private if environment is None else public

        published = {
            "status": "staged",
            "round_id": "round-new",
            "item_count": 1,
            "choice_count": 2,
            "blind_manifest_sha256": "b" * 64,
        }
        raw_function = module.assemble_weekly_quiz_round.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            side_effect=from_env,
        ), patch(
            "foldarium_pipeline.weekly_quiz.select_complete_method_pairs",
            return_value=([{"target_id": "target-1"}], [], []),
        ), patch(
            "foldarium_pipeline.weekly_quiz.stage_weekly_quiz",
            side_effect=stage_quiz,
        ), patch(
            "foldarium_pipeline.weekly_quiz.publish_staged_weekly_quiz",
            return_value=published,
        ):
            result = raw_function(
                campaign_id="campaign-1",
                round_id="round-new",
                opens_at="2026-08-08T00:00:00Z",
                closes_at="2026-08-12T00:00:00Z",
                include_pose_metrics=include_pose_metrics,
                round_environment="preview",
                public_quiz_bucket="public-weekly-quiz",
                reuse_pose_metrics_from_round_id="round-source",
            )
        self.assertEqual(private.campaign_id, "campaign-1")
        self.assertEqual(private.source_round_id, "round-source")
        return result

    def test_reuses_exact_choice_and_remotely_scores_only_missing_choice(self) -> None:
        module = self.deployment_module()
        from foldarium_pipeline.clustering import choice_order_digest

        existing_identity = {
            "run_id": "run-existing",
            "sample_id": "sample-existing",
            "artifact_sha256": "a" * 64,
        }
        missing_identity = {
            "run_id": "run-existing",
            "sample_id": "sample-existing",
            "artifact_sha256": "c" * 64,
        }
        existing_pose_id = choice_order_digest(
            "round-new", "target-1", existing_identity
        )
        missing_pose_id = choice_order_digest(
            "round-new", "target-1", missing_identity
        )
        source_scoring = {
            "pose_id": "source-pose-id",
            "scores": {"smina_affinity_kcal_mol": -7.1},
            "provenance": {"mode": "score_only"},
        }
        captured = {}

        class RemoteScorer:
            def __init__(self):
                self.calls = []

            def remote(self, *args):
                self.calls.append(args)
                return {"pose_id": args[3], "scores": {"remote": True}}

        remote = RemoteScorer()

        def stage_quiz(_complete, temporary, **kwargs):
            root = Path(temporary)
            protein_path = root / "protein.pdb"
            ligand_path = root / "ligand.sdf"
            protein_path.write_bytes(b"protein-bytes")
            ligand_path.write_bytes(b"ligand-bytes")
            scorer = kwargs["choice_batch_scorer"]
            captured["reused"], captured["missing"] = scorer(
                (
                    {
                        "protein_path": protein_path,
                        "ligand_path": ligand_path,
                        "ligand_smiles": "CCO",
                        "pose_id": existing_pose_id,
                    },
                    {
                        "protein_path": protein_path,
                        "ligand_path": ligand_path,
                        "ligand_smiles": "CCO",
                        "pose_id": missing_pose_id,
                    },
                )
            )
            return {"items": [{"clustering": {"cluster_count": 2}}]}

        with patch.object(
            module.modal.Function, "from_name", return_value=remote
        ) as from_name:
            result = self.run_assembly(
                module,
                include_pose_metrics=True,
                stage_quiz=stage_quiz,
                scoring=source_scoring,
            )

        self.assertEqual(captured["reused"]["pose_id"], existing_pose_id)
        self.assertEqual(
            captured["reused"]["provenance"]["metric_reuse"],
            {
                "source_round_id": "round-source",
                "policy": "rigid-transform-invariant-fixed-pose-metrics/v1",
            },
        )
        self.assertEqual(source_scoring["pose_id"], "source-pose-id")
        self.assertEqual(captured["missing"]["pose_id"], missing_pose_id)
        from_name.assert_called_once_with(
            module.WEEKLY_SCORING_APP_NAME,
            module.WEEKLY_SCORING_FUNCTION_NAME,
        )
        self.assertEqual(len(remote.calls), 1)
        self.assertEqual(
            remote.calls[0],
            (
                b"protein-bytes",
                b"ligand-bytes",
                "CCO",
                missing_pose_id,
                hashlib.sha256(b"protein-bytes").hexdigest(),
                hashlib.sha256(b"ligand-bytes").hexdigest(),
            ),
        )
        self.assertTrue(result["pose_metrics_included"])
        self.assertEqual(result["pose_metrics_reused_from_round_id"], "round-source")

    def test_missing_reused_choice_remains_fail_closed_without_metric_scoring(self) -> None:
        module = self.deployment_module()
        from foldarium_pipeline.clustering import choice_order_digest

        missing_pose_id = choice_order_digest(
            "round-new",
            "target-1",
            {
                "run_id": "run-existing",
                "sample_id": "sample-existing",
                "artifact_sha256": "c" * 64,
            },
        )

        def stage_quiz(_complete, temporary, **kwargs):
            root = Path(temporary)
            protein_path = root / "protein.pdb"
            ligand_path = root / "ligand.sdf"
            protein_path.write_bytes(b"protein-bytes")
            ligand_path.write_bytes(b"ligand-bytes")
            return kwargs["choice_batch_scorer"](
                (
                    {
                        "protein_path": protein_path,
                        "ligand_path": ligand_path,
                        "ligand_smiles": "CCO",
                        "pose_id": missing_pose_id,
                    },
                )
            )

        with patch.object(module.modal.Function, "from_name") as from_name:
            with self.assertRaisesRegex(
                RuntimeError, "lacks an exact run/sample choice"
            ):
                self.run_assembly(
                    module,
                    include_pose_metrics=False,
                    stage_quiz=stage_quiz,
                    scoring={"pose_id": "source-pose-id"},
                )
        from_name.assert_not_called()


class WeeklyScoringConcurrencyTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        return TransientMsaRetrySubmissionTests.deployment_module()

    def test_scores_at_most_eight_calls_and_restores_request_order(self) -> None:
        import threading
        import time

        module = self.deployment_module()
        lock = threading.Lock()
        first_wave = threading.Barrier(module.WEEKLY_SCORING_MAX_WORKERS)
        active = 0
        maximum_active = 0

        def scorer(*, pose_id, **_kwargs):
            nonlocal active, maximum_active
            index = int(pose_id.split("-")[-1])
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                if index < module.WEEKLY_SCORING_MAX_WORKERS:
                    first_wave.wait(timeout=2)
                time.sleep((8 - index) * 0.002)
                return {"pose_id": pose_id}
            finally:
                with lock:
                    active -= 1

        requests = tuple(
            {
                "protein_path": Path(f"protein-{index}.pdb"),
                "ligand_path": Path(f"ligand-{index}.pdb"),
                "ligand_smiles": "CCO",
                "pose_id": f"pose-{index}",
            }
            for index in range(8)
        )
        results = module._score_weekly_choices_concurrently(scorer, requests)

        self.assertEqual(module.WEEKLY_SCORING_MAX_WORKERS, 8)
        self.assertEqual(maximum_active, 8)
        self.assertEqual(
            [result["pose_id"] for result in results],
            [request["pose_id"] for request in requests],
        )


class WeeklyLifecycleReconciliationTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        return TransientMsaRetrySubmissionTests.deployment_module()

    def test_production_window_derives_preview_identity(self) -> None:
        module = self.deployment_module()
        window = module._weekly_production_window("2026-08-15")
        self.assertEqual(window["preview_round_id"], "preview-weekly-2026-08-15-nextweekly-v2")
        self.assertEqual(window["round_id"], "weekly-2026-08-15-beta-v1")
        self.assertEqual(window["environment"], "production")

    def test_lifecycle_report_includes_retrospective_gates(self) -> None:
        module = self.deployment_module()
        report = module._lifecycle_deployment_report()
        self.assertEqual(report["weekly_retrospective_publication"]["cron"], "45 0-5 * * 3")
        self.assertIn(
            "20260826190000_require_retrospective_vote_scope.sql",
            report["required_migrations_before_publication"],
        )

    def test_public_private_coordinators_use_reviewed_bucket_split(self) -> None:
        module = self.deployment_module()
        environments: list[dict[str, str]] = []

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

        class PublicCoordinator:
            storage_bucket = "foldarium-weekly-quiz"

        def from_env(environment=None):
            environments.append(dict(environment or os.environ))
            if environment is None:
                return PrivateCoordinator()
            return PublicCoordinator()

        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            side_effect=from_env,
        ), patch.object(module, "_weekly_public_bucket", return_value="foldarium-weekly-quiz"):
            private, public = module._weekly_quiz_public_private_coordinators()

        self.assertIsInstance(private, PrivateCoordinator)
        self.assertIsInstance(public, PublicCoordinator)
        self.assertEqual(
            environments[1]["FOLDARIUM_STORAGE_BUCKET"],
            "foldarium-weekly-quiz",
        )

    def test_selector_backfill_uses_exact_round_row_and_coordinators(self) -> None:
        module = self.deployment_module()
        round_row = {
            "round_id": "weekly-2026-08-15-beta-v1",
            "blind_manifest": {"round_id": "weekly-2026-08-15-beta-v1", "items": []},
            "metadata": {},
        }
        calls: list[tuple] = []

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

            def weekly_quiz_round(self, round_id: str):
                self.round_id = round_id
                return round_row

        class PublicCoordinator:
            storage_bucket = "foldarium-weekly-quiz"

        private = PrivateCoordinator()
        public = PublicCoordinator()

        def backfill(round_row_arg, *, public_coordinator, private_coordinator, register_catalog):
            calls.append(
                (
                    round_row_arg,
                    public_coordinator,
                    private_coordinator,
                    register_catalog,
                )
            )
            return {"kit_sha256": "a" * 64, "registered": True}

        with patch(
            "foldarium_pipeline.weekly_quiz.backfill_selector_kit_for_round",
            side_effect=backfill,
            create=True,
        ):
            result = module._attempt_production_selector_kit_registration(
                "weekly-2026-08-15-beta-v1",
                private_coordinator=private,
                public_coordinator=public,
            )

        self.assertEqual(result["status"], "registered")
        self.assertEqual(private.round_id, "weekly-2026-08-15-beta-v1")
        self.assertEqual(calls[0][0], round_row)
        self.assertIs(calls[0][1], public)
        self.assertIs(calls[0][2], private)
        self.assertTrue(calls[0][3])

    def test_production_promotion_is_idempotent_when_round_exists_without_registration(
        self,
    ) -> None:
        module = self.deployment_module()

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

            def weekly_quiz_round_exists(self, round_id):
                return round_id == "weekly-2026-08-15-beta-v1"

        with patch.object(
            module,
            "_weekly_quiz_public_private_coordinators",
            return_value=(PrivateCoordinator(), object()),
        ), patch.object(
            module, "_attempt_production_selector_kit_registration"
        ) as register:
            raw_function = module.weekly_production_promotion_tick.get_raw_f()
            report = raw_function("2026-08-15")

        self.assertEqual(report["status"], "production-ready")
        register.assert_not_called()

    def test_existing_production_round_retries_selector_registration(self) -> None:
        module = self.deployment_module()
        register_calls: list[str] = []

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

            def weekly_quiz_round_exists(self, round_id):
                return round_id == "weekly-2026-08-15-beta-v1"

        def register(round_id, *, private_coordinator, public_coordinator):
            register_calls.append(round_id)
            return {
                "status": "registered",
                "retryable": False,
                "kit_sha256": "b" * 64,
                "registered": True,
            }

        raw_function = module.weekly_production_promotion_tick.get_raw_f()
        with patch.object(
            module,
            "_weekly_quiz_public_private_coordinators",
            return_value=(PrivateCoordinator(), object()),
        ), patch.object(
            module,
            "_attempt_production_selector_kit_registration",
            side_effect=register,
        ), patch.dict(os.environ, {module.WEEKLY_PRODUCTION_OPEN_ENV: "1", module.WEEKLY_REGISTER_SELECTOR_KIT_ENV: "1"}):
            report = raw_function("2026-08-15")

        self.assertEqual(report["status"], "production-ready")
        self.assertEqual(register_calls, ["weekly-2026-08-15-beta-v1"])
        self.assertEqual(report["selector_kit_status"], "registered")

    def test_selector_module_unavailable_is_non_mutating(self) -> None:
        module = self.deployment_module()

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

            def weekly_quiz_round_exists(self, round_id):
                return round_id == "weekly-2026-08-15-beta-v1"

        with patch.object(
            module,
            "_weekly_quiz_public_private_coordinators",
            return_value=(PrivateCoordinator(), object()),
        ), patch.object(
            module,
            "_attempt_production_selector_kit_registration",
            return_value={
                "status": "skipped-module-unavailable",
                "retryable": False,
            },
        ), patch.dict(os.environ, {module.WEEKLY_PRODUCTION_OPEN_ENV: "1", module.WEEKLY_REGISTER_SELECTOR_KIT_ENV: "1"}):
            report = module.weekly_production_promotion_tick.get_raw_f()("2026-08-15")

        self.assertEqual(report["status"], "production-ready-selector-kit-skipped")
        self.assertEqual(report["selector_kit_status"], "skipped-module-unavailable")
        self.assertFalse(report["selector_kit_retryable"])

    def test_selector_registration_failure_remains_retryable_on_later_tick(self) -> None:
        module = self.deployment_module()
        outcomes = []

        class PrivateCoordinator:
            storage_bucket = "private-predictions"

            def weekly_quiz_round_exists(self, round_id):
                return round_id == "weekly-2026-08-15-beta-v1"

        def register(round_id, *, private_coordinator, public_coordinator):
            if len(outcomes) == 0:
                return {
                    "status": "failed:RuntimeError",
                    "retryable": True,
                    "error": "catalog unavailable",
                }
            return {
                "status": "registered",
                "retryable": False,
                "kit_sha256": "c" * 64,
                "registered": True,
            }

        raw_function = module.weekly_production_promotion_tick.get_raw_f()
        with patch.object(
            module,
            "_weekly_quiz_public_private_coordinators",
            return_value=(PrivateCoordinator(), object()),
        ), patch.object(
            module,
            "_attempt_production_selector_kit_registration",
            side_effect=register,
        ), patch.dict(os.environ, {module.WEEKLY_PRODUCTION_OPEN_ENV: "1", module.WEEKLY_REGISTER_SELECTOR_KIT_ENV: "1"}):
            first = raw_function("2026-08-15")
            outcomes.append(first)
            second = raw_function("2026-08-15")
            outcomes.append(second)

        self.assertEqual(first["status"], "production-ready-selector-kit-retryable")
        self.assertTrue(first["selector_kit_retryable"])
        self.assertEqual(second["status"], "production-ready")
        self.assertEqual(second["selector_kit_status"], "registered")

    def test_lifecycle_preflight_reports_round_existence(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            def weekly_quiz_round_exists(self, round_id):
                return round_id.startswith("preview-weekly-")

            def weekly_campaign_exists(self, campaign_id):
                self.campaign_id = campaign_id
                return False

            def current_weekly_quiz_round(self):
                return {"round_id": "weekly-2026-08-08-v2"}

        coordinator = Coordinator()
        raw_function = module.weekly_lifecycle_preflight.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ):
            report = raw_function("2026-08-15")

        self.assertTrue(report["preview"]["exists"])
        self.assertFalse(report["production"]["exists"])
        self.assertEqual(coordinator.campaign_id, "wwpdb-2026-08-15")
        self.assertEqual(report["current_production_round_id"], "weekly-2026-08-08-v2")


class WednesdayRevealDeploymentTests(unittest.TestCase):
    @staticmethod
    def deployment_module():
        return TransientMsaRetrySubmissionTests.deployment_module()

    def test_default_round_is_most_recent_utc_saturday(self) -> None:
        module = self.deployment_module()
        self.assertEqual(
            module._default_weekly_round_id(
                datetime(2026, 8, 12, 0, 5, tzinfo=timezone.utc)
            ),
            "weekly-2026-08-08",
        )
        self.assertEqual(
            module._default_weekly_round_id(
                datetime(2026, 8, 8, 23, 59, tzinfo=timezone.utc)
            ),
            "weekly-2026-08-08",
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            module._default_weekly_round_id(datetime(2026, 8, 12, 0, 5))
        self.assertEqual(
            module._default_weekly_campaign_id(
                datetime(2026, 8, 12, 0, 5, tzinfo=timezone.utc)
            ),
            "wwpdb-2026-08-08",
        )

    def test_private_materializer_requires_explicit_no_publish_before_configuration(self) -> None:
        module = self.deployment_module()
        raw_function = module.materialize_private_weekly_evaluation.get_raw_f()
        for publish in (None, True):
            with self.subTest(publish=publish), self.assertRaisesRegex(
                RuntimeError, "explicit --no-publish"
            ), patch(
                "foldarium_pipeline.supabase.SupabaseCoordinator.from_env"
            ) as from_env:
                raw_function(
                    "weekly-2026-08-08-beta-v5-global-tm-29", publish
                )
            from_env.assert_not_called()

    def test_private_materializer_passes_only_exact_round_to_no_publish_core(self) -> None:
        module = self.deployment_module()
        calls = []
        coordinator = object()
        expected = {
            "status": "materialized-private-preclose",
            "round_id": "weekly-2026-08-08-beta-v5-global-tm-29",
            "evaluation_id": "weekly_eval_" + "a" * 32,
            "item_count": 29,
            "choice_count": 290,
            "artifact": {
                "object_uri": "supabase://private/sha256/aa/" + "a" * 64,
                "sha256": "a" * 64,
                "size_bytes": 123,
                "media_type": "application/json",
            },
        }

        def materialize(round_id, destination, *, coordinator):
            calls.append((round_id, Path(destination).is_dir(), coordinator))
            return expected

        raw_function = module.materialize_private_weekly_evaluation.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.private_evaluation.materialize_private_preclose_evaluation",
            side_effect=materialize,
        ):
            report = raw_function(expected["round_id"], False)

        self.assertEqual(calls, [(expected["round_id"], True, coordinator)])
        self.assertEqual(report["mode"], "private-no-publish")
        self.assertFalse(report["mutation_enabled"])
        self.assertEqual(report["artifact"]["sha256"], "a" * 64)

        source = inspect.getsource(raw_function)
        self.assertNotIn("reveal_weekly_quiz_round", source)
        self.assertNotIn("run_wednesday_reveal", source)
        self.assertNotIn("_wednesday_publish_enabled", source)
        self.assertNotIn("weekly_quiz_rounds", source)

    def test_retrospective_tick_catalogs_latest_round_without_reveal_dependency(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            def current_weekly_quiz_round(self, campaign_id):
                self.campaign_id = campaign_id
                return {"round_id": "weekly-2026-08-08-v2"}

        coordinator = Coordinator()
        calls = []

        def materialize(round_id, destination, *, coordinator):
            calls.append((round_id, Path(destination).is_dir(), coordinator))
            return {
                "status": "materialized-private-postclose",
                "round_id": round_id,
                "evaluation_id": "weekly_eval_" + "a" * 32,
                "item_count": 29,
                "choice_count": 290,
                "artifact": {"sha256": "b" * 64},
            }

        raw_function = module.weekly_retrospective_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.private_evaluation.materialize_postclose_weekly_evaluation",
            side_effect=materialize,
        ), patch.object(
            module,
            "_default_weekly_campaign_id",
            return_value="wwpdb-2026-08-08",
        ):
            report = raw_function()

        self.assertEqual(coordinator.campaign_id, "wwpdb-2026-08-08")
        self.assertEqual(
            calls,
            [("weekly-2026-08-08-v2", True, coordinator)],
        )
        self.assertEqual(report["mode"], "private-postclose")
        source = inspect.getsource(raw_function)
        self.assertNotIn("reveal_weekly_quiz_round", source)
        self.assertNotIn("run_wednesday_reveal", source)

    def test_archive_publication_tick_supports_exact_round_and_global_backfill(
        self,
    ) -> None:
        module = self.deployment_module()
        coordinator = object()
        calls = []

        def publish(*, coordinator, round_id=None):
            calls.append((coordinator, round_id))
            return {
                "status": "complete",
                "requested_round_id": round_id,
                "round_count": 1,
                "round_ids": [round_id or "weekly-old"],
                "results": [],
            }

        raw_function = module.weekly_retrospective_publication_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.retrospective_archive.publish_missing_retrospectives",
            side_effect=publish,
        ):
            exact = raw_function("weekly-exact")
            backfill = raw_function()

        self.assertEqual(calls, [(coordinator, "weekly-exact"), (coordinator, None)])
        self.assertEqual(exact["mode"], "post-reveal-publication")
        self.assertTrue(exact["admin_artifacts_private"])
        self.assertEqual(backfill["round_ids"], ["weekly-old"])
        source = inspect.getsource(raw_function)
        self.assertNotIn("_default_weekly_campaign_id", source)
        self.assertNotIn("current_weekly_quiz_round", source)
        self.assertNotIn("materialize_postclose_weekly_evaluation", source)

    def test_scheduled_tick_follows_current_round_across_campaign_rollover(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            def current_weekly_quiz_round(self):
                return {
                    "round_id": "weekly-2026-08-08-v2",
                    "campaign_id": "wwpdb-2026-08-01",
                }

            def weekly_quiz_reveal_inputs(self, round_id):
                self.round_id = round_id
                return {"round_id": round_id}, b"private-index"

            def download_predicted_complex(self, run_id, sample_id):
                raise AssertionError("fixture does not resolve predictions")

        coordinator = Coordinator()

        def reveal_service(round_record, private_index_content, destination, **kwargs):
            return {
                "status": "evaluated-not-revealed",
                "round_id": round_record["round_id"],
                "item_count": 0,
                "choice_count": 0,
            }

        raw_function = module.wednesday_reveal_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.wednesday_reveal.run_wednesday_reveal",
            side_effect=reveal_service,
        ), patch(
            "foldarium_pipeline.private_evaluation.recover_legacy_ligand_eligibility",
            return_value=None,
        ):
            report = raw_function(None, False)

        self.assertEqual(coordinator.round_id, "weekly-2026-08-08-v2")
        self.assertEqual(report["round_id"], "weekly-2026-08-08-v2")

    def test_open_round_is_a_normal_scheduled_noop(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            def current_weekly_quiz_round(self):
                return {"round_id": "weekly-open"}

            def weekly_quiz_reveal_inputs(self, round_id):
                return {
                    "round_id": round_id,
                    "closes_at": "2099-08-23T23:59:59Z",
                }, b"private-index"

            def download_predicted_complex(self, run_id, sample_id):
                raise AssertionError("open round must not resolve predictions")

        raw_function = module.wednesday_reveal_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=Coordinator(),
        ), patch(
            "foldarium_pipeline.wednesday_reveal.run_wednesday_reveal"
        ) as reveal_service:
            report = raw_function(None, False)

        reveal_service.assert_not_called()
        self.assertEqual(report["status"], "voting-open")
        self.assertEqual(report["round_id"], "weekly-open")
        self.assertEqual(report["closes_at"], "2099-08-23T23:59:59Z")
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["mutation_enabled"])

    def test_schedule_and_cpu_image_have_bounded_retries_and_evaluation_stack(self) -> None:
        module = self.deployment_module()
        self.assertEqual(module.WEDNESDAY_REVEAL_CRON_UTC, "5 0-5 * * 3")
        self.assertEqual(module.WEEKLY_RETROSPECTIVE_CRON_UTC, "15 0-5 * * 3")
        self.assertEqual(
            module.WEEKLY_RETROSPECTIVE_PUBLICATION_CRON_UTC,
            "45 0 * * 3",
        )
        self.assertFalse(module.WEEKLY_RETROSPECTIVE_PUBLICATION_ENABLED)
        self.assertEqual(module.WEDNESDAY_REVEAL_MODAL_RETRIES, 2)
        self.assertEqual(
            module.QUIZ_EVALUATION_PACKAGES,
            ("gemmi==0.7.5", "numpy==2.3.2", "rdkit==2025.3.6"),
        )

    def test_tick_dry_run_uses_exact_private_artifacts_without_publishing(self) -> None:
        module = self.deployment_module()
        calls = {"predictions": [], "publishes": []}

        class Coordinator:
            def weekly_quiz_reveal_inputs(self, round_id):
                self.round_id = round_id
                return {"round_id": round_id}, b"private-index"

            def download_predicted_complex(self, run_id, sample_id):
                calls["predictions"].append((run_id, sample_id))
                return {"content": b"complex", "sha256": "a" * 64}

            def reveal_weekly_quiz_round(self, **kwargs):
                calls["publishes"].append(kwargs)
                return {"status": "revealed"}

        coordinator = Coordinator()

        def reveal_service(
            round_record,
            private_index_content,
            destination,
            *,
            prediction_resolver,
            reveal_publisher,
            recovered_ligand_eligibility,
        ):
            self.assertEqual(round_record, {"round_id": "weekly-2026-08-08"})
            self.assertEqual(private_index_content, b"private-index")
            self.assertTrue(Path(destination).is_dir())
            prediction_resolver({"run_id": "run-of3", "sample_id": "sample-4"})
            self.assertIsNone(reveal_publisher)
            self.assertIsNone(recovered_ligand_eligibility)
            return {
                "status": "evaluated-not-revealed",
                "round_id": "weekly-2026-08-08",
                "item_count": 1,
                "choice_count": 2,
            }

        raw_function = module.wednesday_reveal_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.wednesday_reveal.run_wednesday_reveal",
            side_effect=reveal_service,
        ), patch(
            "foldarium_pipeline.private_evaluation.recover_legacy_ligand_eligibility",
            return_value=None,
        ):
            report = raw_function("weekly-2026-08-08", False)

        self.assertEqual(coordinator.round_id, "weekly-2026-08-08")
        self.assertEqual(calls["predictions"], [("run-of3", "sample-4")])
        self.assertEqual(calls["publishes"], [])
        self.assertEqual(report["mode"], "dry-run")
        self.assertFalse(report["mutation_enabled"])

    def test_tick_requires_explicit_gate_then_passes_atomic_publisher(self) -> None:
        module = self.deployment_module()
        published = []

        class Coordinator:
            def weekly_quiz_reveal_inputs(self, round_id):
                return {"round_id": round_id}, b"private-index"

            def download_predicted_complex(self, run_id, sample_id):
                raise AssertionError("service fixture does not resolve predictions")

            def reveal_weekly_quiz_round(self, **kwargs):
                published.append(kwargs)
                return {"status": "revealed"}

        coordinator = Coordinator()

        def reveal_service(
            round_record,
            private_index_content,
            destination,
            *,
            prediction_resolver,
            reveal_publisher,
            recovered_ligand_eligibility,
        ):
            self.assertIsNotNone(reveal_publisher)
            self.assertIsNone(recovered_ligand_eligibility)
            reveal_publisher(
                round_id=round_record["round_id"], reveal_manifest={"items": []}
            )
            return {
                "status": "revealed",
                "round_id": round_record["round_id"],
                "item_count": 0,
                "choice_count": 0,
            }

        raw_function = module.wednesday_reveal_tick.get_raw_f()
        with patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env",
            return_value=coordinator,
        ), patch(
            "foldarium_pipeline.wednesday_reveal.run_wednesday_reveal",
            side_effect=reveal_service,
        ), patch(
            "foldarium_pipeline.private_evaluation.recover_legacy_ligand_eligibility",
            return_value=None,
        ):
            report = raw_function("weekly-2026-08-08", True)

        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["round_id"], "weekly-2026-08-08")
        self.assertEqual(report["mode"], "publish")
        self.assertTrue(report["mutation_enabled"])

    def test_invalid_environment_mutation_gate_fails_before_database_access(self) -> None:
        module = self.deployment_module()
        raw_function = module.wednesday_reveal_tick.get_raw_f()
        with patch.dict(
            module.os.environ,
            {module.WEDNESDAY_REVEAL_PUBLISH_ENV: "yes"},
        ), patch(
            "foldarium_pipeline.supabase.SupabaseCoordinator.from_env"
        ) as from_env, self.assertRaisesRegex(ValueError, "must be 0 or 1"):
            raw_function("weekly-2026-08-08", None)
        from_env.assert_not_called()


if __name__ == "__main__":
    unittest.main()
