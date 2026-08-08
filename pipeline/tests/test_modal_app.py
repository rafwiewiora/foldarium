from __future__ import annotations

import importlib.util
import unittest
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


if __name__ == "__main__":
    unittest.main()
