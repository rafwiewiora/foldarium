from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from foldarium_pipeline.supabase import (
    SupabaseConfigurationError,
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


if __name__ == "__main__":
    unittest.main()
