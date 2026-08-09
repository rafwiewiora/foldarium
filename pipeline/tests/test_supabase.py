from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from foldarium_pipeline.supabase import (
    SupabaseConfigurationError,
    SupabaseCoordinator,
    SupabasePublicationError,
    SupabasePublisher,
)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, *, claim_body: bytes = b"true") -> None:
        self.claim_body = claim_body
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
        self.calls.append((request, timeout))
        url = request.full_url  # type: ignore[attr-defined]
        if url.endswith("/rest/v1/rpc/claim_prediction_run"):
            return FakeResponse(self.claim_body)
        if url.endswith("/rest/v1/rpc/finish_prediction_run"):
            return FakeResponse(b'{"status":"succeeded"}')
        if url.endswith("/rest/v1/rpc/register_weekly_prediction_plan"):
            return FakeResponse(b'{"status":"registered","target_count":1,"run_count":2}')
        return FakeResponse(b'{"Key":"stored"}')


def successful_result(relative_path: str, content: bytes) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "schema_version": "foldarium.prediction/v1",
        "task_id": "run_test123",
        "campaign_id": "campaign-test",
        "target_id": "target-test",
        "method": "boltz2",
        "method_version": "2.2.1",
        "container_image": "registry.example/boltz@sha256:" + "a" * 64,
        "status": "succeeded",
        "duration_seconds": 12.5,
        "provenance": {"config": {"seed": 7}},
        "samples": [
            {
                "sample_id": "seed-7-rank-0",
                "seed": 7,
                "sample_index": 0,
                "confidence": {"ranking_score": 0.91},
                "artifacts": [
                    {
                        "role": "predicted_complex",
                        "relative_path": relative_path,
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "chemical/x-mmcif",
                    }
                ],
            }
        ],
    }


class SupabasePublisherTests(unittest.TestCase):
    def test_from_env_is_explicit_and_repr_redacts_service_role(self) -> None:
        opener = RecordingOpener()
        publisher = SupabasePublisher.from_env(
            {
                "SUPABASE_URL": "https://project.supabase.co/",
                "SUPABASE_SERVICE_ROLE_KEY": "top-secret-service-role",
                "FOLDARIUM_STORAGE_BUCKET": "prediction-results",
            },
            opener=opener,
        )
        self.assertEqual(publisher.storage_bucket, "prediction-results")
        self.assertNotIn("top-secret-service-role", repr(publisher))
        with self.assertRaisesRegex(SupabaseConfigurationError, "SUPABASE_SERVICE_ROLE_KEY"):
            SupabasePublisher.from_env(
                {
                    "SUPABASE_URL": "https://project.supabase.co",
                    "FOLDARIUM_STORAGE_BUCKET": "prediction-results",
                }
            )

    def test_rejects_non_origin_url_and_unsafe_bucket(self) -> None:
        with self.assertRaises(SupabaseConfigurationError):
            SupabasePublisher("http://project.supabase.co", "key", "results")
        with self.assertRaises(SupabaseConfigurationError):
            SupabasePublisher("https://project.supabase.co/rest/v1", "key", "results")
        with self.assertRaises(SupabaseConfigurationError):
            SupabasePublisher("https://project.supabase.co", "key", "../results")

    def test_public_bucket_check_requires_matching_public_storage_metadata(self) -> None:
        class BucketOpener(RecordingOpener):
            def __init__(self, payload: bytes) -> None:
                super().__init__()
                self.payload = payload

            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                return FakeResponse(self.payload)

        public = BucketOpener(b'{"id":"quiz-assets","public":true}')
        publisher = SupabaseCoordinator(
            "https://project.supabase.co", "key", "quiz-assets", opener=public
        )
        publisher.require_public_bucket()
        self.assertTrue(
            public.calls[0][0].full_url.endswith("/storage/v1/bucket/quiz-assets")
        )
        self.assertEqual(public.calls[0][0].get_method(), "GET")

        private = BucketOpener(b'{"id":"quiz-assets","public":false}')
        publisher = SupabaseCoordinator(
            "https://project.supabase.co", "key", "quiz-assets", opener=private
        )
        with self.assertRaisesRegex(SupabasePublicationError, "must be public"):
            publisher.require_public_bucket()

    def test_claim_run_uses_the_atomic_claim_rpc(self) -> None:
        opener = RecordingOpener(
            claim_body=b'{"run_id":"run_test123","status":"running","lease_owner":"modal-worker-1"}'
        )
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        self.assertTrue(publisher.claim_run("run_test123", "modal-worker-1", 600))
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0][0]
        self.assertTrue(request.full_url.endswith("/rest/v1/rpc/claim_prediction_run"))
        self.assertEqual(
            json.loads(request.data),
            {
                "p_run_id": "run_test123",
                "p_worker_id": "modal-worker-1",
                "p_lease_seconds": 600,
            },
        )
        self.assertNotIn(b"service-role-key", request.data)

    def test_uploads_verified_digest_path_before_atomic_finish_rpc(self) -> None:
        content = b"data_foldarium\n#\n"
        digest = hashlib.sha256(content).hexdigest()
        opener = RecordingOpener()
        publisher = SupabasePublisher(
            "https://project.supabase.co",
            "service-role-key",
            "prediction-results",
            opener=opener,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            model = output / "nested" / "model.cif"
            model.parent.mkdir()
            model.write_bytes(content)
            response = publisher.publish_result(
                successful_result("nested/model.cif", content), output, "modal-worker-1"
            )

        self.assertEqual(response, {"status": "succeeded"})
        self.assertEqual(len(opener.calls), 2)
        upload = opener.calls[0][0]
        finish = opener.calls[1][0]
        self.assertEqual(upload.get_method(), "POST")
        self.assertTrue(
            upload.full_url.endswith(
                f"/storage/v1/object/prediction-results/sha256/{digest[:2]}/{digest}"
            )
        )
        self.assertEqual(upload.get_header("X-upsert"), "false")
        self.assertEqual(upload.data, content)
        self.assertTrue(finish.full_url.endswith("/rest/v1/rpc/finish_prediction_run"))
        payload = json.loads(finish.data)
        self.assertEqual(payload["p_run_id"], "run_test123")
        self.assertEqual(payload["p_worker_id"], "modal-worker-1")
        self.assertNotIn("artifacts", payload["p_result"]["samples"][0])
        artifact = payload["p_artifacts"][0]
        self.assertEqual(artifact["sha256"], digest)
        self.assertEqual(artifact["sample_id"], "seed-7-rank-0")
        self.assertEqual(artifact["relative_path"], "nested/model.cif")
        self.assertEqual(
            artifact["object_uri"],
            f"supabase://prediction-results/sha256/{digest[:2]}/{digest}",
        )
        self.assertEqual(artifact["metadata"]["source_relative_path"], "nested/model.cif")
        self.assertNotIn(b"service-role-key", finish.data)
        headers = dict(finish.header_items())
        self.assertEqual(headers["Authorization"], "Bearer service-role-key")
        self.assertEqual(headers["Apikey"], "service-role-key")

    def test_verifies_all_artifacts_before_making_requests(self) -> None:
        content = b"expected"
        opener = RecordingOpener()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "model.cif").write_bytes(b"tampered")
            with self.assertRaisesRegex(SupabasePublicationError, "SHA-256"):
                publisher.publish_result(
                    successful_result("model.cif", content), output, "worker-1"
                )
        self.assertEqual(opener.calls, [])

    def test_rejects_path_traversal_before_making_requests(self) -> None:
        content = b"data"
        opener = RecordingOpener()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (Path(temporary) / "escape.cif").write_bytes(content)
            with self.assertRaisesRegex(SupabasePublicationError, "artifact_root"):
                publisher.publish_result(
                    successful_result("../escape.cif", content), output, "worker-1"
                )
        self.assertEqual(opener.calls, [])

    def test_duplicate_upload_conflict_is_idempotent(self) -> None:
        content = b"already stored"

        class ConflictThenFinish(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if "/storage/v1/object/results/" in request.full_url:  # type: ignore[attr-defined]
                    raise HTTPError(request.full_url, 409, "duplicate", {}, None)  # type: ignore[attr-defined]
                if "/storage/v1/object/authenticated/results/" in request.full_url:  # type: ignore[attr-defined]
                    return FakeResponse(content)
                return FakeResponse(b'{"status":"succeeded"}')

        opener = ConflictThenFinish()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.cif"
            model.write_bytes(content)
            response = publisher.publish_result(
                successful_result("model.cif", content), model.parent, "worker-1"
            )
        self.assertEqual(response["status"], "succeeded")
        self.assertEqual(len(opener.calls), 3)

    def test_duplicate_upload_conflict_rejects_wrong_existing_bytes(self) -> None:
        content = b"expected content"

        class ConflictWithWrongObject(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if "/storage/v1/object/results/" in request.full_url:  # type: ignore[attr-defined]
                    raise HTTPError(request.full_url, 409, "duplicate", {}, None)  # type: ignore[attr-defined]
                if "/storage/v1/object/authenticated/results/" in request.full_url:  # type: ignore[attr-defined]
                    return FakeResponse(b"wrong content")
                return FakeResponse(b'{"status":"succeeded"}')

        opener = ConflictWithWrongObject()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.cif"
            model.write_bytes(content)
            with self.assertRaisesRegex(SupabasePublicationError, "does not match"):
                publisher.publish_result(
                    successful_result("model.cif", content), model.parent, "worker-1"
                )
        self.assertEqual(len(opener.calls), 2)

    def test_storage_http_400_duplicate_verifies_matching_object(self) -> None:
        content = b"already stored quiz asset"
        duplicate = json.dumps(
            {
                "statusCode": "409",
                "error": "Duplicate",
                "message": "The resource already exists",
            }
        ).encode()

        class DuplicateThenVerify(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if "/storage/v1/object/results/" in request.full_url:  # type: ignore[attr-defined]
                    raise HTTPError(  # type: ignore[attr-defined]
                        request.full_url, 400, "bad request", {}, io.BytesIO(duplicate)
                    )
                if "/storage/v1/object/authenticated/results/" in request.full_url:  # type: ignore[attr-defined]
                    return FakeResponse(content)
                raise AssertionError(request.full_url)  # type: ignore[attr-defined]

        opener = DuplicateThenVerify()
        publisher = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        stored = publisher.store_bytes(content, "chemical/x-pdb")
        self.assertEqual(stored["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(opener.calls[1][0].get_method(), "GET")

    def test_storage_http_400_duplicate_rejects_mismatching_object(self) -> None:
        content = b"expected quiz asset"
        duplicate = json.dumps(
            {
                "statusCode": 409,
                "error": "Duplicate",
                "message": "The resource already exists",
            }
        ).encode()

        class DuplicateWithWrongObject(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if "/storage/v1/object/results/" in request.full_url:  # type: ignore[attr-defined]
                    raise HTTPError(  # type: ignore[attr-defined]
                        request.full_url, 400, "bad request", {}, io.BytesIO(duplicate)
                    )
                if "/storage/v1/object/authenticated/results/" in request.full_url:  # type: ignore[attr-defined]
                    return FakeResponse(b"different bytes")
                raise AssertionError(request.full_url)  # type: ignore[attr-defined]

        opener = DuplicateWithWrongObject()
        publisher = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "does not match"):
            publisher.store_bytes(content, "chemical/x-pdb")
        self.assertEqual(len(opener.calls), 2)

    def test_storage_http_400_non_duplicate_remains_a_failure(self) -> None:
        content = b"quiz asset"
        non_duplicate = json.dumps(
            {
                "statusCode": "400",
                "error": "Bad Request",
                "message": "The object name is invalid",
            }
        ).encode()

        class BadRequest(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                raise HTTPError(  # type: ignore[attr-defined]
                    request.full_url,
                    400,
                    "bad request",
                    {},
                    io.BytesIO(non_duplicate),
                )

        opener = BadRequest()
        publisher = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "failed with HTTP 400"):
            publisher.store_bytes(content, "chemical/x-pdb")
        self.assertEqual(len(opener.calls), 1)

    def test_failed_result_finishes_without_artifact_io(self) -> None:
        opener = RecordingOpener()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        failed = {
            key: value
            for key, value in successful_result("unused.cif", b"unused").items()
            if key != "samples"
        }
        failed.update({"status": "failed", "error": "prediction command failed"})
        response = publisher.publish_result(failed, "/path/need/not/exist", "worker-1")
        self.assertEqual(response, {"status": "succeeded"})
        self.assertEqual(len(opener.calls), 1)
        payload = json.loads(opener.calls[0][0].data)
        self.assertEqual(payload["p_result"]["status"], "failed")
        self.assertEqual(payload["p_artifacts"], [])

    def test_refuses_to_serialize_its_credential_in_result_metadata(self) -> None:
        content = b"data"
        opener = RecordingOpener()
        publisher = SupabasePublisher(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        result = successful_result("model.cif", content)
        result["provenance"]["accidental"] = "service-role-key"
        with tempfile.TemporaryDirectory() as temporary:
            model = Path(temporary) / "model.cif"
            model.write_bytes(content)
            with self.assertRaisesRegex(SupabasePublicationError, "credential"):
                publisher.publish_result(result, model.parent, "worker-1")
        self.assertEqual(len(opener.calls), 0)


class SupabaseCoordinatorTests(unittest.TestCase):
    @staticmethod
    def weekly_plan() -> dict:
        from foldarium_pipeline.intake import build_weekly_plan, parse_wwpdb_snapshot

        sequence = (
            b"PDB_ID\tSequence_Count\tSequence\n"
            b"36IQ\t1\tMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        )
        ligand = (
            b"PDB_ID\tComponent_ID\tInChI\tSMILES string\n"
            b"36IQ\tDRG\tInChI=fixture\tCCCCCCCCCCCCCCCC\n"
        )
        payload = {
            "target": {
                "id": "2026-06-20_00000082",
                "week_id": "2026-06-20",
                "labels_submission_3d": "hard",
            },
            "entities": [
                {
                    "id": "polymer",
                    "entity_type": "protein",
                    "canonical_sequence": "M" + "A" * 39,
                },
                {
                    "id": "ligand",
                    "entity_type": "non_polymer",
                    "component_id": "DRG",
                    "smiles": "CCCCCCCCCCCCCCCC",
                    "inchi": "InChI=fixture",
                },
            ],
            "biounits": [],
            "predictions": [],
        }
        return build_weekly_plan(
            release_date=date(2026, 6, 20),
            ww_pdb_snapshot=parse_wwpdb_snapshot(sequence, ligand),
            cameo_payloads=[payload],
            output_prefix="supabase://results/runs",
            generated_at=datetime(2026, 6, 20, 6, tzinfo=timezone.utc),
        )

    def test_uploads_sources_and_target_before_atomic_registration(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        response = coordinator.register_weekly_plan(
            self.weekly_plan(),
            {
                "wwpdb_sequence": (b"sequence source", "text/tab-separated-values"),
                "wwpdb_nonpolymer": (b"ligand source", "text/tab-separated-values"),
            },
            adapter_version="foldarium-pipeline/0.2",
        )
        self.assertEqual(response["status"], "registered")
        self.assertEqual(len(opener.calls), 4)  # two sources, one target package, one RPC
        register = opener.calls[-1][0]
        self.assertTrue(register.full_url.endswith("/rpc/register_weekly_prediction_plan"))
        payload = json.loads(register.data)
        self.assertEqual(len(payload["p_targets"]), 1)
        self.assertEqual(len(payload["p_runs"]), 2)
        target = payload["p_targets"][0]
        self.assertEqual(target["package_sha256"], payload["p_runs"][0]["input_sha256"])
        self.assertEqual(target["package_uri"], payload["p_runs"][0]["input_uri"])
        self.assertNotIn(b"service-role-key", register.data)
        decisions = payload["p_snapshot"]["metadata"]["selection_decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["decision"], "selected")
        self.assertEqual(decisions[0]["target_id"], target["target_id"])

    def test_authorizes_exact_transient_boltz_msa_retry_and_verifies_patch(self) -> None:
        task = next(
            task for task in self.weekly_plan()["tasks"] if task["method"] == "boltz2"
        )
        run_id = task["task_id"]

        for error_code in ("output_validation_failed", "msa_preprocessing_failed"):
            with self.subTest(error_code=error_code):
                row = {
                    "run_id": run_id,
                    "target_id": task["target"]["target_id"],
                    "method": "boltz2",
                    "status": "failed",
                    "attempt_count": 1,
                    "max_attempts": 1,
                    "error_code": error_code,
                    "task_payload": task,
                }

                class RetryOpener(RecordingOpener):
                    def __init__(self) -> None:
                        super().__init__()
                        self.authorized = False

                    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                        self.calls.append((request, timeout))
                        url = request.full_url  # type: ignore[attr-defined]
                        if request.get_method() == "GET":  # type: ignore[attr-defined]
                            current = {**row, "max_attempts": 2 if self.authorized else 1}
                            return FakeResponse(json.dumps([current]).encode())
                        if request.get_method() == "PATCH":  # type: ignore[attr-defined]
                            query = parse_qs(urlsplit(url).query)
                            if query["run_id"] != [f"in.({run_id})"]:
                                raise AssertionError(query)
                            if query["method"] != ["eq.boltz2"]:
                                raise AssertionError(query)
                            if query["status"] != ["eq.failed"]:
                                raise AssertionError(query)
                            if query["attempt_count"] != ["eq.1"]:
                                raise AssertionError(query)
                            if query["max_attempts"] != ["eq.1"]:
                                raise AssertionError(query)
                            if sorted(
                                query["error_code"][0].removeprefix("in.(").removesuffix(")").split(",")
                            ) != ["msa_preprocessing_failed", "output_validation_failed"]:
                                raise AssertionError(query)
                            if json.loads(request.data) != {"max_attempts": 2}:  # type: ignore[attr-defined]
                                raise AssertionError(request.data)  # type: ignore[attr-defined]
                            self.authorized = True
                            return FakeResponse(json.dumps([{**row, "max_attempts": 2}]).encode())
                        raise AssertionError(url)

                opener = RetryOpener()
                coordinator = SupabaseCoordinator(
                    "https://project.supabase.co", "service-role-key", "results", opener=opener
                )
                report = coordinator.authorize_transient_boltz_msa_retries(
                    [run_id],
                    confirmed_oom_run_ids=["run_known_oom"],
                )
                self.assertEqual(report["status"], "authorized")
                self.assertEqual(report["requested_run_ids"], [run_id])
                self.assertEqual(report["authorized_run_ids"], [run_id])
                self.assertEqual(report["already_authorized_run_ids"], [])
                self.assertEqual(report["confirmed_oom_run_ids"], ["run_known_oom"])
                self.assertEqual(report["task_payloads"], {run_id: task})
                self.assertEqual(
                    report["authorization_rows"],
                    [
                        {
                            "run_id": run_id,
                            "target_id": task["target"]["target_id"],
                            "error_code": error_code,
                            "attempt_count": 1,
                            "previous_max_attempts": 1,
                            "max_attempts": 2,
                            "action": "authorized",
                        }
                    ],
                )
                self.assertEqual(
                    [request.get_method() for request, _ in opener.calls],
                    ["GET", "PATCH", "GET"],
                )

    def test_transient_boltz_msa_retry_authorization_is_idempotent(self) -> None:
        task = next(
            task for task in self.weekly_plan()["tasks"] if task["method"] == "boltz2"
        )
        row = {
            "run_id": task["task_id"],
            "target_id": task["target"]["target_id"],
            "method": "boltz2",
            "status": "failed",
            "attempt_count": 1,
            "max_attempts": 2,
            "error_code": "msa_preprocessing_failed",
            "task_payload": task,
        }

        class AlreadyAuthorizedOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if request.get_method() != "GET":  # type: ignore[attr-defined]
                    raise AssertionError("idempotent authorization must not write")
                return FakeResponse(json.dumps([row]).encode())

        opener = AlreadyAuthorizedOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        report = coordinator.authorize_transient_boltz_msa_retries(
            [task["task_id"]], confirmed_oom_run_ids=[]
        )
        self.assertEqual(report["status"], "already-authorized")
        self.assertEqual(report["authorized_run_ids"], [])
        self.assertEqual(report["already_authorized_run_ids"], [task["task_id"]])
        self.assertEqual(report["approved_submission_run_ids"], [])
        self.assertEqual(report["task_payloads"], {})
        self.assertEqual(len(opener.calls), 2)

        recovery = coordinator.authorize_transient_boltz_msa_retries(
            [task["task_id"]],
            confirmed_oom_run_ids=[],
            resubmit_already_authorized=True,
        )
        self.assertEqual(recovery["status"], "resubmission-authorized")
        self.assertEqual(
            recovery["resubmission_authorized_run_ids"], [task["task_id"]]
        )
        self.assertEqual(
            recovery["approved_submission_run_ids"], [task["task_id"]]
        )
        self.assertEqual(recovery["task_payloads"], {task["task_id"]: task})
        self.assertEqual(
            recovery["authorization_rows"][0]["action"],
            "approved-for-resubmission",
        )
        self.assertTrue(all(
            request.get_method() == "GET" for request, _ in opener.calls
        ))

    def test_transient_boltz_msa_retry_refuses_confirmed_oom_before_query(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "confirmed OOM"):
            coordinator.authorize_transient_boltz_msa_retries(
                ["run_9ce43151df233cc5fbae4f6e"],
                confirmed_oom_run_ids=["run_9ce43151df233cc5fbae4f6e"],
            )
        self.assertEqual(opener.calls, [])

    def test_transient_boltz_msa_retry_rejects_non_transient_row_without_patch(self) -> None:
        task = next(
            task for task in self.weekly_plan()["tasks"] if task["method"] == "boltz2"
        )
        row = {
            "run_id": task["task_id"],
            "target_id": task["target"]["target_id"],
            "method": "boltz2",
            "status": "failed",
            "attempt_count": 1,
            "max_attempts": 1,
            "error_code": "gpu_out_of_memory",
            "task_payload": task,
        }

        class OomOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                if request.get_method() != "GET":  # type: ignore[attr-defined]
                    raise AssertionError("OOM preflight must not write")
                return FakeResponse(json.dumps([row]).encode())

        opener = OomOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "transient MSA error"):
            coordinator.authorize_transient_boltz_msa_retries(
                [task["task_id"]], confirmed_oom_run_ids=["run_known_oom"]
            )
        self.assertEqual(len(opener.calls), 1)

    def test_appends_only_absent_runs_to_the_stored_weekly_campaign(self) -> None:
        plan = self.weekly_plan()
        campaign = {
            **plan["campaign"],
            "status": "predicting",
            "metadata": {"weekly_plan_sha256": "b" * 64},
        }
        snapshot = {
            "snapshot_id": "snapshot_existing",
            "campaign_id": campaign["campaign_id"],
            "release_date": campaign["release_date"],
            "plan_sha256": "b" * 64,
            "files": {"wwpdb_sequence": {"sha256": "c" * 64}},
            "metadata": {},
        }
        existing = plan["tasks"][0]["task_id"]

        class ExpansionOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                url = request.full_url  # type: ignore[attr-defined]
                if request.get_method() == "GET":  # type: ignore[attr-defined]
                    if "/rest/v1/campaigns?" in url:
                        return FakeResponse(json.dumps([campaign]).encode())
                    if "/rest/v1/prerelease_snapshots?" in url:
                        return FakeResponse(json.dumps([snapshot]).encode())
                    if "/rest/v1/prediction_runs?" in url:
                        return FakeResponse(json.dumps([{"run_id": existing}]).encode())
                if url.endswith("/rest/v1/rpc/register_weekly_prediction_plan"):
                    return FakeResponse(b'{"status":"registered","target_count":1,"run_count":1}')
                if url.endswith("/rest/v1/rpc/record_curation_decisions"):
                    return FakeResponse(b'{"status":"recorded"}')
                return FakeResponse(b'{"Key":"stored"}')

        opener = ExpansionOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        response = coordinator.append_weekly_plan(
            plan,
            adapter_version="foldarium-pipeline/0.3",
        )
        self.assertEqual(response["status"], "registered")
        self.assertEqual(response["run_count"], 1)
        self.assertEqual(response["registered_run_ids"], [plan["tasks"][1]["task_id"]])
        register = next(
            request for request, _ in opener.calls
            if request.full_url.endswith("/rest/v1/rpc/register_weekly_prediction_plan")
        )
        payload = json.loads(register.data)
        self.assertEqual(payload["p_campaign"], campaign)
        self.assertEqual(payload["p_snapshot"], snapshot)
        self.assertEqual(len(payload["p_targets"]), 1)
        self.assertEqual([row["run_id"] for row in payload["p_runs"]], [plan["tasks"][1]["task_id"]])
        decisions = next(
            request for request, _ in opener.calls
            if request.full_url.endswith("/rest/v1/rpc/record_curation_decisions")
        )
        self.assertEqual(
            json.loads(decisions.data)["p_decisions"][0]["stage"],
            "prospective-expansion",
        )

    def test_records_private_curation_decisions_through_one_rpc(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        response = coordinator.record_curation_decisions(
            [
                {
                    "decision_id": "curation_test123",
                    "source": "cameo-public-catchup",
                    "stage": "foldseek-novelty",
                    "target_id": "12IY",
                    "release_week": "2026-07-11",
                    "decision": "familiar",
                    "reason": "training-ligand-overlap-at-least-0.25",
                    "input_sha256": "a" * 64,
                    "metrics": {"train_shape_overlap": 0.548},
                    "provenance": {"scorer_version": "test/v1"},
                }
            ]
        )
        self.assertEqual(response, {"Key": "stored"})
        request = opener.calls[-1][0]
        self.assertTrue(request.full_url.endswith("/rpc/record_curation_decisions"))
        payload = json.loads(request.data)
        self.assertEqual(payload["p_decisions"][0]["decision"], "familiar")

    def test_weekly_campaign_preflight_stops_after_first_matching_row(self) -> None:
        class CampaignPreflightOpener(RecordingOpener):
            def __init__(self, body: bytes) -> None:
                super().__init__()
                self.body = body

            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                self.assert_get(request)
                return FakeResponse(self.body)

            @staticmethod
            def assert_get(request: object) -> None:
                if request.get_method() != "GET":  # type: ignore[attr-defined]
                    raise AssertionError("preflight must be read-only")

        for body, expected in ((b"[]", False), (b'[{"campaign_id":"wwpdb-2026-08-08"}]', True)):
            with self.subTest(expected=expected):
                opener = CampaignPreflightOpener(body)
                coordinator = SupabaseCoordinator(
                    "https://project.supabase.co", "service-role-key", "results", opener=opener
                )
                self.assertEqual(
                    coordinator.weekly_campaign_exists("wwpdb-2026-08-08"), expected
                )
                self.assertEqual(len(opener.calls), 1)
                self.assertIn("campaign_id=eq.wwpdb-2026-08-08", opener.calls[0][0].full_url)

    def test_tampered_plan_is_rejected_before_upload(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        plan = self.weekly_plan()
        plan["campaign"]["name"] = "tampered"
        with self.assertRaisesRegex(SupabasePublicationError, "does not match"):
            coordinator.register_weekly_plan(
                plan,
                {"source": (b"data", "application/octet-stream")},
                adapter_version="adapter/v1",
            )
        self.assertEqual(opener.calls, [])

    def test_registers_external_cameo_models_with_private_provenance(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        response = coordinator.register_external_prediction_set(
            target_id="2026-06-20_00000082",
            import_manifest={
                "provider": "cameo",
                "method": "alphafold3",
                "provider_server_id": "993",
                "provider_target_id": "2026-06-20_00000082",
                "source_page": "https://cameo3d.org/target/2026-06-20_00000082",
                "license": "CC-BY-SA-4.0",
            },
            source_page=b"<html>public target</html>",
            artifacts=[
                {
                    "role": "prediction",
                    "model_index": 1,
                    "content": b"data_model\n",
                    "source_uri": "https://cameo3d.org/api/coords/t/993/1/model-1.cif",
                },
                {
                    "role": "reference",
                    "assembly_id": 1,
                    "content": b"reference\n",
                    "media_type": "application/gzip",
                    "source_uri": "https://cameo3d.org/api/coords/t/biounit/01/reference.cif.gz",
                },
            ],
        )
        self.assertEqual(response, {"Key": "stored"})
        self.assertEqual(len(opener.calls), 4)  # page, model, reference, RPC
        rpc = opener.calls[-1][0]
        self.assertTrue(rpc.full_url.endswith("/rpc/register_external_prediction_set"))
        payload = json.loads(rpc.data)
        self.assertEqual(len(payload["p_artifacts"]), 3)
        self.assertEqual(payload["p_set"]["license"], "CC-BY-SA-4.0")
        self.assertNotIn("content", str(payload))

    def test_invalid_external_artifact_makes_no_requests(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaises(SupabasePublicationError):
            coordinator.register_external_prediction_set(
                target_id="target",
                import_manifest={
                    "provider": "cameo",
                    "method": "alphafold3",
                    "provider_target_id": "target",
                    "source_page": "https://cameo3d.org/target/target",
                    "license": "CC-BY-SA-4.0",
                },
                source_page=b"page",
                artifacts=[
                    {
                        "role": "prediction",
                        "model_index": 9,
                        "content": b"model",
                        "source_uri": "https://cameo3d.org/model",
                    }
                ],
            )
        self.assertEqual(opener.calls, [])

    def test_reads_succeeded_campaign_outputs_and_verifies_private_bytes(self) -> None:
        content = b"data_blind_fixture\n#\n"
        digest = hashlib.sha256(content).hexdigest()
        run = {
            "run_id": "run_test123",
            "target_id": "target-test",
            "method": "boltz2",
            "method_version": "2.2.1",
            "status": "succeeded",
            "task_payload": {"target": {"target_id": "target-test", "entities": []}},
            "result": {"samples": [{"sample_id": "seed-7-rank-0", "sample_index": 0}]},
        }
        artifact = {
            "run_id": "run_test123",
            "sample_id": "seed-7-rank-0",
            "role": "predicted_complex",
            "object_uri": f"supabase://results/sha256/{digest[:2]}/{digest}",
            "sha256": digest,
            "media_type": "chemical/x-mmcif",
        }

        class CampaignOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                url = request.full_url  # type: ignore[attr-defined]
                if "/targets?" in url:
                    return FakeResponse(b'[{"target_id":"target-test"}]')
                if "/prediction_runs?" in url:
                    return FakeResponse(json.dumps([run]).encode())
                if "/prediction_artifacts?" in url:
                    return FakeResponse(json.dumps([artifact]).encode())
                if "/storage/v1/object/authenticated/results/" in url:
                    return FakeResponse(content)
                raise AssertionError(url)

        opener = CampaignOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        rows = coordinator.campaign_prediction_outputs("campaign-test")
        self.assertEqual(rows[0]["samples"][0]["predicted_complex"], artifact)
        self.assertEqual(
            coordinator.download_content_object(artifact["object_uri"], expected_sha256=digest),
            content,
        )
        self.assertTrue(all(call[0].get_method() == "GET" for call in opener.calls))

    def test_private_object_download_rejects_wrong_bucket_without_request(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "this bucket"):
            coordinator.download_content_object(
                "supabase://public/sha256/aa/" + "a" * 64
            )
        self.assertEqual(opener.calls, [])

    def test_resolves_exact_private_weekly_round_and_digest_bound_index(self) -> None:
        private_content = b'{"round_id":"weekly-2026-08-08"}'
        digest = hashlib.sha256(private_content).hexdigest()
        round_row = {
            "round_id": "weekly-2026-08-08",
            "campaign_id": "wwpdb-2026-08-08",
            "status": "open",
            "opens_at": "2026-08-08T03:00:00Z",
            "closes_at": "2026-08-12T00:00:00Z",
            "blind_manifest": {"schema_version": 1, "items": []},
            "blind_manifest_sha256": "a" * 64,
            "reveal_manifest": None,
            "reveal_manifest_sha256": None,
            "metadata": {
                "private_index": {
                    "object_uri": f"supabase://results/sha256/{digest[:2]}/{digest}",
                    "sha256": digest,
                    "size_bytes": len(private_content),
                    "media_type": "application/json",
                }
            },
        }

        class RoundOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                url = request.full_url  # type: ignore[attr-defined]
                if "/weekly_quiz_rounds?" in url:
                    query = parse_qs(urlsplit(url).query)
                    self.test_case.assertEqual(
                        query["round_id"], ["eq.weekly-2026-08-08"]
                    )
                    self.test_case.assertEqual(query["limit"], ["2"])
                    self.test_case.assertIn("metadata", query["select"][0].split(","))
                    return FakeResponse(json.dumps([round_row]).encode())
                if "/storage/v1/object/authenticated/results/" in url:
                    return FakeResponse(private_content)
                raise AssertionError(url)

        opener = RoundOpener()
        opener.test_case = self
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        returned_round, returned_content = coordinator.weekly_quiz_reveal_inputs(
            "weekly-2026-08-08"
        )
        self.assertEqual(returned_round, round_row)
        self.assertEqual(returned_content, private_content)
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(all(call[0].get_method() == "GET" for call in opener.calls))

    def test_weekly_round_lookup_fails_closed_on_duplicate_identity(self) -> None:
        row = {"round_id": "weekly-2026-08-08", "metadata": {}}

        class DuplicateRoundOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                return FakeResponse(json.dumps([row, row]).encode())

        opener = DuplicateRoundOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "returned 2 rows"):
            coordinator.weekly_quiz_round("weekly-2026-08-08")
        self.assertEqual(len(opener.calls), 1)

    def test_current_weekly_round_is_bound_to_expected_campaign(self) -> None:
        current = {
            "round_id": "weekly-2026-08-08-v2",
            "campaign_id": "wwpdb-2026-08-08",
            "public_status": "open",
        }

        class CurrentRoundOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                self.test_case.assertIn(
                    "/rest/v1/rpc/get_current_weekly_quiz_round",
                    request.full_url,  # type: ignore[attr-defined]
                )
                self.test_case.assertIn(  # type: ignore[attr-defined]
                    json.loads(request.data).get("p_environment"),
                    {"production", "preview", "development"},
                )
                return FakeResponse(json.dumps([current]).encode())

        opener = CurrentRoundOpener()
        opener.test_case = self
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        self.assertEqual(
            coordinator.current_weekly_quiz_round("wwpdb-2026-08-08"), current
        )
        self.assertEqual(
            json.loads(opener.calls[0][0].data),  # type: ignore[attr-defined]
            {"p_environment": "production"},
        )
        with self.assertRaisesRegex(SupabasePublicationError, "expected campaign"):
            coordinator.current_weekly_quiz_round("wwpdb-2026-08-15")

        coordinator.current_weekly_quiz_round(
            "wwpdb-2026-08-08", environment="preview"
        )
        self.assertEqual(
            json.loads(opener.calls[-1][0].data),  # type: ignore[attr-defined]
            {"p_environment": "preview"},
        )
        with self.assertRaisesRegex(SupabasePublicationError, "environment must be"):
            coordinator.current_weekly_quiz_round(
                "wwpdb-2026-08-08", environment="staging"
            )

    def test_opens_weekly_round_in_explicit_environment(self) -> None:
        opener = RecordingOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        manifest = {
            "schema_version": 1,
            "round_id": "preview-weekly-2026-08-08-v3",
            "items": [{"id": "item-1", "choices": [{"id": "choice-1"}]}],
        }
        coordinator.open_weekly_quiz_round(
            round_id="preview-weekly-2026-08-08-v3",
            campaign_id="wwpdb-2026-08-08",
            opens_at="2026-08-08T03:00:00Z",
            closes_at="2026-08-12T00:00:00Z",
            blind_manifest=manifest,
            environment="preview",
        )
        request = opener.calls[0][0]
        self.assertTrue(  # type: ignore[attr-defined]
            request.full_url.endswith("/rest/v1/rpc/open_weekly_quiz_round")
        )
        payload = json.loads(request.data)  # type: ignore[attr-defined]
        self.assertEqual(payload["p_environment"], "preview")
        self.assertEqual(payload["p_round_id"], "preview-weekly-2026-08-08-v3")
        self.assertEqual(len(payload["p_blind_manifest_sha256"]), 64)

    def test_downloads_only_exact_run_sample_predicted_complex_with_digest(self) -> None:
        content = b"data_original_complex\n#\n"
        digest = hashlib.sha256(content).hexdigest()
        artifact = {
            "run_id": "run-of3",
            "sample_id": "sample-4",
            "role": "predicted_complex",
            "object_uri": f"supabase://results/sha256/{digest[:2]}/{digest}",
            "sha256": digest,
            "media_type": "chemical/x-mmcif",
        }

        class ArtifactOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                url = request.full_url  # type: ignore[attr-defined]
                if "/prediction_artifacts?" in url:
                    query = parse_qs(urlsplit(url).query)
                    self.test_case.assertEqual(query["run_id"], ["eq.run-of3"])
                    self.test_case.assertEqual(query["sample_id"], ["eq.sample-4"])
                    self.test_case.assertEqual(query["role"], ["eq.predicted_complex"])
                    self.test_case.assertEqual(query["limit"], ["2"])
                    return FakeResponse(json.dumps([artifact]).encode())
                if "/storage/v1/object/authenticated/results/" in url:
                    return FakeResponse(content)
                raise AssertionError(url)

        opener = ArtifactOpener()
        opener.test_case = self
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        resolved = coordinator.download_predicted_complex("run-of3", "sample-4")
        self.assertEqual(resolved["content"], content)
        self.assertEqual(resolved["sha256"], digest)
        self.assertEqual(resolved["object_uri"], artifact["object_uri"])

    def test_predicted_complex_lookup_never_picks_from_duplicate_rows(self) -> None:
        digest = "a" * 64
        artifact = {
            "run_id": "run-of3",
            "sample_id": "sample-4",
            "role": "predicted_complex",
            "object_uri": f"supabase://results/sha256/aa/{digest}",
            "sha256": digest,
            "media_type": "chemical/x-mmcif",
        }

        class DuplicateArtifactOpener(RecordingOpener):
            def __call__(self, request: object, *, timeout: float) -> FakeResponse:
                self.calls.append((request, timeout))
                return FakeResponse(json.dumps([artifact, artifact]).encode())

        opener = DuplicateArtifactOpener()
        coordinator = SupabaseCoordinator(
            "https://project.supabase.co", "service-role-key", "results", opener=opener
        )
        with self.assertRaisesRegex(SupabasePublicationError, "exactly one row"):
            coordinator.download_predicted_complex("run-of3", "sample-4")
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
