from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.wednesday_reveal import (
    CORRECT_RMSD_ANGSTROM,
    WednesdayRevealError,
    WednesdayRevealNotReady,
    fetch_rcsb_released_reference,
    rcsb_reference_url,
    run_wednesday_reveal,
)


def source_items() -> list[dict]:
    return [
        {
            "id": "9XYZ",
            "target_id": "9XYZ",
            "ligand": {"component_id": "DRG", "heavy_atoms": 17},
            "week": "2026-08-08",
            "protein_uri": "supabase://quiz/protein.pdb",
            "choices": [
                {
                    "run_id": "run-of3",
                    "sample_id": "sample-of3-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "pose_uri": "supabase://quiz/pose-a.pdb",
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-boltz-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "pose_uri": "supabase://quiz/pose-b.pdb",
                },
            ],
        }
    ]


def round_fixture() -> tuple[dict, dict, bytes]:
    blind, private = build_blind_manifest("weekly-2026-08-08", source_items())
    content = canonical_json(private).encode("utf-8")
    round_record = {
        "round_id": "weekly-2026-08-08",
        "status": "open",
        "closes_at": "2026-08-12T00:00:00Z",
        "blind_manifest": blind,
        "blind_manifest_sha256": manifest_sha256(blind),
        "metadata": {
            "private_index": {
                "object_uri": "supabase://private/index.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": "application/json",
            }
        },
    }
    return round_record, private, content


def coordinate(content: bytes, uri: str) -> dict:
    return {
        "content": content,
        "object_uri": uri,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "chemical/x-mmcif",
    }


class FakeResponse:
    def __init__(self, content: bytes, url: str):
        self.content = content
        self.url = url
        self.headers = {"Content-Length": str(len(content))}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


class RcsbReferenceTests(unittest.TestCase):
    def test_builds_only_canonical_four_character_pdb_urls(self) -> None:
        self.assertEqual(
            rcsb_reference_url("9xyz"),
            "https://files.rcsb.org/download/9XYZ.cif.gz",
        )
        for invalid in ("../../etc/passwd", "AF-Q9", "ABCD", "9XYZ.cif"):
            with self.subTest(invalid=invalid), self.assertRaises(WednesdayRevealError):
                rcsb_reference_url(invalid)

    def test_fetch_records_compressed_content_provenance(self) -> None:
        url = rcsb_reference_url("9XYZ")
        body = gzip.compress(b"data_9XYZ\n#\n", mtime=0)
        calls = []
        response = FakeResponse(body, url)

        def opener(request, timeout):
            calls.append((request.full_url, request.headers, timeout))
            return response

        artifact = fetch_rcsb_released_reference(
            {"target_id": "9XYZ"}, opener=opener, timeout_seconds=7
        )
        self.assertEqual(calls[0][0], url)
        self.assertEqual(calls[0][2], 7)
        self.assertEqual(artifact["content"], body)
        self.assertEqual(artifact["sha256"], hashlib.sha256(body).hexdigest())
        self.assertTrue(response.closed)

    def test_unreleased_404_is_retryable(self) -> None:
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not released", {}, None)

        with self.assertRaises(WednesdayRevealNotReady):
            fetch_rcsb_released_reference({"target_id": "9XYZ"}, opener=opener)

    def test_redirect_off_canonical_url_is_rejected(self) -> None:
        body = gzip.compress(b"data_9XYZ\n", mtime=0)
        response = FakeResponse(body, "https://example.invalid/reference.cif.gz")
        with self.assertRaisesRegex(WednesdayRevealNotReady, "redirected"):
            fetch_rcsb_released_reference(
                {"target_id": "9XYZ"}, opener=lambda request, timeout: response
            )


class WednesdayRevealServiceTests(unittest.TestCase):
    def test_scores_original_complexes_and_keeps_method_private_until_reveal(self) -> None:
        round_record, private, private_content = round_fixture()
        choice_ids = {
            choice["run_id"]: choice["id"] for choice in private["items"][0]["choices"]
        }
        prediction_calls = []
        evaluator_calls = []
        publisher_calls = []

        def prediction_resolver(choice):
            prediction_calls.append(dict(choice))
            body = f"raw complex for {choice['run_id']}".encode()
            return coordinate(body, f"supabase://private/{choice['run_id']}.cif")

        reference_body = gzip.compress(b"released reference", mtime=0)

        def reference_resolver(item):
            self.assertEqual(item["target_id"], "9XYZ")
            return {
                "content": reference_body,
                "source_uri": rcsb_reference_url("9XYZ"),
                "sha256": hashlib.sha256(reference_body).hexdigest(),
                "media_type": "application/gzip",
            }

        def evaluator(reference_path, prediction_path, *, component_id, heavy_atoms):
            evaluator_calls.append(
                (Path(reference_path), Path(prediction_path), component_id, heavy_atoms)
            )
            self.assertEqual(Path(reference_path).read_bytes(), b"released reference")
            body = Path(prediction_path).read_bytes()
            return {
                "evaluator_version": "test-evaluator/v1",
                "rmsd": 1.49 if b"run-of3" in body else CORRECT_RMSD_ANGSTROM,
                "receptor_rmsd": 0.75,
                "sequence_similarity": 0.95,
                "reference_receptor_chain": "A",
            }

        def publisher(**kwargs):
            publisher_calls.append(kwargs)
            return {"status": "revealed"}

        with tempfile.TemporaryDirectory() as temporary:
            result = run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction_resolver,
                reference_resolver=reference_resolver,
                evaluator=evaluator,
                reveal_publisher=publisher,
                now=datetime(2026, 8, 12, 0, 1, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "revealed")
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["choice_count"], 2)
        self.assertEqual(len(prediction_calls), 2)
        self.assertEqual(len(evaluator_calls), 2)
        self.assertEqual(len(publisher_calls), 1)
        self.assertEqual(publisher_calls[0]["round_id"], round_record["round_id"])
        reveal_choices = {
            choice["run_id"]: choice
            for choice in result["reveal_manifest"]["items"][0]["choices"]
        }
        self.assertTrue(reveal_choices["run-of3"]["correct"])
        self.assertFalse(reveal_choices["run-boltz"]["correct"])
        self.assertEqual(reveal_choices["run-of3"]["method"], "openfold3")
        self.assertEqual(reveal_choices["run-boltz"]["method"], "boltz2")
        self.assertEqual(reveal_choices["run-of3"]["id"], choice_ids["run-of3"])
        self.assertEqual(
            reveal_choices["run-of3"]["reference_sha256"],
            hashlib.sha256(reference_body).hexdigest(),
        )

    def test_without_publisher_performs_no_external_mutation(self) -> None:
        round_record, _private, private_content = round_fixture()

        def prediction(choice):
            return coordinate(b"raw complex", f"supabase://private/{choice['id']}.cif")

        def reference(item):
            return coordinate(b"reference", "https://files.rcsb.org/download/9XYZ.cif")

        with tempfile.TemporaryDirectory() as temporary:
            result = run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=reference,
                evaluator=lambda *args, **kwargs: {"rmsd": 2.5},
                now=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "evaluated-not-revealed")
        self.assertIsNone(result["publish_response"])

    def test_before_close_does_not_resolve_or_score_anything(self) -> None:
        round_record, _private, private_content = round_fixture()
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(True)
            raise AssertionError("must not be called")

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            WednesdayRevealNotReady
        ):
            run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=forbidden,
                reference_resolver=forbidden,
                evaluator=forbidden,
                now=datetime(2026, 8, 11, 23, 59, tzinfo=timezone.utc),
            )
        self.assertEqual(calls, [])

    def test_private_index_digest_tampering_fails_before_resolvers(self) -> None:
        round_record, _private, private_content = round_fixture()
        calls = []
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "private-index content"
        ):
            run_wednesday_reveal(
                round_record,
                private_content + b" ",
                temporary,
                prediction_resolver=lambda choice: calls.append(choice),
                reference_resolver=lambda item: calls.append(item),
                now=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(calls, [])

    def test_private_run_identity_cannot_be_rebound_to_a_blind_choice(self) -> None:
        round_record, private, _private_content = round_fixture()
        private["items"][0]["choices"][0]["run_id"] = "different-run"
        tampered = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            tampered
        ).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "private choice identity"
        ):
            run_wednesday_reveal(
                round_record,
                tampered,
                temporary,
                prediction_resolver=lambda choice: {},
                now=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            )

    def test_incomplete_evaluation_never_calls_publisher(self) -> None:
        round_record, _private, private_content = round_fixture()
        publisher_calls = []

        def prediction(choice):
            return coordinate(b"raw complex", f"supabase://private/{choice['id']}.cif")

        def evaluator(*args, **kwargs):
            raise RuntimeError("no compatible ligand")

        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "evaluation failed"
        ):
            run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=lambda item: coordinate(
                    b"reference", "https://files.rcsb.org/download/9XYZ.cif"
                ),
                evaluator=evaluator,
                reveal_publisher=lambda **kwargs: publisher_calls.append(kwargs),
                now=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            )
        self.assertEqual(publisher_calls, [])

    def test_prediction_artifact_requires_database_digest(self) -> None:
        round_record, _private, private_content = round_fixture()

        def prediction(choice):
            return {
                "content": b"raw complex",
                "object_uri": f"supabase://private/{choice['id']}.cif",
                "media_type": "chemical/x-mmcif",
            }

        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "no recorded SHA-256"
        ):
            run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=lambda item: coordinate(
                    b"reference", "https://files.rcsb.org/download/9XYZ.cif"
                ),
                now=datetime(2026, 8, 12, 1, tzinfo=timezone.utc),
            )

    def test_existing_reveal_is_idempotent_without_rescoring(self) -> None:
        round_record, private, private_content = round_fixture()
        reveal_choices = [
            {"id": choice["id"], "rmsd": 0.5, "correct": True}
            for choice in private["items"][0]["choices"]
        ]
        from foldarium_pipeline.quiz import build_reveal_manifest

        existing = build_reveal_manifest(
            round_record["blind_manifest"], [{"id": "9XYZ", "choices": reveal_choices}]
        )
        round_record["status"] = "revealed"
        round_record["reveal_manifest"] = existing
        round_record["reveal_manifest_sha256"] = manifest_sha256(existing)
        calls = []
        with tempfile.TemporaryDirectory() as temporary:
            result = run_wednesday_reveal(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: calls.append(choice),
                reference_resolver=lambda item: calls.append(item),
            )
        self.assertEqual(result["status"], "already-revealed")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
