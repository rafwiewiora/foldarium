from __future__ import annotations

import importlib.util
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

    def test_schedule_and_cpu_image_have_bounded_retries_and_evaluation_stack(self) -> None:
        module = self.deployment_module()
        self.assertEqual(module.WEDNESDAY_REVEAL_CRON_UTC, "5 0-5 * * 3")
        self.assertEqual(module.WEDNESDAY_REVEAL_MODAL_RETRIES, 2)
        self.assertEqual(
            module.QUIZ_EVALUATION_PACKAGES,
            ("gemmi==0.7.3", "numpy==2.3.2", "rdkit==2025.3.6"),
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
        ):
            self.assertEqual(round_record, {"round_id": "weekly-2026-08-08"})
            self.assertEqual(private_index_content, b"private-index")
            self.assertTrue(Path(destination).is_dir())
            prediction_resolver({"run_id": "run-of3", "sample_id": "sample-4"})
            self.assertIsNone(reveal_publisher)
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
        ):
            self.assertIsNotNone(reveal_publisher)
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
