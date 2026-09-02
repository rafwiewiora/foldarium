from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock

from foldarium_pipeline.contracts import SCHEMA_VERSION, canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.supabase import IMMUTABLE_PUBLIC_CACHE_CONTROL
from foldarium_pipeline.weekly_quiz import (
    WeeklyQuizAssemblyError,
    backfill_selector_kit_for_round,
    build_staged_selector_kit,
    publish_selector_kit,
    publish_staged_selector_kit,
    regenerate_promoted_selector_kit,
)
from foldarium_pipeline.weekly_selector import parse_selector_kit


def source_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "target-1",
            "target_id": "cameo-target-1",
            "ligand": "DRG",
            "week": "2026-08-08",
            "protein_uri": "supabase://quiz-public/protein.pdb",
            "pocket_uri": "supabase://quiz-public/pocket.pdb",
            "choices": [
                {
                    "run_id": "run-of3",
                    "sample_id": "sample-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "cluster_id": "cluster-a",
                    "is_rep": True,
                    "pose_uri": "supabase://quiz-public/of3-1.pdb",
                    "protein_uri": "supabase://quiz-public/of3-protein.pdb",
                    "pocket_uri": "supabase://quiz-public/of3-pocket.pdb",
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "cluster_id": "cluster-b",
                    "is_rep": False,
                    "pose_uri": "supabase://quiz-public/boltz-1.pdb",
                    "protein_uri": "supabase://quiz-public/boltz-protein.pdb",
                    "pocket_uri": "supabase://quiz-public/boltz-pocket.pdb",
                },
            ],
        }
    ]


def selector_target(item_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "target_id": item_id,
        "entities": [
            {"type": "protein", "chain_ids": ["A"], "sequence": "ACDEFGHIK"},
            {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
        ],
    }


def asset_bytes(label: str) -> bytes:
    return (
        f"ATOM      1  C   LIG L   1       1.000   2.000   3.000  1.00  0.00           C  \n"
        f"# {label}\n"
    ).encode("utf-8")


class FakeCoordinator:
    def __init__(self, bucket: str) -> None:
        self.storage_bucket = bucket
        self.stored: dict[str, tuple[bytes, str]] = {}
        self.cache_controls: list[str | None] = []
        self.downloads: list[str] = []
        self.registered: list[dict[str, Any]] = []
        self.campaign_rows: list[dict[str, Any]] = []

    def require_public_bucket(self) -> None:
        return None

    def store_bytes(
        self,
        content: bytes,
        media_type: str,
        *,
        cache_control: str | None = None,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        self.stored[digest] = (content, media_type)
        self.cache_controls.append(cache_control)
        return {
            "object_uri": f"supabase://{self.storage_bucket}/sha256/{digest[:2]}/{digest}",
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
        }

    def download_content_object(
        self, object_uri: str, *, expected_sha256: str | None = None
    ) -> bytes:
        self.downloads.append(object_uri)
        if expected_sha256 is not None:
            content, _media_type = self.stored[expected_sha256]
            return content
        marker = object_uri.rsplit("/", 1)[-1]
        content, _media_type = self.stored[marker]
        return content

    def seed_public_asset(self, object_uri: str, content: bytes) -> None:
        marker = object_uri.rsplit("/", 1)[-1]
        self.stored[marker] = (content, "chemical/x-pdb")

    def register_weekly_selector_kit(self, **kwargs: Any) -> dict[str, Any]:
        self.registered.append(kwargs)
        return {"status": "registered", "round_id": kwargs["round_id"]}

    def campaign_prediction_run_statuses(self, _campaign_id: str) -> list[dict[str, Any]]:
        return self.campaign_rows


class SelectorKitPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_id = "weekly-2026-08-08"
        self.blind, self.private = build_blind_manifest(self.round_id, source_items())
        self.stage_root: Path | None = None

    def tearDown(self) -> None:
        if self.stage_root is not None:
            self.stage_root = None

    def _write_stage(self) -> Path:
        temporary = Path(tempfile.mkdtemp(prefix="foldarium-selector-stage-"))
        self.stage_root = temporary
        item = self.blind["items"][0]
        item_id = item["id"]
        stage_choices = []
        for index, choice in enumerate(item["choices"], start=1):
            pose_path = f"assets/{item_id}/pose-{index}.pdb"
            protein_path = f"assets/{item_id}/protein-{index}.pdb"
            pocket_path = f"assets/{item_id}/pocket-{index}.pdb"
            for relative, label in (
                (pose_path, f"{choice['id']}:pose"),
                (protein_path, f"{choice['id']}:protein"),
                (pocket_path, f"{choice['id']}:pocket"),
            ):
                path = temporary / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(asset_bytes(label))
            private_choice = self.private["items"][0]["choices"][index - 1]
            stage_choices.append(
                {
                    "run_id": private_choice["run_id"],
                    "sample_id": private_choice["sample_id"],
                    "artifact_sha256": private_choice.get("artifact_sha256", "a" * 64),
                    "pose_path": pose_path,
                    "protein_path": protein_path,
                    "pocket_path": pocket_path,
                }
            )
        stage = {
            "schema_version": 11,
            "round_id": self.round_id,
            "campaign_id": "weekly-2026-08-08",
            "items": [
                {
                    "id": item_id,
                    "target_id": item_id,
                    "selector_target": selector_target(item_id),
                    "choices": stage_choices,
                }
            ],
        }
        (temporary / "stage.json").write_text(
            json.dumps(stage, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return temporary

    def test_build_staged_selector_kit_is_deterministic_and_leak_safe(self) -> None:
        stage_dir = self._write_stage()
        first_zip, first_descriptor, _targets = build_staged_selector_kit(
            stage_dir, self.blind
        )
        second_zip, second_descriptor, _targets = build_staged_selector_kit(
            stage_dir, self.blind
        )
        self.assertEqual(first_zip, second_zip)
        self.assertEqual(first_descriptor, second_descriptor)
        kit = parse_selector_kit(first_zip)
        self.assertEqual(kit["round_id"], self.round_id)
        self.assertEqual(kit["environment"], "production")
        self.assertEqual(
            kit["blind_manifest_sha256"],
            manifest_sha256(self.blind),
        )
        serialized = json.dumps(kit)
        self.assertNotIn("run_id", serialized)
        self.assertNotIn("sample_id", serialized)

    def test_publish_selector_kit_uploads_content_addressed_zip(self) -> None:
        stage_dir = self._write_stage()
        zip_bytes, descriptor, targets = build_staged_selector_kit(stage_dir, self.blind)
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        published = publish_selector_kit(
            round_id=self.round_id,
            blind_manifest_sha256=manifest_sha256(self.blind),
            zip_bytes=zip_bytes,
            descriptor=descriptor,
            public_coordinator=public,
            private_coordinator=private,
            selector_targets=targets,
            register_catalog=True,
        )
        self.assertTrue(published["storage_path"].startswith("quiz-public/sha256/"))
        zip_digest = published["object_uri"].rsplit("/", 1)[-1]
        self.assertEqual(
            published["storage_path"].split("/")[-1],
            zip_digest,
        )
        stored_zip, media_type = public.stored[zip_digest]
        self.assertEqual(media_type, "application/zip")
        self.assertEqual(stored_zip, zip_bytes)
        self.assertEqual(public.cache_controls, [IMMUTABLE_PUBLIC_CACHE_CONTROL])
        self.assertEqual(private.cache_controls, [None])
        self.assertEqual(len(private.registered), 1)
        self.assertEqual(private.registered[0]["round_id"], self.round_id)

    def test_publish_staged_selector_kit_can_register_catalog(self) -> None:
        stage_dir = self._write_stage()
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        published = publish_staged_selector_kit(
            stage_dir,
            self.blind,
            public_coordinator=public,
            private_coordinator=private,
            register_catalog=True,
        )
        self.assertTrue(published["registered"])
        self.assertEqual(len(private.registered), 1)
        self.assertEqual(private.registered[0]["blind_manifest_sha256"], manifest_sha256(self.blind))
        self.assertIsNotNone(published["selector_targets"])

    def test_publish_staged_selector_kit_skips_registration_when_requested(self) -> None:
        stage_dir = self._write_stage()
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        published = publish_staged_selector_kit(
            stage_dir,
            self.blind,
            public_coordinator=public,
            private_coordinator=private,
            register_catalog=False,
        )
        self.assertFalse(published["registered"])
        self.assertEqual(private.registered, [])
        zip_digest = published["object_uri"].rsplit("/", 1)[-1]
        self.assertIn(zip_digest, public.stored)

    def test_regenerate_promoted_selector_kit_rebinds_round_identity(self) -> None:
        promoted_round_id = "production-beta-round"
        promoted_blind, _promoted_private = build_blind_manifest(
            promoted_round_id,
            source_items(),
        )
        stage_dir = self._write_stage()
        zip_bytes, descriptor, targets = build_staged_selector_kit(stage_dir, self.blind)
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        source_publication = publish_selector_kit(
            round_id=self.round_id,
            blind_manifest_sha256=manifest_sha256(self.blind),
            zip_bytes=zip_bytes,
            descriptor=descriptor,
            public_coordinator=public,
            private_coordinator=private,
            selector_targets=targets,
            register_catalog=False,
        )
        for choice in promoted_blind["items"][0]["choices"]:
            for uri_key in ("pose_uri", "protein_uri", "pocket_uri"):
                public.seed_public_asset(choice[uri_key], asset_bytes(choice[uri_key]))

        regenerated = regenerate_promoted_selector_kit(
            source_round={
                "campaign_id": "weekly-2026-08-08",
                "blind_manifest": self.blind,
            },
            source_metadata={"selector_targets": source_publication["selector_targets"]},
            promoted_blind_manifest=promoted_blind,
            public_coordinator=public,
            private_coordinator=private,
            register_catalog=False,
        )
        self.assertNotEqual(regenerated["kit_sha256"], source_publication["kit_sha256"])
        self.assertNotEqual(regenerated["byte_size"], 0)
        zip_digest = regenerated["object_uri"].rsplit("/", 1)[-1]
        with zipfile.ZipFile(BytesIO(public.stored[zip_digest][0])) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["round_id"], promoted_round_id)
        self.assertEqual(
            manifest["blind_manifest_sha256"],
            manifest_sha256(promoted_blind),
        )

    def test_backfill_selector_kit_for_round_downloads_public_assets(self) -> None:
        stage_dir = self._write_stage()
        zip_bytes, descriptor, targets = build_staged_selector_kit(stage_dir, self.blind)
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        publication = publish_selector_kit(
            round_id=self.round_id,
            blind_manifest_sha256=manifest_sha256(self.blind),
            zip_bytes=zip_bytes,
            descriptor=descriptor,
            public_coordinator=public,
            private_coordinator=private,
            selector_targets=targets,
            register_catalog=False,
        )
        for choice in self.blind["items"][0]["choices"]:
            for uri_key in ("pose_uri", "protein_uri", "pocket_uri"):
                public.seed_public_asset(choice[uri_key], asset_bytes(choice[uri_key]))

        round_row = {
            "round_id": self.round_id,
            "blind_manifest": self.blind,
            "metadata": {"selector_targets": publication["selector_targets"]},
        }
        transient_uri = self.blind["items"][0]["choices"][0]["pose_uri"]
        original_download = public.download_content_object
        transient_attempts = 0

        def flaky_download(object_uri, **kwargs):
            nonlocal transient_attempts
            if object_uri == transient_uri:
                transient_attempts += 1
                if transient_attempts == 1:
                    raise OSError("transient storage transport failure")
            return original_download(object_uri, **kwargs)

        public.download_content_object = flaky_download
        with mock.patch(
            "foldarium_pipeline.weekly_quiz.time.sleep"
        ) as retry_sleep:
            backfilled = backfill_selector_kit_for_round(
                round_row,
                public_coordinator=public,
                private_coordinator=private,
                register_catalog=True,
            )
        self.assertEqual(backfilled["round_id"], self.round_id)
        self.assertEqual(len(private.registered), 1)
        self.assertGreaterEqual(len(public.downloads), 3)
        self.assertEqual(transient_attempts, 2)
        retry_sleep.assert_called_once_with(0.5)

    def test_backfill_recovers_targets_for_pre_selector_round(self) -> None:
        public = FakeCoordinator("quiz-public")
        private = FakeCoordinator("private")
        blind_item = self.blind["items"][0]
        target_id = blind_item["id"]
        private.campaign_rows = [
            {
                "target_id": target_id,
                "task_payload": {"target": selector_target(target_id)},
            }
        ]
        for choice in blind_item["choices"]:
            for uri_key in ("pose_uri", "protein_uri", "pocket_uri"):
                public.seed_public_asset(choice[uri_key], asset_bytes(choice[uri_key]))

        backfilled = backfill_selector_kit_for_round(
            {
                "round_id": self.round_id,
                "campaign_id": "weekly-2026-08-08",
                "blind_manifest": self.blind,
                "metadata": {},
            },
            public_coordinator=public,
            private_coordinator=private,
            register_catalog=True,
        )

        self.assertEqual(backfilled["item_count"], 1)
        self.assertEqual(len(private.registered), 1)

    def test_publish_staged_selector_kit_rejects_missing_selector_target(self) -> None:
        stage_dir = self._write_stage()
        stage = json.loads((stage_dir / "stage.json").read_text(encoding="utf-8"))
        stage["items"][0].pop("selector_target")
        (stage_dir / "stage.json").write_text(
            json.dumps(stage, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WeeklyQuizAssemblyError, "normalized selector target"
        ):
            build_staged_selector_kit(stage_dir, self.blind)


if __name__ == "__main__":
    unittest.main()
