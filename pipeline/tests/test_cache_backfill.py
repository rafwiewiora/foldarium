from __future__ import annotations

import hashlib
import unittest

from foldarium_pipeline.cache_backfill import (
    backfill_immutable_cache,
    verified_public_object_inventory,
)
from foldarium_pipeline.quiz import manifest_sha256
from foldarium_pipeline.supabase import (
    IMMUTABLE_PUBLIC_CACHE_CONTROL,
    SupabasePublicationError,
)


def object_uri(bucket: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"supabase://{bucket}/sha256/{digest[:2]}/{digest}"


class FakeCoordinator:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.downloads: list[tuple[str, str]] = []
        self.replacements: list[tuple[str, bytes, str, str]] = []

    def download_content_object(self, uri: str, *, expected_sha256: str) -> bytes:
        self.downloads.append((uri, expected_sha256))
        content = self.objects[uri]
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise SupabasePublicationError("downloaded artifact does not match its object digest")
        return content

    def replace_content_object(
        self,
        uri: str,
        content: bytes,
        media_type: str,
        *,
        cache_control: str,
    ) -> None:
        self.replacements.append((uri, content, media_type, cache_control))


class CacheBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bucket = "foldarium-quiz-public"
        self.protein = b"protein"
        self.pose = b"pose"
        self.manifest = {
            "round_id": "weekly-2026-08-29",
            "items": [
                {
                    "protein_file": object_uri(self.bucket, self.protein),
                    "choices": [
                        {"pose_file": object_uri(self.bucket, self.pose)},
                        {"pose_file": object_uri(self.bucket, self.pose)},
                    ],
                }
            ],
        }

    def test_builds_unique_digest_verified_inventory(self) -> None:
        inventory = verified_public_object_inventory(
            self.manifest,
            round_id=self.manifest["round_id"],
            expected_manifest_sha256=manifest_sha256(self.manifest),
            public_bucket=self.bucket,
        )
        self.assertEqual(len(inventory), 2)
        self.assertEqual({item["media_type"] for item in inventory}, {"chemical/x-pdb"})

    def test_rejects_wrong_manifest_hash_or_bucket(self) -> None:
        with self.assertRaisesRegex(SupabasePublicationError, "SHA-256"):
            verified_public_object_inventory(
                self.manifest,
                round_id=self.manifest["round_id"],
                expected_manifest_sha256="0" * 64,
                public_bucket=self.bucket,
            )
        with self.assertRaisesRegex(SupabasePublicationError, "unexpected"):
            verified_public_object_inventory(
                self.manifest,
                round_id=self.manifest["round_id"],
                expected_manifest_sha256=manifest_sha256(self.manifest),
                public_bucket="different-public-bucket",
            )

    def test_rejects_non_pdb_manifest_contracts(self) -> None:
        manifest = {
            **self.manifest,
            "items": [{
                **self.manifest["items"][0],
                "choices": [{
                    **self.manifest["items"][0]["choices"][0],
                    "media_type": "application/octet-stream",
                }],
            }],
        }
        with self.assertRaisesRegex(SupabasePublicationError, "current PDB"):
            verified_public_object_inventory(
                manifest,
                round_id=manifest["round_id"],
                expected_manifest_sha256=manifest_sha256(manifest),
                public_bucket=self.bucket,
            )

    def test_dry_run_verifies_every_digest_without_writes(self) -> None:
        inventory = verified_public_object_inventory(
            self.manifest,
            round_id=self.manifest["round_id"],
            expected_manifest_sha256=manifest_sha256(self.manifest),
            public_bucket=self.bucket,
        )
        coordinator = FakeCoordinator({
            object_uri(self.bucket, self.protein): self.protein,
            object_uri(self.bucket, self.pose): self.pose,
        })
        summary = backfill_immutable_cache(coordinator, inventory)
        self.assertEqual(summary["verified_objects"], 2)
        self.assertEqual(summary["updated_objects"], 0)
        self.assertEqual(coordinator.replacements, [])

    def test_apply_reuploads_only_identical_verified_bytes(self) -> None:
        inventory = verified_public_object_inventory(
            self.manifest,
            round_id=self.manifest["round_id"],
            expected_manifest_sha256=manifest_sha256(self.manifest),
            public_bucket=self.bucket,
        )
        coordinator = FakeCoordinator({
            object_uri(self.bucket, self.protein): self.protein,
            object_uri(self.bucket, self.pose): self.pose,
        })
        summary = backfill_immutable_cache(coordinator, inventory, apply=True)
        self.assertEqual(summary["updated_objects"], 2)
        self.assertEqual(
            {row[3] for row in coordinator.replacements},
            {IMMUTABLE_PUBLIC_CACHE_CONTROL},
        )

    def test_digest_failure_happens_before_any_replacement(self) -> None:
        inventory = verified_public_object_inventory(
            self.manifest,
            round_id=self.manifest["round_id"],
            expected_manifest_sha256=manifest_sha256(self.manifest),
            public_bucket=self.bucket,
        )
        coordinator = FakeCoordinator({
            object_uri(self.bucket, self.protein): self.protein,
            object_uri(self.bucket, self.pose): b"tampered",
        })
        with self.assertRaisesRegex(SupabasePublicationError, "does not match"):
            backfill_immutable_cache(coordinator, inventory, apply=True)
        self.assertEqual(coordinator.replacements, [])


if __name__ == "__main__":
    unittest.main()
