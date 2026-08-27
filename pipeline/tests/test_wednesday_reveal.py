from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

from foldarium_pipeline.clustering import choice_order_digest
from foldarium_pipeline.contracts import canonical_json
from foldarium_pipeline.quiz import build_blind_manifest, manifest_sha256
from foldarium_pipeline.evaluation import (
    EVALUATOR_VERSION,
    EvaluationError,
    LIGAND_MAPPING_POLICY_PARTIAL,
    LIGAND_MAPPING_POLICY_PARTIAL_TASK_SMILES,
    LIGAND_MAPPING_POLICY_FULL_TASK_SMILES,
    RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
    TOPOLOGY_SOURCE_TASK_SMILES,
)
from foldarium_pipeline.wednesday_reveal import (
    CORRECT_RMSD_ANGSTROM,
    WednesdayRevealError,
    WednesdayRevealNotReady,
    _evaluate_validated_round,
    _evaluation_fields,
    _validate_legacy_clustering_ligand_binding,
    _validated_round,
    fetch_rcsb_released_reference,
    rcsb_reference_url,
    run_private_preclose_evaluation,
    run_wednesday_reveal,
)
from foldarium_pipeline.weekly_quiz import (
    LEGACY_LIGAND_ORDER_POLICY,
    WeeklyQuizAssemblyError,
    _weekly_ligand_eligibility,
    clone_weekly_quiz_manifests,
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


def fixture_legacy_ligand_topology_audit(smiles: str) -> dict:
    try:
        return legacy_ligand_topology_audit(smiles)
    except WeeklyQuizAssemblyError as exc:
        raise unittest.SkipTest(
            "Gemmi, NumPy, and RDKit are optional evaluation dependencies"
        ) from exc


def overlay_evaluation_score(rmsd: float = 0.8) -> dict:
    return {
        "evaluator_version": "test-evaluator/v1",
        "rmsd": rmsd,
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


def aug22_26wd_round_fixture(
    *, heavy_atoms: int = 66
) -> tuple[dict, dict, bytes]:
    round_id = "weekly-2026-08-22-beta-v1"
    smiles = "C" * heavy_atoms
    eligibility = {
        "policy": fixture_ligand_eligibility()["policy"],
        "passed": True,
        "component_id": "AAO",
        "heavy_atoms": heavy_atoms,
        "smiles": smiles,
        "smiles_sha256": hashlib.sha256(smiles.encode("utf-8")).hexdigest(),
    }
    source = [
        {
            "id": "26WD",
            "target_id": "26WD",
            "ligand": {"component_id": "AAO", "heavy_atoms": heavy_atoms},
            "ligand_eligibility": eligibility,
            "week": "2026-08-22",
            "protein_uri": "supabase://quiz/protein.pdb",
            "choices": [
                {
                    "run_id": "run-test",
                    "sample_id": "sample-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "pose_uri": "supabase://quiz/pose-a.pdb",
                    "cluster_id": "cluster-a",
                    "is_rep": True,
                }
            ],
        }
    ]
    blind, private = build_blind_manifest(round_id, source)
    content = canonical_json(private).encode("utf-8")
    round_record = {
        "round_id": round_id,
        "status": "open",
        "closes_at": "2026-08-22T20:00:00Z",
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


def promoted_round_fixture(
    *,
    include_choice_identity_round_id: bool = True,
) -> tuple[dict, dict, bytes]:
    source_round_id = "weekly-2026-08-08-preview-v5-global-tm-29"
    promoted_round_id = "weekly-2026-08-08-beta-v5-global-tm-29"
    blind, private = build_blind_manifest(source_round_id, source_items())
    source_blind_digest = manifest_sha256(blind)
    promoted_blind, promoted_private = clone_weekly_quiz_manifests(
        blind, private, round_id=promoted_round_id
    )
    if not include_choice_identity_round_id:
        promoted_private.pop("choice_identity_round_id", None)
    content = canonical_json(promoted_private).encode("utf-8")
    round_record = {
        "round_id": promoted_round_id,
        "status": "open",
        "environment": "production",
        "opens_at": "2026-08-14T20:05:00Z",
        "closes_at": "2026-08-17T20:00:00Z",
        "blind_manifest": promoted_blind,
        "blind_manifest_sha256": manifest_sha256(promoted_blind),
        "metadata": {
            "private_index": {
                "object_uri": "supabase://private/index.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": "application/json",
            },
            "promoted_from_round_id": source_round_id,
            "promoted_from_blind_manifest_sha256": source_blind_digest,
        },
    }
    return round_record, promoted_private, content


def coordinate(content: bytes, uri: str) -> dict:
    return {
        "content": content,
        "object_uri": uri,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "chemical/x-mmcif",
    }


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


def legacy_audit_choice_row(
    *,
    identity_round_id: str,
    item_id: str,
    choice: dict,
) -> dict:
    artifact_sha256 = choice.get("artifact_sha256")
    if not isinstance(artifact_sha256, str) or not artifact_sha256:
        artifact_sha256 = hashlib.sha256(
            f"{choice['run_id']}:{choice['sample_id']}".encode()
        ).hexdigest()
        choice["artifact_sha256"] = artifact_sha256
    return {
        "choice_digest": choice_order_digest(
            identity_round_id,
            item_id,
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


def legacy_clustering_for_item(
    item: dict,
    *,
    identity_round_id: str,
    smiles: str = "CCCCCCCCCCCCCCCCC",
) -> dict:
    audit = fixture_legacy_ligand_topology_audit(smiles)
    audit["choices"] = [
        legacy_audit_choice_row(
            identity_round_id=identity_round_id,
            item_id=item["id"],
            choice=choice,
        )
        for choice in item["choices"]
    ]
    return {"ligand_atom_mapping": audit}


def legacy_private_fixture(
    mutator=None,
    *,
    identity_round_id: str = "weekly-2026-08-08",
) -> tuple[dict, bytes, dict[str, dict]]:
    round_record, private, _content = round_fixture()
    item = private["items"][0]
    item.pop("ligand_eligibility", None)
    item["clustering"] = legacy_clustering_for_item(
        item, identity_round_id=identity_round_id
    )
    if mutator is not None:
        mutator(item)
    private_content = canonical_json(private).encode("utf-8")
    round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
        private_content
    ).hexdigest()
    target_package = target_package_for_item(item)
    recovered = {
        item["target_id"].upper(): ligand_eligibility_from_target(target_package)
    }
    return round_record, private_content, recovered


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
    @staticmethod
    def preclose_round() -> tuple[dict, dict, bytes]:
        round_record, private, private_content = round_fixture()
        round_record.update(
            {
                "campaign_id": "wwpdb-2026-08-08",
                "environment": "production",
                "opens_at": "2026-08-08T03:00:00Z",
                "reveal_manifest": None,
                "reveal_manifest_sha256": None,
                "revealed_at": None,
            }
        )
        return round_record, private, private_content

    def test_private_preclose_path_has_no_publisher_and_uses_same_evaluator(self) -> None:
        round_record, private, private_content = self.preclose_round()
        self.assertNotIn(
            "reveal_publisher", inspect.signature(run_private_preclose_evaluation).parameters
        )
        prediction_calls = []
        reference_calls = []

        def prediction(choice):
            prediction_calls.append((choice["run_id"], choice["sample_id"]))
            return coordinate(
                f"prediction {choice['id']}".encode(),
                f"supabase://private/{choice['id']}.cif",
            )

        def reference(item):
            reference_calls.append(item["target_id"])
            return coordinate(
                gzip.compress(b"released", mtime=0),
                rcsb_reference_url(item["target_id"]),
            )

        with tempfile.TemporaryDirectory() as temporary:
            result = run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=reference,
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(0.75),
                now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "evaluated-private-preclose")
        self.assertIsNone(result["publish_response"])
        self.assertEqual(reference_calls, ["9XYZ"])
        self.assertEqual(len(prediction_calls), len(private["items"][0]["choices"]))

    def test_private_preclose_path_requires_active_production_unrevealed_round(self) -> None:
        base, _private, private_content = self.preclose_round()
        cases = (
            ("preview", "open", None, datetime(2026, 8, 11, 12, tzinfo=timezone.utc)),
            ("production", "revealed", {}, datetime(2026, 8, 11, 12, tzinfo=timezone.utc)),
            ("production", "open", {}, datetime(2026, 8, 11, 12, tzinfo=timezone.utc)),
            ("production", "open", None, datetime(2026, 8, 8, 2, tzinfo=timezone.utc)),
            ("production", "open", None, datetime(2026, 8, 12, 0, tzinfo=timezone.utc)),
        )
        for environment, status, reveal, now in cases:
            with self.subTest(
                environment=environment, status=status, reveal=reveal, now=now
            ):
                round_record = dict(base)
                round_record["metadata"] = deepcopy(base["metadata"])
                round_record["environment"] = environment
                round_record["status"] = status
                round_record["reveal_manifest"] = reveal
                calls = []
                with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
                    WednesdayRevealError
                ):
                    run_private_preclose_evaluation(
                        round_record,
                        private_content,
                        temporary,
                        prediction_resolver=lambda choice: calls.append(choice),
                        reference_resolver=lambda item: calls.append(item),
                        now=now,
                    )
                self.assertEqual(calls, [])

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

        def evaluator(reference_path, prediction_path, **kwargs):
            evaluator_calls.append(
                (Path(reference_path), Path(prediction_path), kwargs)
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
        self.assertTrue(reveal_choices["run-of3"]["accepted_correct"])
        self.assertTrue(reveal_choices["run-boltz"]["accepted_correct"])
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


class PromotedRoundIdentityTests(unittest.TestCase):
    def test_promoted_round_accepts_preserved_choice_ids(self) -> None:
        round_record, _private, private_content = promoted_round_fixture()

        def prediction(choice):
            return coordinate(b"raw complex", f"supabase://private/{choice['id']}.cif")

        reference_body = gzip.compress(b"released reference", mtime=0)

        def reference(item):
            return coordinate(reference_body, rcsb_reference_url(item["target_id"]))

        with tempfile.TemporaryDirectory() as temporary:
            result = run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=reference,
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "evaluated-private-preclose")
        self.assertEqual(result["round_id"], round_record["round_id"])

    def test_live_style_promoted_round_without_choice_identity_field(self) -> None:
        round_record, _private, private_content = promoted_round_fixture(
            include_choice_identity_round_id=False
        )
        self.assertNotIn("choice_identity_round_id", _private)

        def prediction(choice):
            return coordinate(b"raw complex", f"supabase://private/{choice['id']}.cif")

        reference_body = gzip.compress(b"released reference", mtime=0)

        with tempfile.TemporaryDirectory() as temporary:
            result = run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=prediction,
                reference_resolver=lambda item: coordinate(
                    reference_body, rcsb_reference_url(item["target_id"])
                ),
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "evaluated-private-preclose")

    def test_promoted_round_rejects_tampered_run_identity(self) -> None:
        round_record, private, _private_content = promoted_round_fixture()
        private["items"][0]["choices"][0]["run_id"] = "different-run"
        tampered = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            tampered
        ).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "private choice identity"
        ):
            run_private_preclose_evaluation(
                round_record,
                tampered,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

    def test_malformed_promotion_metadata_is_rejected(self) -> None:
        round_record, _private, private_content = promoted_round_fixture()
        round_record["metadata"].pop("promoted_from_blind_manifest_sha256")
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "promotion metadata is incomplete"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

        round_record, _private, private_content = promoted_round_fixture()
        round_record["metadata"]["promoted_from_blind_manifest_sha256"] = "not-a-digest"
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "promotion source blind digest"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )


class LigandEligibilityValidationTests(unittest.TestCase):
    @staticmethod
    def tampered_private_fixture(
        mutator,
    ) -> tuple[dict, bytes]:
        round_record, private, _content = round_fixture()
        mutator(private["items"][0])
        private_content = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            private_content
        ).hexdigest()
        return round_record, private_content

    def test_missing_ligand_eligibility_is_rejected_without_recovery(self) -> None:
        round_record, private_content = self.tampered_private_fixture(
            lambda item: item.pop("ligand_eligibility", None)
        )
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "ligand_eligibility"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
            )

    def test_failed_eligibility_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            eligibility = fixture_ligand_eligibility()
            eligibility["passed"] = False
            item["ligand_eligibility"] = eligibility

        round_record, private_content = self.tampered_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "did not pass assembly policy"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
            )

    def test_smiles_digest_tampering_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            eligibility = fixture_ligand_eligibility()
            eligibility["smiles_sha256"] = "0" * 64
            item["ligand_eligibility"] = eligibility

        round_record, private_content = self.tampered_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "SMILES digest mismatch"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
            )

    def test_component_or_count_tampering_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            eligibility = fixture_ligand_eligibility()
            eligibility["heavy_atoms"] = 16
            item["ligand_eligibility"] = eligibility

        round_record, private_content = self.tampered_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "disagrees with item ligand"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
            )


class LegacyLigandEligibilityRecoveryTests(unittest.TestCase):
    def test_recovered_eligibility_allows_legacy_private_index(self) -> None:
        round_record, private, _content = promoted_round_fixture()
        item = private["items"][0]
        source_round_id = round_record["metadata"]["promoted_from_round_id"]
        item.pop("ligand_eligibility", None)
        item["clustering"] = legacy_clustering_for_item(
            item, identity_round_id=source_round_id
        )
        private_content = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            private_content
        ).hexdigest()
        recovered = {
            item["target_id"].upper(): ligand_eligibility_from_target(
                target_package_for_item(item)
            )
        }
        reference_body = gzip.compress(b"released reference", mtime=0)
        with tempfile.TemporaryDirectory() as temporary:
            result = run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                reference_resolver=lambda reference_item: coordinate(
                    reference_body, rcsb_reference_url(reference_item["target_id"])
                ),
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                recovered_ligand_eligibility=recovered,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "evaluated-private-preclose")
        self.assertEqual(result["item_count"], 1)

    def test_legacy_clustering_validation_uses_digest_only_topology(self) -> None:
        round_record, private, _content = promoted_round_fixture()
        item = private["items"][0]
        source_round_id = round_record["metadata"]["promoted_from_round_id"]
        item.pop("ligand_eligibility", None)
        smiles = "COC"
        eligibility = fixture_ligand_eligibility(smiles=smiles, heavy_atoms=3)
        item["ligand"]["heavy_atoms"] = 3
        audit = fixture_legacy_ligand_topology_audit(smiles)
        audit["choices"] = legacy_clustering_for_item(
            item, identity_round_id=source_round_id, smiles=smiles
        )["ligand_atom_mapping"]["choices"]
        item["clustering"] = {"ligand_atom_mapping": audit}
        module = __import__("foldarium_pipeline.weekly_quiz", fromlist=["LIGAND_AUTOMORPHISM_CAP"])
        original_cap = module.LIGAND_AUTOMORPHISM_CAP
        try:
            module.LIGAND_AUTOMORPHISM_CAP = 1
            with self.assertRaises(WeeklyQuizAssemblyError):
                legacy_ligand_topology_audit(smiles)
            _validate_legacy_clustering_ligand_binding(
                item,
                eligibility,
                identity_round_id=source_round_id,
                item_id=item["id"],
            )
        finally:
            module.LIGAND_AUTOMORPHISM_CAP = original_cap

    def test_embedded_and_recovered_eligibility_mismatch_is_rejected(self) -> None:
        round_record, private_content = LigandEligibilityValidationTests.tampered_private_fixture(
            lambda item: item.__setitem__(
                "ligand_eligibility", fixture_ligand_eligibility()
            )
        )
        recovered_eligibility = deepcopy(fixture_ligand_eligibility())
        recovered_eligibility["smiles"] = recovered_eligibility["smiles"][:-1] + "N"
        recovered_eligibility["smiles_sha256"] = hashlib.sha256(
            recovered_eligibility["smiles"].encode("utf-8")
        ).hexdigest()
        recovered = {"9XYZ": recovered_eligibility}
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "embedded and recovered ligand_eligibility disagree"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_legacy_clustering_policy_tamper_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["clustering"]["ligand_atom_mapping"]["policy"] = "tampered"

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "legacy ligand_atom_mapping policy is invalid"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_legacy_clustering_topology_tamper_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["clustering"]["ligand_atom_mapping"]["source_topology_sha256"] = (
                "0" * 64
            )

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "topology digest does not match task SMILES"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_legacy_clustering_choice_method_tamper_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["choices"][0]["method_version"] = "9.9.9"

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "unsupported method version"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_legacy_clustering_smiles_digest_tamper_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["clustering"]["ligand_atom_mapping"]["source_smiles_sha256"] = (
                "0" * 64
            )

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "SMILES digest does not match eligibility"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )


class LegacyClusteringChoiceDigestTests(unittest.TestCase):
    @staticmethod
    def same_method_source_items() -> list[dict]:
        base = source_items()[0]
        return [
            {
                **base,
                "choices": [
                    {
                        "run_id": "run-boltz-a",
                        "sample_id": "sample-boltz-a",
                        "artifact_sha256": "a" * 64,
                        "method": "boltz2",
                        "method_version": "2.2.1",
                        "pose_uri": "supabase://quiz/pose-a.pdb",
                        "cluster_id": "cluster-a",
                        "is_rep": True,
                    },
                    {
                        "run_id": "run-boltz-b",
                        "sample_id": "sample-boltz-b",
                        "artifact_sha256": "b" * 64,
                        "method": "boltz2",
                        "method_version": "2.2.1",
                        "pose_uri": "supabase://quiz/pose-b.pdb",
                        "cluster_id": "cluster-a",
                        "is_rep": False,
                    },
                ],
            }
        ]

    @staticmethod
    def legacy_preclose_fixture(
        round_record: dict,
        private_content: bytes,
        recovered: dict[str, dict],
    ) -> dict:
        reference_body = gzip.compress(b"released reference", mtime=0)
        with tempfile.TemporaryDirectory() as temporary:
            return run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                reference_resolver=lambda reference_item: coordinate(
                    reference_body, rcsb_reference_url(reference_item["target_id"])
                ),
                evaluator=lambda *args, **kwargs: overlay_evaluation_score(),
                recovered_ligand_eligibility=recovered,
                now=datetime(2026, 8, 15, 2, tzinfo=timezone.utc),
            )

    @staticmethod
    def build_legacy_round(
        round_id: str,
        items: list[dict],
        *,
        identity_round_id: str,
        round_extra: dict | None = None,
    ) -> tuple[dict, bytes, dict[str, dict]]:
        blind, private = build_blind_manifest(round_id, items)
        for item in private["items"]:
            item.pop("ligand_eligibility", None)
            item["clustering"] = legacy_clustering_for_item(
                item, identity_round_id=identity_round_id
            )
        private_content = canonical_json(private).encode("utf-8")
        round_record = {
            "round_id": round_id,
            "status": "open",
            "environment": "production",
            "opens_at": "2026-08-14T20:05:00Z",
            "closes_at": "2026-08-17T20:00:00Z",
            "blind_manifest": blind,
            "blind_manifest_sha256": manifest_sha256(blind),
            "metadata": {
                "private_index": {
                    "object_uri": "supabase://private/index.json",
                    "sha256": hashlib.sha256(private_content).hexdigest(),
                    "media_type": "application/json",
                }
            },
        }
        if round_extra:
            round_record.update(round_extra)
        recovered = {
            item["target_id"].upper(): ligand_eligibility_from_target(
                target_package_for_item(item)
            )
            for item in private["items"]
        }
        return round_record, private_content, recovered

    def test_two_choices_with_same_method_version_bind_by_choice_digest(self) -> None:
        round_record, private_content, recovered = self.build_legacy_round(
            "weekly-2026-08-08",
            self.same_method_source_items(),
            identity_round_id="weekly-2026-08-08",
        )
        result = self.legacy_preclose_fixture(round_record, private_content, recovered)
        self.assertEqual(result["status"], "evaluated-private-preclose")
        self.assertEqual(result["choice_count"], 2)

    def test_promoted_round_uses_source_identity_round_for_choice_digest(self) -> None:
        source_round_id = "weekly-2026-08-08-preview-v5-global-tm-29"
        promoted_round_id = "weekly-2026-08-08-beta-v5-global-tm-29"
        blind, private = build_blind_manifest(source_round_id, self.same_method_source_items())
        source_blind_digest = manifest_sha256(blind)
        promoted_blind, promoted_private = clone_weekly_quiz_manifests(
            blind, private, round_id=promoted_round_id
        )
        item = promoted_private["items"][0]
        item.pop("ligand_eligibility", None)
        item["clustering"] = legacy_clustering_for_item(
            item, identity_round_id=source_round_id
        )
        private_content = canonical_json(promoted_private).encode("utf-8")
        round_record = {
            "round_id": promoted_round_id,
            "status": "open",
            "environment": "production",
            "opens_at": "2026-08-14T20:05:00Z",
            "closes_at": "2026-08-17T20:00:00Z",
            "blind_manifest": promoted_blind,
            "blind_manifest_sha256": manifest_sha256(promoted_blind),
            "metadata": {
                "private_index": {
                    "object_uri": "supabase://private/index.json",
                    "sha256": hashlib.sha256(private_content).hexdigest(),
                    "media_type": "application/json",
                },
                "promoted_from_round_id": source_round_id,
                "promoted_from_blind_manifest_sha256": source_blind_digest,
            },
        }
        recovered = {
            item["target_id"].upper(): ligand_eligibility_from_target(
                target_package_for_item(item)
            )
        }
        result = self.legacy_preclose_fixture(round_record, private_content, recovered)
        self.assertEqual(result["status"], "evaluated-private-preclose")

    def test_promoted_round_rejects_current_round_choice_digests(self) -> None:
        source_round_id = "weekly-2026-08-08-preview-v5-global-tm-29"
        promoted_round_id = "weekly-2026-08-08-beta-v5-global-tm-29"
        blind, private = build_blind_manifest(source_round_id, self.same_method_source_items())
        promoted_blind, promoted_private = clone_weekly_quiz_manifests(
            blind, private, round_id=promoted_round_id
        )
        item = promoted_private["items"][0]
        item.pop("ligand_eligibility", None)
        item["clustering"] = legacy_clustering_for_item(
            item, identity_round_id=promoted_round_id
        )
        private_content = canonical_json(promoted_private).encode("utf-8")
        round_record = {
            "round_id": promoted_round_id,
            "status": "open",
            "environment": "production",
            "opens_at": "2026-08-14T20:05:00Z",
            "closes_at": "2026-08-17T20:00:00Z",
            "blind_manifest": promoted_blind,
            "blind_manifest_sha256": manifest_sha256(promoted_blind),
            "metadata": {
                "private_index": {
                    "object_uri": "supabase://private/index.json",
                    "sha256": hashlib.sha256(private_content).hexdigest(),
                    "media_type": "application/json",
                },
                "promoted_from_round_id": source_round_id,
                "promoted_from_blind_manifest_sha256": manifest_sha256(blind),
            },
        }
        recovered = {
            item["target_id"].upper(): ligand_eligibility_from_target(
                target_package_for_item(item)
            )
        }
        with self.assertRaisesRegex(
            WednesdayRevealError,
            "choice audit does not match a private choice",
        ):
            self.legacy_preclose_fixture(round_record, private_content, recovered)

    def test_duplicate_audit_rows_are_rejected(self) -> None:
        def mutate(item: dict) -> None:
            audits = item["clustering"]["ligand_atom_mapping"]["choices"]
            audits.append(deepcopy(audits[0]))

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError,
            "choice audit does not match a private choice",
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_missing_audit_row_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["clustering"]["ligand_atom_mapping"]["choices"].pop()

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError,
            "choice audit does not match a private choice",
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_extra_audit_row_is_rejected(self) -> None:
        def mutate(item: dict) -> None:
            item["clustering"]["ligand_atom_mapping"]["choices"].append(
                {
                    "choice_digest": "0" * 64,
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "mapping_mode": "source-heavy-atom-index-order",
                }
            )

        round_record, private_content, recovered = legacy_private_fixture(mutate)
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError, "extra choice audit rows"
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )

    def test_reused_audit_row_is_rejected(self) -> None:
        round_record, private, _content = round_fixture()
        item = private["items"][0]
        item.pop("ligand_eligibility", None)
        first_choice = item["choices"][0]
        first_choice["artifact_sha256"] = "a" * 64
        second_choice = item["choices"][1]
        second_choice["method"] = first_choice["method"]
        second_choice["method_version"] = first_choice["method_version"]
        second_choice["run_id"] = first_choice["run_id"]
        second_choice["sample_id"] = first_choice["sample_id"]
        second_choice["artifact_sha256"] = first_choice["artifact_sha256"]
        audit_row = legacy_audit_choice_row(
            identity_round_id="weekly-2026-08-08",
            item_id=item["id"],
            choice=first_choice,
        )
        item["clustering"] = {
            "ligand_atom_mapping": {
                **fixture_legacy_ligand_topology_audit("CCCCCCCCCCCCCCCCC"),
                "choices": [audit_row],
            }
        }
        private_content = canonical_json(private).encode("utf-8")
        round_record["metadata"]["private_index"]["sha256"] = hashlib.sha256(
            private_content
        ).hexdigest()
        recovered = {
            item["target_id"].upper(): ligand_eligibility_from_target(
                target_package_for_item(item)
            )
        }
        with tempfile.TemporaryDirectory() as temporary, self.assertRaisesRegex(
            WednesdayRevealError,
            "choice audit does not match a private choice",
        ):
            run_private_preclose_evaluation(
                round_record,
                private_content,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"raw complex", f"supabase://private/{choice['id']}.cif"
                ),
                recovered_ligand_eligibility=recovered,
            )


class EvaluationFieldThreadingTests(unittest.TestCase):
    def test_partial_scoring_audit_fields_are_threaded_into_reveal_provenance(self) -> None:
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "sequence_similarity": 0.98,
                "reference_receptor_chain": "A",
                "reference_heavy_atoms_expected": 24,
                "reference_heavy_atoms_observed": 21,
                "reference_heavy_atoms_scored": 21,
                "reference_coverage": 21 / 24,
                "ligand_mapping_policy": LIGAND_MAPPING_POLICY_PARTIAL,
            }
        )
        self.assertEqual(fields["evaluator_version"], EVALUATOR_VERSION)
        self.assertEqual(fields["reference_heavy_atoms_observed"], 21)
        self.assertAlmostEqual(fields["reference_coverage"], 21 / 24)
        self.assertEqual(fields["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_PARTIAL)

    def test_task_smiles_audit_fields_are_threaded_without_raw_smiles(self) -> None:
        digest = "a" * 64
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "ligand_mapping_policy": LIGAND_MAPPING_POLICY_FULL_TASK_SMILES,
                "ligand_topology_source": TOPOLOGY_SOURCE_TASK_SMILES,
                "ligand_order_policy": LEGACY_LIGAND_ORDER_POLICY,
                "task_smiles_sha256": digest,
                "smiles": "must-not-leak",
            }
        )
        self.assertEqual(fields["ligand_mapping_policy"], LIGAND_MAPPING_POLICY_FULL_TASK_SMILES)
        self.assertEqual(fields["ligand_topology_source"], TOPOLOGY_SOURCE_TASK_SMILES)
        self.assertEqual(fields["ligand_order_policy"], LEGACY_LIGAND_ORDER_POLICY)
        self.assertEqual(fields["task_smiles_sha256"], digest)
        self.assertNotIn("smiles", fields)

    def test_altloc_audit_fields_are_threaded_when_present(self) -> None:
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "reference_ligand_altloc": "A",
                "predicted_ligand_altloc": "B",
            }
        )
        self.assertEqual(fields["reference_ligand_altloc"], "A")
        self.assertEqual(fields["predicted_ligand_altloc"], "B")
        self.assertNotIn("reference_pocket_pdb", fields)

    def test_reference_pocket_pdb_is_not_threaded_into_reveal_provenance(self) -> None:
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "reference_pocket_pdb": "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00  0.00           C\nEND\n",
            }
        )
        self.assertNotIn("reference_pocket_pdb", fields)


class ReleasedPartialReferenceOverrideRevealTests(unittest.TestCase):
    def test_validated_round_attaches_authenticated_override_for_26wd(self) -> None:
        round_record, private, _ = aug22_26wd_round_fixture()
        _, _, items = _validated_round(round_record, private)
        self.assertEqual(len(items), 1)
        override = items[0].get("released_partial_reference_override")
        self.assertIsNotNone(override)
        self.assertEqual(override["minimum_observed_heavy_atoms"], 52)
        self.assertEqual(override["policy"], RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY)

    def test_validated_round_rejects_override_binding_mismatch(self) -> None:
        round_record, private, _ = aug22_26wd_round_fixture(heavy_atoms=65)
        with self.assertRaisesRegex(EvaluationError, "heavy_atoms binding mismatch"):
            _validated_round(round_record, private)

    def test_other_rounds_do_not_receive_override(self) -> None:
        round_record, private, _ = round_fixture()
        _, _, items = _validated_round(round_record, private)
        self.assertNotIn("released_partial_reference_override", items[0])

    def test_evaluate_validated_round_passes_minimum_reference_heavy_atoms(self) -> None:
        round_record, private, _ = aug22_26wd_round_fixture()
        _, blind, items = _validated_round(round_record, private)
        captured: list[dict] = []

        def evaluator(*_args, **kwargs):
            captured.append(dict(kwargs))
            return overlay_evaluation_score()

        with tempfile.TemporaryDirectory() as temporary:
            _evaluate_validated_round(
                round_record["round_id"],
                blind,
                items,
                temporary,
                prediction_resolver=lambda choice: coordinate(
                    b"prediction", f"supabase://private/{choice['id']}.cif"
                ),
                reference_resolver=lambda item: coordinate(
                    b"reference", f"supabase://reference/{item['target_id']}.cif"
                ),
                evaluator=evaluator,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["minimum_reference_heavy_atoms"], 52)

    def test_override_minimum_observed_propagates_into_reveal_provenance(self) -> None:
        fields = _evaluation_fields(
            {
                "evaluator_version": EVALUATOR_VERSION,
                "receptor_rmsd": 0.5,
                "reference_heavy_atoms_expected": 66,
                "reference_heavy_atoms_observed": 52,
                "reference_heavy_atoms_scored": 52,
                "reference_heavy_atoms_minimum_observed": 52,
                "reference_coverage": 52 / 66,
                "ligand_mapping_policy": LIGAND_MAPPING_POLICY_PARTIAL,
                "released_partial_reference_override_policy": (
                    RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY
                ),
            }
        )
        self.assertEqual(fields["reference_heavy_atoms_minimum_observed"], 52)
        self.assertEqual(
            fields["released_partial_reference_override_policy"],
            RELEASED_PARTIAL_REFERENCE_OVERRIDE_POLICY,
        )


if __name__ == "__main__":
    unittest.main()
