from __future__ import annotations

import hashlib
import importlib.util
import json
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
            scorer = kwargs["choice_scorer"]
            captured["reused"] = scorer(
                protein_path=protein_path,
                ligand_path=ligand_path,
                ligand_smiles="CCO",
                pose_id=existing_pose_id,
            )
            captured["missing"] = scorer(
                protein_path=protein_path,
                ligand_path=ligand_path,
                ligand_smiles="CCO",
                pose_id=missing_pose_id,
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
            return kwargs["choice_scorer"](
                protein_path=protein_path,
                ligand_path=ligand_path,
                ligand_smiles="CCO",
                pose_id=missing_pose_id,
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

    def test_scheduled_tick_resolves_latest_immutable_campaign_round(self) -> None:
        module = self.deployment_module()

        class Coordinator:
            def current_weekly_quiz_round(self, campaign_id):
                self.campaign_id = campaign_id
                return {"round_id": "weekly-2026-08-08-v2"}

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
        ), patch.object(
            module,
            "_default_weekly_campaign_id",
            return_value="wwpdb-2026-08-08",
        ):
            report = raw_function(None, False)

        self.assertEqual(coordinator.campaign_id, "wwpdb-2026-08-08")
        self.assertEqual(coordinator.round_id, "weekly-2026-08-08-v2")
        self.assertEqual(report["round_id"], "weekly-2026-08-08-v2")

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
