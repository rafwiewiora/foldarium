from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from foldarium_pipeline.supabase import SupabaseCoordinator
from foldarium_pipeline.training_similarity import SCORER_VERSION
from foldarium_pipeline.weekly_training_audit import AUDIT_FORMAT
from foldarium_pipeline.weekly_training_overlays import (
    OVERLAY_MANIFEST_FORMAT,
    publish_overlays,
)
from foldarium_pipeline.weekly_training_report import (
    REPORT_FORMAT,
    WeeklyTrainingReportError,
    write_artifacts,
)


class FakePublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []
        self.public_bucket_checks = 0

    def require_public_bucket(self) -> None:
        self.public_bucket_checks += 1

    def store_bytes(self, content: bytes, media_type: str) -> dict:
        self.calls.append((content, media_type))
        digest = hashlib.sha256(content).hexdigest()
        return {
            "object_uri": f"supabase://weekly/sha256/{digest[:2]}/{digest}",
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
        }


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class WeeklyTrainingOverlayTests(unittest.TestCase):
    def test_cli_coordinator_supports_public_verification_and_storage(self) -> None:
        coordinator = SupabaseCoordinator.from_env(
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "test-key",
                "FOLDARIUM_STORAGE_BUCKET": "weekly-assets",
            }
        )
        self.assertIsInstance(coordinator, SupabaseCoordinator)
        self.assertTrue(callable(coordinator.require_public_bucket))
        self.assertTrue(callable(coordinator.store_bytes))

    def _fixtures(self, root: Path) -> tuple[Path, Path, bytes]:
        content = b"ATOM      1  CA  ALA A   1       0.000   0.000   0.000\nEND\n"
        digest = hashlib.sha256(content).hexdigest()
        overlay = (
            root
            / "cache"
            / "training-overlays"
            / "sha256"
            / digest[:2]
            / f"{digest}.pdb"
        )
        overlay.parent.mkdir(parents=True)
        overlay.write_bytes(content)
        exact = root / "exact.json"
        _write(
            exact,
            {
                "format_version": AUDIT_FORMAT,
                "mode": "exact",
                "scorer_version": SCORER_VERSION,
                "records": [
                    {
                        "status": "complete",
                        "round_id": "weekly-test",
                        "blind_week": "2026-01-01",
                        "item_id": "1ABC",
                        "ligand_component_id": "DRG",
                        "classification": "familiar",
                        "reason": "training-ligand-overlap-at-least-0.25",
                        "train_pdb": "2DEF",
                        "train_het": "DRG",
                        "train_identity": 0.5,
                        "train_align_rmsd": 0.8,
                        "train_shape_overlap": 0.75,
                        "has_correct_pose": True,
                        "correct_choice_ids": ["pose-a"],
                        "automated_correct": {},
                        "scorer_version": SCORER_VERSION,
                        "training_system_overlay_status": "available",
                        "training_system_overlay_unavailable_reason": None,
                        "training_system_overlay_cache": {
                            "sha256": digest,
                            "size_bytes": len(content),
                            "media_type": "chemical/x-pdb",
                        },
                    }
                ],
            },
        )
        blind = root / "blind.json"
        _write(
            blind,
            {
                "format_version": AUDIT_FORMAT,
                "mode": "blind",
                "scorer_version": SCORER_VERSION,
                "records": [
                    {
                        "status": "complete",
                        "item_id": "1ABC",
                        "nearest_training_system": {
                            "classification": "familiar",
                            "score": 0.7,
                            "choice_id": "pose-a",
                            "predict_none": False,
                        },
                        "pocket_aware": {
                            "classification": "familiar",
                            "score": 0.75,
                            "choice_id": "pose-a",
                            "predict_none": False,
                        },
                    }
                ],
            },
        )
        return exact, blind, content

    def test_publisher_resumes_and_report_merges_bound_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact, blind, content = self._fixtures(root)
            manifest_path = root / "overlays.json"
            publisher = FakePublisher()
            manifest = publish_overlays(
                exact_audit_path=exact,
                cache_directory=root / "cache",
                manifest_path=manifest_path,
                publisher=publisher,
            )
            self.assertEqual(manifest["format_version"], OVERLAY_MANIFEST_FORMAT)
            self.assertTrue(manifest["complete"])
            self.assertEqual(publisher.calls, [(content, "chemical/x-pdb")])
            self.assertEqual(publisher.public_bucket_checks, 1)

            second = FakePublisher()
            publish_overlays(
                exact_audit_path=exact,
                cache_directory=root / "cache",
                manifest_path=manifest_path,
                publisher=second,
            )
            self.assertEqual(second.calls, [])
            self.assertEqual(second.public_bucket_checks, 0)

            report = write_artifacts(
                exact,
                blind,
                root / "report.json",
                root / "report.csv",
                root / "report.md",
                overlay_manifest_path=manifest_path,
            )
            self.assertEqual(report["format_version"], REPORT_FORMAT)
            descriptor = report["records"][0]["training_system_overlay"]
            self.assertEqual(descriptor["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(
                report["training_system_overlays"]["record_count"], 1
            )

    def test_report_rejects_manifest_not_bound_to_exact_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact, blind, _content = self._fixtures(root)
            manifest_path = root / "overlays.json"
            publish_overlays(
                exact_audit_path=exact,
                cache_directory=root / "cache",
                manifest_path=manifest_path,
                publisher=FakePublisher(),
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["records"][0]["train_shape_overlap"] = 0.74
            _write(manifest_path, manifest)
            with self.assertRaisesRegex(
                WeeklyTrainingReportError, "overlay manifest"
            ):
                write_artifacts(
                    exact,
                    blind,
                    root / "report.json",
                    root / "report.csv",
                    root / "report.md",
                    overlay_manifest_path=manifest_path,
                )

    def test_unavailable_winner_is_explicit_but_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact, blind, _content = self._fixtures(root)
            audit = json.loads(exact.read_text())
            record = audit["records"][0]
            record["training_system_overlay_status"] = "unavailable"
            record["training_system_overlay_unavailable_reason"] = (
                "TrainingSimilarityError: PDB chain limit exceeded"
            )
            record["training_system_overlay_cache"] = None
            _write(exact, audit)
            manifest_path = root / "overlays.json"
            publisher = FakePublisher()
            manifest = publish_overlays(
                exact_audit_path=exact,
                cache_directory=root / "cache",
                manifest_path=manifest_path,
                publisher=publisher,
            )
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["expected_record_count"], 0)
            self.assertEqual(publisher.calls, [])
            self.assertEqual(publisher.public_bucket_checks, 0)

            report = write_artifacts(
                exact,
                blind,
                root / "report.json",
                root / "report.csv",
                root / "report.md",
                overlay_manifest_path=manifest_path,
            )
            compact = report["records"][0]
            self.assertEqual(compact["classification"], "familiar")
            self.assertEqual(
                compact["training_system_overlay_status"], "unavailable"
            )
            self.assertIn(
                "PDB chain limit exceeded",
                compact["training_system_overlay_unavailable_reason"],
            )
            self.assertIsNone(compact["training_system_overlay"])


if __name__ == "__main__":
    unittest.main()
