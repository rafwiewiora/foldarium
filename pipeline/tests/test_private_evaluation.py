from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from foldarium_pipeline.clustering import choice_order_digest
from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.private_evaluation import (
    ALLOWED_PRECLOSE_EVALUATION_ROUND_IDS,
    PRIVATE_EVALUATION_FORMAT_VERSION,
    PRODUCTION_BETA_CATCHUP_ROUND_ID,
    PrivateEvaluationError,
    _recovered_ligand_eligibility_for_legacy_items,
    build_private_evaluation_artifact,
    describe_private_evaluation_artifact,
    materialize_delayed_preclose_weekly_evaluation,
    materialize_postclose_weekly_evaluation,
    materialize_private_preclose_evaluation,
    recover_legacy_ligand_eligibility,
)
from foldarium_pipeline.sizing import count_smiles_heavy_atoms
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.supabase import PRIVATE_WEEKLY_EVALUATION_FIELDS, SupabasePublicationError
from foldarium_pipeline.wednesday_reveal import (
    ACCEPTANCE_POLICY_VERSION,
    CORRECT_RMSD_ANGSTROM,
    REVEAL_POLICY_VERSION,
    rcsb_reference_url,
)
from foldarium_pipeline.weekly_quiz import (
    WeeklyQuizAssemblyError,
    _weekly_ligand_eligibility,
    legacy_ligand_topology_audit,
    ligand_eligibility_from_target,
)


def fixture_ligand_eligibility(
    *,
    component_id: str = "DRG",
    heavy_atoms: int = 17,
    smiles: str = "CCCCCCCCCCCCCCCCC",
) -> dict:
    return _weekly_ligand_eligibility(component_id, heavy_atoms, smiles)


def overlay_evaluation_score() -> dict:
    return {
        "evaluator_version": "test-evaluator/v1",
        "rmsd": 0.8,
        "reference_receptor_chain": "A",
        "reference_ligand_chain": "B",
        "reference_ligand_residue": "DRG",
        "predicted_ligand_atoms": [{"name": "C1", "element": "C"}],
        "predicted_ligand_coordinates": [[1.0, 2.0, 3.0]],
        "reference_ligand_atoms": [{"name": "C1", "element": "C"}],
        "reference_ligand_coordinates": [[1.1, 2.1, 3.1]],
        "reference_pocket_pdb": (
            "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00  0.00           C\n"
            "END\n"
        ),
    }


def minimal_reference_mmcif_gz() -> bytes:
    try:
        import gemmi
    except (ImportError, ModuleNotFoundError) as exc:
        raise unittest.SkipTest("Gemmi is an optional evaluation dependency") from exc
    structure = gemmi.Structure()
    structure.add_model(gemmi.Model(0))
    model = structure[0]
    chain = gemmi.Chain("A")
    near_residue = gemmi.Residue()
    near_residue.name = "ALA"
    near_residue.seqid.num = 1
    near_atom = gemmi.Atom()
    near_atom.name = "CA"
    near_atom.element = gemmi.Element("C")
    near_atom.pos = gemmi.Position(1.0, 2.0, 3.0)
    near_residue.add_atom(near_atom)
    chain.add_residue(near_residue)
    far_residue = gemmi.Residue()
    far_residue.name = "ALA"
    far_residue.seqid.num = 2
    far_atom = gemmi.Atom()
    far_atom.name = "CA"
    far_atom.element = gemmi.Element("C")
    far_atom.pos = gemmi.Position(50.0, 50.0, 50.0)
    far_residue.add_atom(far_atom)
    chain.add_residue(far_residue)
    model.add_chain(chain)
    ligand_chain = gemmi.Chain("B")
    ligand_residue = gemmi.Residue()
    ligand_residue.name = "DRG"
    ligand_residue.seqid.num = 1
    ligand_atom = gemmi.Atom()
    ligand_atom.name = "C1"
    ligand_atom.element = gemmi.Element("C")
    ligand_atom.pos = gemmi.Position(1.1, 2.1, 3.1)
    ligand_residue.add_atom(ligand_atom)
    ligand_chain.add_residue(ligand_residue)
    model.add_chain(ligand_chain)
    with tempfile.NamedTemporaryFile(suffix=".cif") as temporary:
        structure.make_mmcif_document().write_file(temporary.name)
        return gzip.compress(Path(temporary.name).read_bytes(), mtime=0)


def target_package_for_item(item: dict, *, smiles: str = "CCCCCCCCCCCCCCCCC") -> dict:
    ligand = item["ligand"]
    return {
        "target_id": item["target_id"],
        "entities": [
            {"type": "protein", "sequence": "MKT", "chain_ids": ["A"]},
            {"type": "ligand", "smiles": smiles, "chain_ids": ["B"]},
        ],
        "metadata": {
            "selected_ligand": {
                "component_id": ligand["component_id"],
                "heavy_atoms": ligand["heavy_atoms"],
            }
        },
    }


def legacy_clustering_for_item(
    item: dict,
    *,
    identity_round_id: str,
    smiles: str = "CCCCCCCCCCCCCCCCC",
) -> dict:
    try:
        audit = legacy_ligand_topology_audit(smiles)
    except WeeklyQuizAssemblyError as exc:
        raise unittest.SkipTest(
            "Gemmi, NumPy, and RDKit are optional evaluation dependencies"
        ) from exc
    audit["choices"] = []
    for choice in item["choices"]:
        artifact_sha256 = choice.get("artifact_sha256")
        if not isinstance(artifact_sha256, str) or not artifact_sha256:
            artifact_sha256 = hashlib.sha256(
                f"{choice['run_id']}:{choice['sample_id']}".encode()
            ).hexdigest()
            choice["artifact_sha256"] = artifact_sha256
        audit["choices"].append(
            {
                "choice_digest": choice_order_digest(
                    identity_round_id,
                    item["id"],
                    {
                        "run_id": choice["run_id"],
                        "sample_id": choice["sample_id"],
                        "artifact_sha256": artifact_sha256,
                    },
                ),
                "method": choice["method"],
                "method_version": choice["method_version"],
                "mapping_mode": "source-heavy-atom-index-order",
            }
        )
    return {"ligand_atom_mapping": audit}


def legacy_round_fixture() -> tuple[dict, dict, bytes]:
    round_record, private, _private_content = round_fixture()
    item = private["items"][0]
    item.pop("ligand_eligibility", None)
    item["clustering"] = legacy_clustering_for_item(
        item, identity_round_id=round_record["round_id"]
    )
    private_content = canonical_json(private).encode("utf-8")
    private_sha256 = hashlib.sha256(private_content).hexdigest()
    round_record["metadata"]["private_index"] = {
        "object_uri": (
            "supabase://prediction-results/sha256/"
            f"{private_sha256[:2]}/{private_sha256}"
        ),
        "sha256": private_sha256,
        "size_bytes": len(private_content),
        "media_type": "application/json",
    }
    return round_record, private, private_content


def source_items() -> list[dict]:
    return [
        {
            "id": "9XYZ",
            "target_id": "9XYZ",
            "ligand": {"component_id": "DRG", "heavy_atoms": 17},
            "ligand_eligibility": fixture_ligand_eligibility(),
            "week": "2026-08-08",
            "protein_uri": "supabase://quiz/protein.pdb",
            "choices": [
                {
                    "run_id": "run-of3",
                    "sample_id": "sample-of3-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "pose_uri": "supabase://quiz/pose-a.pdb",
                    "cluster_id": "cluster-a",
                    "is_rep": True,
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-boltz-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "pose_uri": "supabase://quiz/pose-b.pdb",
                    "cluster_id": "cluster-a",
                    "is_rep": False,
                },
            ],
        }
    ]


def round_fixture() -> tuple[dict, dict, bytes]:
    blind, private = build_blind_manifest(
        PRODUCTION_BETA_CATCHUP_ROUND_ID, source_items()
    )
    private_content = canonical_json(private).encode("utf-8")
    private_sha256 = hashlib.sha256(private_content).hexdigest()
    round_record = {
        "round_id": PRODUCTION_BETA_CATCHUP_ROUND_ID,
        "campaign_id": "wwpdb-2026-08-08",
        "environment": "production",
        "status": "open",
        "opens_at": "2026-08-14T20:05:00Z",
        "closes_at": "2026-08-17T20:00:00Z",
        "blind_manifest": blind,
        "blind_manifest_sha256": manifest_sha256(blind),
        "reveal_manifest": None,
        "reveal_manifest_sha256": None,
        "revealed_at": None,
        "metadata": {
            "private_index": {
                "object_uri": (
                    "supabase://prediction-results/sha256/"
                    f"{private_sha256[:2]}/{private_sha256}"
                ),
                "sha256": private_sha256,
                "size_bytes": len(private_content),
                "media_type": "application/json",
            }
        },
    }
    return round_record, private, private_content


def coordinate(content: bytes, uri: str) -> dict:
    return {
        "content": content,
        "object_uri": uri,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "chemical/x-mmcif",
    }


class FakeCoordinator:
    storage_bucket = "prediction-results"

    def __init__(
        self,
        round_record: dict,
        private_content: bytes,
        *,
        target_packages: dict[str, dict] | None = None,
    ) -> None:
        self.round_record = round_record
        self.private_content = private_content
        self.target_packages = target_packages or {}
        self.calls: list[object] = []
        self.stored_contents: list[bytes] = []
        self.catalog: dict[str, dict] = {}

    def require_private_bucket(self) -> None:
        self.calls.append("require_private_bucket")

    def weekly_quiz_reveal_inputs(self, round_id: str):
        self.calls.append(("weekly_quiz_reveal_inputs", round_id))
        return deepcopy(self.round_record), self.private_content

    def weekly_quiz_round(self, round_id: str):
        self.calls.append(("weekly_quiz_round", round_id))
        return deepcopy(self.round_record)

    def fetch_campaign_target_packages(self, campaign_id: str, target_ids):
        self.calls.append(
            ("fetch_campaign_target_packages", campaign_id, sorted(target_ids))
        )
        packages: dict[str, dict] = {}
        for target_id in target_ids:
            normalized = target_id.strip().upper()
            package = self.target_packages.get(normalized)
            if package is None:
                raise SupabasePublicationError(
                    f"missing test target package for {normalized}"
                )
            content = canonical_json(package).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            packages[normalized] = {
                "target_id": normalized,
                "campaign_id": campaign_id,
                "package_uri": (
                    f"supabase://{self.storage_bucket}/sha256/{digest[:2]}/{digest}"
                ),
                "package_sha256": digest,
                "package": deepcopy(package),
            }
        return packages

    def download_predicted_complex(self, run_id: str, sample_id: str):
        self.calls.append(("download_predicted_complex", run_id, sample_id))
        return coordinate(
            f"complex {run_id}/{sample_id}".encode(),
            f"supabase://prediction-results/{run_id}/{sample_id}.cif",
        )

    def store_bytes(self, content: bytes, media_type: str):
        self.calls.append(("store_bytes", media_type))
        self.stored_contents.append(content)
        digest = hashlib.sha256(content).hexdigest()
        return {
            "object_uri": (
                f"supabase://{self.storage_bucket}/sha256/{digest[:2]}/{digest}"
            ),
            "sha256": digest,
            "size_bytes": len(content),
            "media_type": media_type,
        }

    def download_content_object(self, object_uri: str, *, expected_sha256: str):
        self.calls.append(("download_content_object", object_uri, expected_sha256))
        for content in self.stored_contents:
            if hashlib.sha256(content).hexdigest() == expected_sha256:
                return content
        raise AssertionError("unknown stored content")

    def record_prepared_weekly_evaluation(
        self, round_id: str, report: dict, *, prepared_at=None
    ):
        self.calls.append(("record_prepared_weekly_evaluation", round_id))
        self.round_record["metadata"]["retrospective_release"][
            "prepared_evaluation"
        ] = {
            **{
                field: report[field]
                for field in (
                    "evaluation_id",
                    "blind_manifest_sha256",
                    "private_index_sha256",
                    "reveal_manifest_sha256",
                    "item_count",
                    "choice_count",
                )
            },
            "artifact": deepcopy(report["artifact"]),
            "prepared_at": (
                prepared_at.isoformat()
                if prepared_at is not None
                else "2026-08-15T02:00:00+00:00"
            ),
        }
        return deepcopy(self.round_record)

    def register_private_weekly_evaluation(self, descriptor):
        self.calls.append(("register_private_weekly_evaluation", descriptor["evaluation_id"]))
        existing = self.catalog.setdefault(
            descriptor["evaluation_id"],
            {**deepcopy(descriptor), "created_at": "2026-08-15T02:00:00Z"},
        )
        return deepcopy(existing)

    def private_weekly_evaluation(self, round_id):
        self.calls.append(("private_weekly_evaluation", round_id))
        matches = [
            row for row in self.catalog.values() if row.get("round_id") == round_id
        ]
        return deepcopy(matches[0]) if matches else None


class PrivateEvaluationTests(unittest.TestCase):
    def test_exact_round_materializes_deterministic_private_artifact_idempotently(self) -> None:
        round_record, private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(item):
            self.assertEqual(item["target_id"], "9XYZ")
            return coordinate(reference_content, rcsb_reference_url(item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        outcomes = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                outcomes.append(
                    materialize_private_preclose_evaluation(
                        PRODUCTION_BETA_CATCHUP_ROUND_ID,
                        temporary,
                        coordinator=coordinator,
                        reference_resolver=reference,
                        evaluator=evaluator,
                        now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
                    )
                )

        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0]["status"], "materialized-private-preclose")
        self.assertFalse(outcomes[0]["register_catalog"])
        self.assertFalse(outcomes[0]["catalog_registered"])
        self.assertIsNone(outcomes[0]["catalog_created_at"])
        self.assertNotIn("reveal_manifest", outcomes[0])
        self.assertEqual(outcomes[0]["item_count"], 1)
        self.assertEqual(outcomes[0]["choice_count"], 2)
        self.assertEqual(coordinator.stored_contents[0], coordinator.stored_contents[1])
        integrity = outcomes[0]["integrity_descriptor"]
        self.assertEqual(set(integrity), set(PRIVATE_WEEKLY_EVALUATION_FIELDS))
        self.assertEqual(
            integrity["artifact_object_uri"],
            outcomes[0]["artifact"]["object_uri"],
        )
        self.assertEqual(
            integrity["artifact_sha256"],
            hashlib.sha256(coordinator.stored_contents[0]).hexdigest(),
        )
        artifact = json.loads(coordinator.stored_contents[0])
        self.assertEqual(artifact["format_version"], PRIVATE_EVALUATION_FORMAT_VERSION)
        self.assertEqual(
            artifact["blind_manifest_canonical_json"],
            canonical_json(artifact["blind_manifest"]),
        )
        self.assertEqual(
            artifact["reveal_manifest_canonical_json"],
            canonical_json(artifact["reveal_manifest"]),
        )
        self.assertEqual(
            artifact["round"]["round_id"], PRODUCTION_BETA_CATCHUP_ROUND_ID
        )
        self.assertEqual(
            artifact["round"]["private_index"]["sha256"],
            hashlib.sha256(private_content).hexdigest(),
        )
        self.assertEqual(len(artifact["reveal_manifest"]["items"]), 1)
        self.assertEqual(
            len(artifact["reveal_manifest"]["items"][0]["choices"]),
            len(private["items"][0]["choices"]),
        )
        self.assertNotIn("evaluated_at", artifact)
        self.assertTrue(
            outcomes[0]["artifact"]["object_uri"].startswith(
                "supabase://prediction-results/sha256/"
            )
        )
        self.assertFalse(any("reveal_weekly_quiz_round" in str(call) for call in coordinator.calls))
        self.assertFalse(
            any(
                call[0] == "register_private_weekly_evaluation"
                for call in coordinator.calls
                if isinstance(call, tuple)
            )
        )

    def test_explicit_catalog_registration_is_opt_in(self) -> None:
        round_record, private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(item):
            return coordinate(reference_content, rcsb_reference_url(item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary:
            outcome = materialize_private_preclose_evaluation(
                PRODUCTION_BETA_CATCHUP_ROUND_ID,
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=evaluator,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
                register_catalog=True,
            )

        self.assertTrue(outcome["register_catalog"])
        self.assertTrue(outcome["catalog_registered"])
        self.assertEqual(outcome["catalog_created_at"], "2026-08-15T02:00:00Z")
        self.assertEqual(
            [call for call in coordinator.calls if call == "require_private_bucket"],
            ["require_private_bucket"],
        )
        self.assertEqual(
            len(
                [
                    call
                    for call in coordinator.calls
                    if isinstance(call, tuple)
                    and call[0] == "register_private_weekly_evaluation"
                ]
            ),
            1,
        )
        self.assertEqual(
            outcome["integrity_descriptor"]["evaluation_id"],
            outcome["evaluation_id"],
        )

    def test_non_allowlisted_round_fails_before_storage_or_database_access(self) -> None:
        round_record, _private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            PrivateEvaluationError, "not explicitly allow-listed"
        ):
            materialize_private_preclose_evaluation(
                "weekly-2026-08-15",
                temporary,
                coordinator=coordinator,
            )
        self.assertEqual(coordinator.calls, [])

    def test_delayed_preclose_materializer_requires_round_policy(self) -> None:
        class Coordinator:
            @staticmethod
            def weekly_quiz_reveal_inputs(round_id):
                return ({"round_id": round_id, "metadata": {}}, b"private")

        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            PrivateEvaluationError,
            "not opted into next-weekly",
        ):
            materialize_delayed_preclose_weekly_evaluation(
                "weekly-2026-08-29-beta-v2",
                temporary,
                coordinator=Coordinator(),
            )

    def test_delayed_preclose_materializer_records_and_reuses_private_artifact(
        self,
    ) -> None:
        round_record, _private, private_content = round_fixture()
        round_record["metadata"]["retrospective_release"] = {
            "policy": "next-weekly-activation",
            "original_closes_at": "2026-08-16T00:00:00Z",
            "safety_closes_at": round_record["closes_at"],
            "configured_at": "2026-08-15T00:00:00Z",
        }
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(item):
            return coordinate(
                reference_content,
                rcsb_reference_url(item["target_id"]),
            )

        outcomes = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                outcomes.append(
                    materialize_delayed_preclose_weekly_evaluation(
                        round_record["round_id"],
                        temporary,
                        coordinator=coordinator,
                        reference_resolver=reference,
                        evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                        now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
                    )
                )

        self.assertEqual(
            outcomes[0]["status"], "materialized-private-delayed-preclose"
        )
        self.assertEqual(
            outcomes[1]["status"],
            "already-materialized-private-delayed-preclose",
        )
        self.assertEqual(len(coordinator.stored_contents), 1)
        self.assertIn(
            ("record_prepared_weekly_evaluation", round_record["round_id"]),
            coordinator.calls,
        )

    def test_postclose_materializer_catalogs_once_and_then_short_circuits(self) -> None:
        round_record, _private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(item):
            return coordinate(reference_content, rcsb_reference_url(item["target_id"]))

        with tempfile.TemporaryDirectory() as temporary:
            first = materialize_postclose_weekly_evaluation(
                round_record["round_id"],
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                now=datetime(2026, 8, 18, 2, tzinfo=timezone.utc),
            )
            second = materialize_postclose_weekly_evaluation(
                round_record["round_id"],
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                now=datetime(2026, 8, 18, 3, tzinfo=timezone.utc),
            )

        self.assertEqual(first["status"], "materialized-private-postclose")
        self.assertTrue(first["catalog_registered"])
        self.assertEqual(
            second["status"], "already-materialized-private-postclose"
        )
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertEqual(len(coordinator.stored_contents), 1)

    def test_postclose_materializer_rejects_an_active_voting_window(self) -> None:
        round_record, _private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            PrivateEvaluationError, "has not closed"
        ):
            materialize_postclose_weekly_evaluation(
                round_record["round_id"],
                temporary,
                coordinator=coordinator,
                now=datetime(2026, 8, 16, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(coordinator.stored_contents, [])
        self.assertEqual(coordinator.catalog, {})

    def test_artifact_builder_rejects_result_or_reference_rebinding(self) -> None:
        round_record, _private, _private_content = round_fixture()
        blind_choices = round_record["blind_manifest"]["items"][0]["choices"]
        base = {
            "status": "evaluated-private-preclose",
            "round_id": PRODUCTION_BETA_CATCHUP_ROUND_ID,
            "item_count": 1,
            "choice_count": len(blind_choices),
            "references": [
                {
                    "item_id": "9XYZ",
                    "target_id": "9XYZ",
                    "source_uri": rcsb_reference_url("9XYZ"),
                    "sha256": "b" * 64,
                }
            ],
            "reveal_manifest": {
                "schema_version": 1,
                "round_id": PRODUCTION_BETA_CATCHUP_ROUND_ID,
                "blind_manifest_sha256": round_record["blind_manifest_sha256"],
                "items": [
                    {
                        "id": "9XYZ",
                        "choices": [
                            {
                                "id": blind_choices[0]["id"],
                                "run_id": "run-x",
                                "sample_id": "sample-x",
                                "reference_uri": rcsb_reference_url("9XYZ"),
                                "reference_sha256": "c" * 64,
                                "prediction_sha256": "d" * 64,
                                "evaluator_version": "test/v1",
                            },
                            {
                                "id": blind_choices[1]["id"],
                                "run_id": "run-y",
                                "sample_id": "sample-y",
                                "reference_uri": rcsb_reference_url("9XYZ"),
                                "reference_sha256": "b" * 64,
                                "prediction_sha256": "e" * 64,
                                "evaluator_version": "test/v1",
                            },
                        ],
                    }
                ],
            },
        }
        base["reveal_manifest_sha256"] = manifest_sha256(base["reveal_manifest"])
        with self.assertRaisesRegex(PrivateEvaluationError, "released reference"):
            build_private_evaluation_artifact(round_record, base)


class DescribePrivateEvaluationArtifactTests(unittest.TestCase):
    @staticmethod
    def materialized_artifact() -> tuple[bytes, dict[str, object], str]:
        round_record, _private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(reference_item):
            return coordinate(reference_content, rcsb_reference_url(reference_item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary:
            outcome = materialize_private_preclose_evaluation(
                PRODUCTION_BETA_CATCHUP_ROUND_ID,
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=evaluator,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )
        artifact_bytes = coordinator.stored_contents[0]
        descriptor = deepcopy(outcome["integrity_descriptor"])
        descriptor["artifact_object_uri"] = outcome["artifact"]["object_uri"]
        return artifact_bytes, descriptor, coordinator.storage_bucket

    def test_round_trip_descriptor_matches_materializer_plus_object_uri(self) -> None:
        artifact_bytes, expected, _bucket = self.materialized_artifact()
        described = describe_private_evaluation_artifact(artifact_bytes)
        self.assertEqual(set(described), set(PRIVATE_WEEKLY_EVALUATION_FIELDS))
        self.assertEqual(described, expected)

    def test_expected_evaluation_id_is_recomputed(self) -> None:
        artifact_bytes, expected, _bucket = self.materialized_artifact()
        described = describe_private_evaluation_artifact(
            artifact_bytes, expected_artifact_sha256=expected["artifact_sha256"]
        )
        self.assertEqual(described["evaluation_id"], expected["evaluation_id"])
        self.assertTrue(described["evaluation_id"].startswith("weekly_eval_"))

    def test_non_canonical_bytes_are_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        decoded = json.loads(artifact_bytes.decode("utf-8"))
        non_canonical = (json.dumps(decoded, indent=2) + "\n").encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "canonical JSON"):
            describe_private_evaluation_artifact(non_canonical)

    def test_tampered_content_digest_is_rejected(self) -> None:
        artifact_bytes, expected, _bucket = self.materialized_artifact()
        tampered = bytearray(artifact_bytes)
        tampered[-2] ^= 0x01
        with self.assertRaisesRegex(PrivateEvaluationError, "expected digest"):
            describe_private_evaluation_artifact(
                bytes(tampered), expected_artifact_sha256=expected["artifact_sha256"]
            )

    def test_tampered_integrity_block_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["integrity"]["reference_set_sha256"] = "0" * 64
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "reference-set digest"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_counts_are_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["counts"]["choice_count"] = artifact["counts"]["choice_count"] + 1
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "choice_count"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_policy_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["policy"]["reveal_policy_version"] = "tampered"
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "reveal policy"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_private_index_uri_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["round"]["private_index"]["object_uri"] = (
            "supabase://prediction-results/sha256/00/" + "0" * 64
        )
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "private-index"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_reveal_binding_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["reveal_manifest"]["blind_manifest_sha256"] = "0" * 64
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "canonical JSON is inconsistent"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_reference_binding_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["references"][0]["sha256"] = "0" * 64
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "released reference"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_canonical_blind_string_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["blind_manifest_canonical_json"] = (
            artifact["blind_manifest_canonical_json"] + "x"
        )
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "canonical JSON is inconsistent"):
            describe_private_evaluation_artifact(tampered)

    def test_blind_reveal_identity_mismatch_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["reveal_manifest"]["items"][0]["choices"][1]["id"] = "choice-z"
        artifact["reveal_manifest_canonical_json"] = canonical_json(
            artifact["reveal_manifest"]
        )
        artifact["integrity"]["reveal_manifest_sha256"] = hashlib.sha256(
            artifact["reveal_manifest_canonical_json"].encode("utf-8")
        ).hexdigest()
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "item identities differ"):
            describe_private_evaluation_artifact(tampered)

    def test_tampered_prediction_binding_is_rejected(self) -> None:
        artifact_bytes, _expected, _bucket = self.materialized_artifact()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
        artifact["reveal_manifest"]["items"][0]["choices"][0]["prediction_sha256"] = (
            "0" * 64
        )
        reveal_canonical = canonical_json(artifact["reveal_manifest"])
        artifact["reveal_manifest_canonical_json"] = reveal_canonical
        artifact["integrity"]["reveal_manifest_sha256"] = hashlib.sha256(
            reveal_canonical.encode("utf-8")
        ).hexdigest()
        tampered = canonical_json(artifact).encode("utf-8")
        with self.assertRaisesRegex(PrivateEvaluationError, "prediction-set digest"):
            describe_private_evaluation_artifact(tampered)


class LegacyPrivateEvaluationRecoveryTests(unittest.TestCase):
    def test_materializer_recovers_legacy_eligibility_without_rewriting_index(self) -> None:
        round_record, private, private_content = legacy_round_fixture()
        item = private["items"][0]
        target_package = target_package_for_item(item)
        coordinator = FakeCoordinator(
            round_record,
            private_content,
            target_packages={item["target_id"]: target_package},
        )
        reference_content = minimal_reference_mmcif_gz()
        original_private_sha256 = round_record["metadata"]["private_index"]["sha256"]

        def reference(reference_item):
            return coordinate(reference_content, rcsb_reference_url(reference_item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary:
            outcome = materialize_private_preclose_evaluation(
                PRODUCTION_BETA_CATCHUP_ROUND_ID,
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=evaluator,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(outcome["status"], "materialized-private-preclose")
        self.assertEqual(outcome["private_index_sha256"], original_private_sha256)
        self.assertIn(
            ("fetch_campaign_target_packages", "wwpdb-2026-08-08", ["9XYZ"]),
            coordinator.calls,
        )
        artifact = json.loads(coordinator.stored_contents[0])
        self.assertEqual(
            artifact["round"]["private_index"]["sha256"],
            original_private_sha256,
        )
        self.assertNotIn("smiles", json.dumps(artifact))

    def test_materializer_fails_when_legacy_recovery_is_unavailable(self) -> None:
        round_record, _private, private_content = legacy_round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(reference_item):
            return coordinate(reference_content, rcsb_reference_url(reference_item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            SupabasePublicationError
        ):
            materialize_private_preclose_evaluation(
                PRODUCTION_BETA_CATCHUP_ROUND_ID,
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=evaluator,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

    def test_materializer_skips_target_fetch_for_embedded_eligibility(self) -> None:
        round_record, _private, private_content = round_fixture()
        coordinator = FakeCoordinator(round_record, private_content)
        reference_content = minimal_reference_mmcif_gz()

        def reference(reference_item):
            return coordinate(reference_content, rcsb_reference_url(reference_item["target_id"]))

        def evaluator(*args, **kwargs):
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary:
            materialize_private_preclose_evaluation(
                PRODUCTION_BETA_CATCHUP_ROUND_ID,
                temporary,
                coordinator=coordinator,
                reference_resolver=reference,
                evaluator=evaluator,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

        self.assertFalse(
            any(
                isinstance(call, tuple) and call[0] == "fetch_campaign_target_packages"
                for call in coordinator.calls
            )
        )

    def test_ligand_eligibility_from_target_matches_assembly(self) -> None:
        item = source_items()[0]
        package = target_package_for_item(item)
        self.assertEqual(
            ligand_eligibility_from_target(package),
            fixture_ligand_eligibility(),
        )

    def test_legacy_recovery_uses_item_heavy_atoms_when_package_smiles_counts_explicit_h(
        self,
    ) -> None:
        item = source_items()[0]
        smiles = "CCCCCCCCCCCCCCCCC[H]"
        package = target_package_for_item(item, smiles=smiles)
        package["metadata"]["selected_ligand"]["heavy_atoms"] = count_smiles_heavy_atoms(
            smiles
        )
        self.assertEqual(item["ligand"]["heavy_atoms"], 17)
        self.assertEqual(count_smiles_heavy_atoms(smiles), 18)

        class Coordinator:
            def fetch_campaign_target_packages(self, campaign_id, target_ids):
                return {
                    item["target_id"]: {"package": package},
                }

        recovered = _recovered_ligand_eligibility_for_legacy_items(
            Coordinator(),
            {"campaign_id": "wwpdb-2026-08-08"},
            [item["target_id"]],
            private_index={"items": [item]},
        )
        eligibility = recovered[item["target_id"].upper()]
        self.assertEqual(eligibility["heavy_atoms"], 17)
        self.assertEqual(eligibility["smiles"], smiles)
        self.assertTrue(eligibility["passed"])
        package_eligibility = ligand_eligibility_from_target(package)
        self.assertEqual(package_eligibility["heavy_atoms"], 18)
        self.assertNotEqual(
            package_eligibility["heavy_atoms"],
            item["ligand"]["heavy_atoms"],
        )

    def test_legacy_recovery_rejects_package_component_mismatch(self) -> None:
        item = source_items()[0]
        package = target_package_for_item(item)
        package["metadata"]["selected_ligand"]["component_id"] = "OTHER"
        package["entities"][1]["chain_ids"] = ["B"]

        class Coordinator:
            def fetch_campaign_target_packages(self, campaign_id, target_ids):
                return {item["target_id"]: {"package": package}}

        with self.assertRaisesRegex(
            PrivateEvaluationError,
            "package component_id disagrees with item ligand",
        ):
            _recovered_ligand_eligibility_for_legacy_items(
                Coordinator(),
                {"campaign_id": "wwpdb-2026-08-08"},
                [item["target_id"]],
                private_index={"items": [item]},
            )

    def test_legacy_recovery_rejects_conflicting_item_bindings(self) -> None:
        item = source_items()[0]
        conflicting = deepcopy(item)
        conflicting["id"] = "item-conflict"
        conflicting["ligand"] = {
            "component_id": item["ligand"]["component_id"],
            "heavy_atoms": item["ligand"]["heavy_atoms"] + 1,
        }

        class Coordinator:
            def fetch_campaign_target_packages(self, campaign_id, target_ids):
                raise AssertionError("package fetch must not run")

        with self.assertRaisesRegex(
            PrivateEvaluationError,
            "disagree on ligand binding",
        ):
            _recovered_ligand_eligibility_for_legacy_items(
                Coordinator(),
                {"campaign_id": "wwpdb-2026-08-08"},
                [item["target_id"]],
                private_index={"items": [item, conflicting]},
            )

    def test_recover_legacy_ligand_eligibility_end_to_end(self) -> None:
        round_record, private, _private_content = legacy_round_fixture()
        item = private["items"][0]
        smiles = "CCCCCCCCCCCCCCCCC[H]"
        package = target_package_for_item(item, smiles=smiles)
        package["metadata"]["selected_ligand"]["heavy_atoms"] = count_smiles_heavy_atoms(
            smiles
        )
        item["ligand"]["heavy_atoms"] = 17
        private_content = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            private_content
        ).hexdigest()

        class Coordinator:
            def fetch_campaign_target_packages(self, campaign_id, target_ids):
                return {item["target_id"]: {"package": package}}

        coordinator = Coordinator()
        recovered = recover_legacy_ligand_eligibility(
            coordinator,
            round_record,
            private_content,
        )
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertEqual(recovered[item["target_id"].upper()]["heavy_atoms"], 17)


if __name__ == "__main__":
    unittest.main()
