from __future__ import annotations

import io
import json
import tempfile
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

import unittest

from foldarium_pipeline.contracts import SCHEMA_VERSION, canonical_json
from foldarium_pipeline.quiz import build_blind_manifest
from foldarium_pipeline.weekly_selector import (
    CLIENT_TEMPLATE,
    KIT_SCHEMA_VERSION,
    MAX_SUBMISSION_PAYLOAD_BYTES,
    SUBMISSION_SCHEMA_VERSION,
    WeeklySelectorError,
    assert_no_forbidden_content,
    build_selector_kit,
    build_selector_submission,
    build_submission_schema,
    build_submission_template,
    digest_selector_submission,
    parse_selector_kit,
    submit_selector_submission,
    validate_blind_manifest,
    validate_selector_kit_manifest,
    validate_selector_submission,
    verify_selector_kit_zip,
)
from foldarium_pipeline.weekly_selector_prompt import (
    SELECTOR_ITEM_PROMPT_TEMPLATE,
    SELECTOR_MODEL_RESPONSE_SCHEMA,
    SELECTOR_PROMPT_PROFILE_ID,
    SELECTOR_PROMPT_SHA256,
    SELECTOR_SYSTEM_PROMPT,
    selector_prompt_profile,
)


def source_items() -> list[dict]:
    return [
        {
            "id": "target-1",
            "target_id": "cameo-target-1",
            "ligand": "DRG",
            "week": "2026-08-08",
            "protein_uri": "supabase://bucket/protein.pdb",
            "choices": [
                {
                    "run_id": "run-of3",
                    "sample_id": "sample-1",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "cluster_id": "cluster-a",
                    "is_rep": True,
                    "pose_uri": "supabase://bucket/of3-1.pdb",
                    "protein_uri": "supabase://bucket/of3-protein.pdb",
                    "pocket_uri": "supabase://bucket/of3-pocket.pdb",
                },
                {
                    "run_id": "run-boltz",
                    "sample_id": "sample-1",
                    "method": "boltz2",
                    "method_version": "2.2.1",
                    "cluster_id": "cluster-b",
                    "is_rep": False,
                    "pose_uri": "supabase://bucket/boltz-1.pdb",
                    "protein_uri": "supabase://bucket/boltz-protein.pdb",
                    "pocket_uri": "supabase://bucket/boltz-pocket.pdb",
                },
            ],
        },
        {
            "id": "target-2",
            "target_id": "cameo-target-2",
            "ligand": "LIG",
            "week": "2026-08-08",
            "protein_uri": "supabase://bucket/protein-2.pdb",
            "choices": [
                {
                    "run_id": "run-of3-2",
                    "sample_id": "sample-2",
                    "method": "openfold3",
                    "method_version": "0.4.4",
                    "cluster_id": "cluster-b",
                    "is_rep": True,
                    "pose_uri": "supabase://bucket/of3-2.pdb",
                    "protein_uri": "supabase://bucket/of3-protein-2.pdb",
                    "pocket_uri": "supabase://bucket/of3-pocket-2.pdb",
                },
            ],
        },
    ]


def target_for(item_id: str) -> dict[str, Any]:
    if item_id == "target-1":
        return {
            "schema_version": SCHEMA_VERSION,
            "target_id": "cameo-target-1",
            "entities": [
                {"type": "protein", "chain_ids": ["A"], "sequence": "ACDEFGHIK"},
                {"type": "ligand", "chain_ids": ["L"], "smiles": "CCO"},
            ],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "target_id": "cameo-target-2",
        "entities": [
            {"type": "protein", "chain_ids": ["A"], "sequence": "MKFLVN"},
            {"type": "ligand", "chain_ids": ["L"], "ccd_codes": ["ATP"]},
        ],
    }


def asset_bytes(label: str) -> bytes:
    return f"ATOM      1  C   LIG L   1       1.000   2.000   3.000  1.00  0.00           C  \n# {label}\n".encode(
        "utf-8"
    )


def build_fixture_assets(blind: dict[str, Any]) -> dict[tuple[str, str], dict[str, bytes]]:
    assets: dict[tuple[str, str], dict[str, bytes]] = {}
    for item in blind["items"]:
        item_id = item["id"]
        for choice in item["choices"]:
            choice_id = choice["id"]
            assets[(item_id, choice_id)] = {
                "pose": asset_bytes(f"{item_id}:{choice_id}:pose"),
                "protein": asset_bytes(f"{item_id}:{choice_id}:protein"),
                "pocket": asset_bytes(f"{item_id}:{choice_id}:pocket"),
            }
    return assets


def none_item(item_id: str) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "clustered": {"selection_kind": "none"},
        "unclustered": {"selection_kind": "none"},
    }


def submission_for(
    kit: dict[str, Any],
    items: list[dict[str, Any]],
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "submission_id": str(uuid.uuid4()),
        "environment": kit["environment"],
        "round_id": kit["round_id"],
        "blind_manifest_sha256": kit["blind_manifest_sha256"],
        "kit_sha256": kit["kit_sha256"],
        "items": items,
        **overrides,
    }


class WeeklySelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.round_id = "weekly-2026-08-08"
        self.blind, _private = build_blind_manifest(self.round_id, source_items())
        self.targets = {item_id: target_for(item_id) for item_id in ("target-1", "target-2")}
        self.assets = build_fixture_assets(self.blind)

    def _build(self) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        zip_bytes, descriptor = build_selector_kit(
            round_id=self.round_id,
            environment="preview",
            blind_manifest=self.blind,
            targets_by_item_id=self.targets,
            assets_by_choice=self.assets,
        )
        kit = verify_selector_kit_zip(zip_bytes)
        return zip_bytes, descriptor, kit

    def test_builds_deterministic_zip_and_descriptor(self) -> None:
        first_zip, first_descriptor, first_kit = self._build()
        second_zip, second_descriptor, second_kit = self._build()

        self.assertEqual(first_zip, second_zip)
        self.assertEqual(first_descriptor, second_descriptor)
        self.assertEqual(first_kit, second_kit)
        self.assertEqual(first_descriptor["kit_sha256"], first_kit["kit_sha256"])
        self.assertEqual(first_descriptor["schema_version"], KIT_SCHEMA_VERSION)
        self.assertEqual(first_descriptor["environment"], "preview")
        self.assertEqual(first_kit["environment"], "preview")
        self.assertEqual(
            first_descriptor["blind_manifest_sha256"],
            first_kit["blind_manifest_sha256"],
        )
        self.assertEqual(first_descriptor["item_count"], 2)
        self.assertEqual(first_descriptor["choice_count"], 3)
        self.assertEqual(
            first_kit["prompt_profile"]["prompt_profile_id"],
            SELECTOR_PROMPT_PROFILE_ID,
        )
        self.assertEqual(
            first_kit["prompt_profile"]["prompt_sha256"],
            SELECTOR_PROMPT_SHA256,
        )

    def test_zip_contains_required_paths_and_canonical_metadata(self) -> None:
        zip_bytes, _descriptor, kit = self._build()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertIn("README.md", names)
            self.assertIn("manifest.json", names)
            self.assertIn("schemas/submission.schema.json", names)
            self.assertIn("schemas/model-response.schema.json", names)
            self.assertIn("prompts/profile.json", names)
            self.assertIn("prompts/system.txt", names)
            self.assertIn("prompts/item-template.txt", names)
            self.assertIn("client/foldarium_selector_client.py", names)
            for item in kit["items"]:
                self.assertIn(f"items/{item['item_id']}/target.json", names)
                for choice in item["choices"]:
                    prefix = f"items/{item['item_id']}/choices/{choice['choice_id']}"
                    self.assertIn(f"{prefix}/pose.pdb", names)
                    self.assertIn(f"{prefix}/protein.pdb", names)
                    self.assertIn(f"{prefix}/pocket.pdb", names)
            for name in names:
                info = archive.getinfo(name)
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
            manifest_raw = archive.read("manifest.json")
            self.assertEqual(
                manifest_raw,
                (canonical_json(json.loads(manifest_raw)) + "\n").encode("utf-8"),
            )
            self.assertEqual(
                archive.read("client/foldarium_selector_client.py"),
                CLIENT_TEMPLATE.encode("utf-8"),
            )
            schema = json.loads(archive.read("schemas/submission.schema.json"))
            self.assertEqual(schema, build_submission_schema())
            self.assertEqual(
                json.loads(archive.read("schemas/model-response.schema.json")),
                SELECTOR_MODEL_RESPONSE_SCHEMA,
            )
            self.assertEqual(
                json.loads(archive.read("prompts/profile.json")),
                selector_prompt_profile(),
            )
            self.assertEqual(
                archive.read("prompts/system.txt").decode("utf-8"),
                SELECTOR_SYSTEM_PROMPT,
            )
            self.assertEqual(
                archive.read("prompts/item-template.txt").decode("utf-8"),
                SELECTOR_ITEM_PROMPT_TEMPLATE,
            )

    def test_prompt_profile_is_canonical_and_blind(self) -> None:
        profile = selector_prompt_profile()
        self.assertEqual(
            profile["prompt_sha256"],
            "e09a6d42af2538ede670dd502ae83f8b6b918e53695b3453ade5e551cfd30f85",
        )
        self.assertEqual(profile["prompt_profile_id"], "weekly-pose-selector-v1")
        self.assertIn("{{candidate_evidence_json}}", profile["item_prompt_template"])
        self.assertIn("independently", profile["item_prompt_template"])
        self.assertIn("Do not use a browser", profile["system_prompt"])
        self.assertIn("not hidden chain-of-thought", profile["system_prompt"])

    def test_verify_selector_kit_zip_rejects_hash_mismatch(self) -> None:
        zip_bytes, _descriptor, _kit = self._build()
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as source, zipfile.ZipFile(
            buffer, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name in source.namelist():
                content = source.read(name)
                if name.endswith("pose.pdb"):
                    content = b"tampered\n"
                info = source.getinfo(name)
                archive.writestr(info, content)
        with self.assertRaisesRegex(WeeklySelectorError, "hash mismatch"):
            verify_selector_kit_zip(buffer.getvalue())

    def test_manifest_includes_targets_sequences_and_ligand_representations(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        by_item = {item["item_id"]: item for item in kit["items"]}
        target_one = by_item["target-1"]["target"]
        self.assertEqual(target_one["entities"][0]["sequence"], "ACDEFGHIK")
        self.assertEqual(target_one["entities"][1]["smiles"], "CCO")
        target_two = by_item["target-2"]["target"]
        self.assertEqual(target_two["entities"][1]["ccd_codes"], ["ATP"])
        for item in kit["items"]:
            for choice in item["choices"]:
                self.assertIn("cluster_id", choice)
                self.assertIn("is_rep", choice)
                self.assertIn("descriptors", choice)
                self.assertIn("assets", choice)
                for kind in ("pose", "protein", "pocket"):
                    asset = choice["assets"][kind]
                    self.assertRegex(asset["sha256"], r"^[0-9a-f]{64}$")
                    self.assertGreater(asset["size_bytes"], 0)

    def test_rejects_forbidden_leak_keys_in_targets_and_submissions(self) -> None:
        leaked_target = deepcopy(self.targets["target-1"])
        leaked_target["metadata"] = {"run_id": "secret"}
        with self.assertRaisesRegex(WeeklySelectorError, "forbidden keys"):
            build_selector_kit(
                round_id=self.round_id,
                blind_manifest=self.blind,
                targets_by_item_id={**self.targets, "target-1": leaked_target},
                assets_by_choice=self.assets,
            )

        _zip_bytes, _descriptor, kit = self._build()
        leaked_submission = submission_for(
            kit,
            [
                {
                    **none_item("target-1"),
                    "rmsd": 1.2,
                },
                none_item("target-2"),
            ],
        )
        with self.assertRaisesRegex(WeeklySelectorError, "forbidden"):
            validate_selector_submission(leaked_submission, kit)

        with self.assertRaisesRegex(WeeklySelectorError, "forbidden"):
            assert_no_forbidden_content({"payload": [[1.0, 2.0, 3.0]]})

    def test_validate_complete_submission_with_independent_none_modes(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        item_one = kit["items"][0]
        item_two = kit["items"][1]
        choice_id = item_one["choices"][0]["choice_id"]
        cluster_id = item_two["choices"][0]["cluster_id"]
        submission = submission_for(
            kit,
            [
                {
                    "item_id": item_one["item_id"],
                    "clustered": {"selection_kind": "none"},
                    "unclustered": {
                        "selection_kind": "exact",
                        "choice_id": choice_id,
                    },
                },
                {
                    "item_id": item_two["item_id"],
                    "clustered": {
                        "selection_kind": "cluster",
                        "cluster_id": cluster_id,
                    },
                    "unclustered": {"selection_kind": "none"},
                },
            ],
        )
        normalized = validate_selector_submission(submission, kit)
        self.assertEqual(normalized["items"][0]["unclustered"]["choice_id"], choice_id)
        self.assertEqual(normalized["items"][0]["clustered"], {"selection_kind": "none"})
        self.assertEqual(normalized["items"][1]["unclustered"], {"selection_kind": "none"})
        self.assertEqual(normalized["items"][1]["clustered"]["cluster_id"], cluster_id)
        self.assertEqual(normalized["submission_id"], submission["submission_id"].lower())

    def test_rejects_incomplete_or_extra_items(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        incomplete = submission_for(kit, [none_item(kit["items"][0]["item_id"])])
        with self.assertRaisesRegex(WeeklySelectorError, "missing"):
            validate_selector_submission(incomplete, kit)

        extra = submission_for(
            kit,
            [none_item(item["item_id"]) for item in kit["items"]]
            + [none_item("unknown-item")],
        )
        with self.assertRaisesRegex(WeeklySelectorError, "unknown item_id"):
            validate_selector_submission(extra, kit)

        duplicate = submission_for(
            kit,
            [
                none_item(kit["items"][0]["item_id"]),
                none_item(kit["items"][0]["item_id"]),
            ],
        )
        with self.assertRaisesRegex(WeeklySelectorError, "duplicate"):
            validate_selector_submission(duplicate, kit)

    def test_rejects_wrong_choice_cluster_and_round_bindings(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        item = kit["items"][0]
        choice_id = item["choices"][0]["choice_id"]
        other_cluster = item["choices"][1]["cluster_id"]
        base = submission_for(
            kit, [none_item(row["item_id"]) for row in kit["items"]]
        )
        wrong_choice = deepcopy(base)
        wrong_choice["items"][0]["unclustered"] = {
            "selection_kind": "exact",
            "choice_id": "choice_not_in_kit",
        }
        with self.assertRaisesRegex(WeeklySelectorError, "not valid for item"):
            validate_selector_submission(wrong_choice, kit)

        wrong_cluster = deepcopy(base)
        wrong_cluster["items"][0]["clustered"] = {
            "selection_kind": "cluster",
            "cluster_id": "cluster-not-in-kit",
        }
        with self.assertRaisesRegex(WeeklySelectorError, "not valid for item"):
            validate_selector_submission(wrong_cluster, kit)

        mismatched = deepcopy(base)
        mismatched["items"][0]["unclustered"] = {
            "selection_kind": "exact",
            "choice_id": choice_id,
        }
        mismatched["items"][0]["clustered"] = {
            "selection_kind": "cluster",
            "cluster_id": other_cluster,
        }
        normalized = validate_selector_submission(mismatched, kit)
        self.assertEqual(normalized["items"][0]["unclustered"]["choice_id"], choice_id)
        self.assertEqual(normalized["items"][0]["clustered"]["cluster_id"], other_cluster)

        wrong_round = deepcopy(base)
        wrong_round["round_id"] = "weekly-other"
        with self.assertRaisesRegex(WeeklySelectorError, "round_id"):
            validate_selector_submission(wrong_round, kit)

        wrong_digest = deepcopy(base)
        wrong_digest["kit_sha256"] = "0" * 64
        with self.assertRaisesRegex(WeeklySelectorError, "kit_sha256"):
            validate_selector_submission(wrong_digest, kit)

        wrong_environment = deepcopy(base)
        wrong_environment["environment"] = "production"
        with self.assertRaisesRegex(WeeklySelectorError, "environment"):
            validate_selector_submission(wrong_environment, kit)

        wrong_blind = deepcopy(base)
        wrong_blind["blind_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(WeeklySelectorError, "blind_manifest_sha256"):
            validate_selector_submission(wrong_blind, kit)

        cross_item = deepcopy(base)
        cross_item["items"][0]["unclustered"] = {
            "selection_kind": "exact",
            "choice_id": kit["items"][1]["choices"][0]["choice_id"],
        }
        with self.assertRaisesRegex(WeeklySelectorError, "not valid for item"):
            validate_selector_submission(cross_item, kit)

    def test_rejects_unknown_fields_omissions_and_malformed_none(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        complete = [none_item(item["item_id"]) for item in kit["items"]]
        with self.assertRaisesRegex(WeeklySelectorError, "unknown keys"):
            validate_selector_submission(
                submission_for(kit, complete, notes="extra"),
                kit,
            )
        with self.assertRaisesRegex(WeeklySelectorError, "method"):
            validate_selector_submission(
                submission_for(
                    kit,
                    complete,
                    method={"name": "demo", "version": "1.0"},
                ),
                kit,
            )
        missing_mode = deepcopy(complete)
        del missing_mode[0]["unclustered"]
        with self.assertRaisesRegex(WeeklySelectorError, "must include unclustered"):
            validate_selector_submission(submission_for(kit, missing_mode), kit)

        malformed_none = deepcopy(complete)
        malformed_none[0]["clustered"]["cluster_id"] = "cluster-a"
        with self.assertRaisesRegex(WeeklySelectorError, "unknown keys"):
            validate_selector_submission(submission_for(kit, malformed_none), kit)

        representative_inference = deepcopy(complete)
        representative_inference[0]["clustered"] = {
            "selection_kind": "cluster",
            "choice_id": kit["items"][0]["choices"][0]["choice_id"],
        }
        with self.assertRaisesRegex(WeeklySelectorError, "unknown keys"):
            validate_selector_submission(
                submission_for(kit, representative_inference), kit
            )

        nullable = deepcopy(complete)
        nullable[0]["unclustered"] = None
        with self.assertRaisesRegex(WeeklySelectorError, "must be an object"):
            validate_selector_submission(submission_for(kit, nullable), kit)

        with self.assertRaisesRegex(WeeklySelectorError, "UUID"):
            validate_selector_submission(
                submission_for(kit, complete, submission_id="not-a-uuid"),
                kit,
            )

    def test_build_submission_template_and_builder(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        template = build_submission_template(kit)
        self.assertEqual(
            template,
            [
                none_item("target-1"),
                none_item("target-2"),
            ],
        )
        submission = build_selector_submission(
            kit,
            submission_id="00000000-0000-4000-8000-000000000001",
            items=list(reversed(template)),
        )
        self.assertEqual(submission["schema_version"], SUBMISSION_SCHEMA_VERSION)
        self.assertEqual(
            [item["item_id"] for item in submission["items"]],
            ["target-1", "target-2"],
        )
        self.assertEqual(len(submission["items"]), 2)
        digest = digest_selector_submission(submission)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

        noncanonical_order = submission_for(
            kit,
            list(reversed(template)),
            submission_id="00000000-0000-4000-8000-000000000001",
        )
        with self.assertRaisesRegex(WeeklySelectorError, "canonical item order"):
            validate_selector_submission(noncanonical_order, kit)

        uppercase_uuid = submission_for(
            kit,
            template,
            submission_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        )
        with self.assertRaisesRegex(WeeklySelectorError, "canonical lowercase UUID"):
            validate_selector_submission(uppercase_uuid, kit)

    def test_payload_size_limit(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        items = [
            {
                "item_id": item["item_id"],
                "clustered": {
                    "selection_kind": "cluster",
                    "cluster_id": item["choices"][0]["cluster_id"],
                },
                "unclustered": {
                    "selection_kind": "exact",
                    "choice_id": item["choices"][0]["choice_id"],
                },
            }
            for item in kit["items"]
        ]
        with mock.patch(
            "foldarium_pipeline.weekly_selector.MAX_SUBMISSION_PAYLOAD_BYTES",
            128,
        ):
            with self.assertRaisesRegex(WeeklySelectorError, "128"):
                build_selector_submission(
                    kit,
                    submission_id=str(uuid.uuid4()),
                    items=items,
                )

    def test_rejects_nonfinite_kit_metadata(self) -> None:
        with self.assertRaisesRegex(WeeklySelectorError, "non-finite|canonical JSON"):
            build_selector_kit(
                round_id=self.round_id,
                environment="preview",
                blind_manifest=self.blind,
                targets_by_item_id=self.targets,
                assets_by_choice=self.assets,
                policies={"temperature": float("nan")},
            )

    def test_submit_selector_submission_posts_bearer_token(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        submission = build_selector_submission(
            kit,
            submission_id="00000000-0000-4000-8000-000000000099",
            items=build_submission_template(kit),
        )
        response_body = json.dumps({"submission_id": submission["submission_id"]}).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return response_body

        captured: dict[str, Any] = {}

        def fake_urlopen(request, timeout=60):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            receipt = submit_selector_submission(
                "https://foldarium.test/api/weekly-selector/submissions",
                "selector-bearer-token",
                submission,
            )
        self.assertEqual(receipt["submission_id"], submission["submission_id"])
        self.assertEqual(
            captured["headers"]["Authorization"],
            "Bearer selector-bearer-token",
        )
        self.assertEqual(json.loads(captured["body"].decode("utf-8")), submission)

    def test_validate_blind_manifest_requires_cluster_metadata(self) -> None:
        validated = validate_blind_manifest(self.blind, round_id=self.round_id)
        self.assertEqual(validated["round_id"], self.round_id)
        self.assertEqual(len(validated["items"]), 2)
        bad = deepcopy(self.blind)
        del bad["items"][0]["choices"][0]["cluster_id"]
        with self.assertRaisesRegex(WeeklySelectorError, "cluster_id"):
            validate_blind_manifest(bad, round_id=self.round_id)

    def test_wasm_parity_fixture(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "weekly-selector-wasm"
            / "fixtures"
            / "parity.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        kit = {
            **fixture["manifest"],
            "blind_manifest_sha256": "b" * 64,
            "policies": {},
            "files": [],
            "items": [
                {
                    "item_id": item["item_id"],
                    "target": target_for("target-1"),
                    "choices": [
                        {
                            "choice_id": choice["choice_id"],
                            "cluster_id": choice["cluster_id"],
                            "is_rep": True,
                            "descriptors": {
                                "pose_uri": "x",
                                "protein_uri": "x",
                                "pocket_uri": "x",
                            },
                            "assets": {
                                kind: {
                                    "path": f"items/{item['item_id']}/choices/{choice['choice_id']}/{kind}.pdb",
                                    "sha256": "c" * 64,
                                    "size_bytes": 1,
                                    "media_type": "chemical/x-pdb",
                                }
                                for kind in ("pose", "protein", "pocket")
                            },
                        }
                        for choice in item["choices"]
                    ],
                }
                for item in fixture["manifest"]["items"]
            ],
        }
        normalized = validate_selector_submission(fixture["submission"], kit)
        self.assertEqual(normalized["items"], fixture["submission"]["items"])
        self.assertEqual(
            digest_selector_submission(normalized),
            fixture["expected_digest"],
        )

        generated_client: dict[str, Any] = {"__name__": "generated_selector_client"}
        exec(
            compile(CLIENT_TEMPLATE, "foldarium_selector_client.py", "exec"),
            generated_client,
        )
        client_normalized = generated_client["validate_submission"](
            fixture["submission"],
            fixture["manifest"],
        )
        self.assertEqual(client_normalized, normalized)
        self.assertEqual(
            generated_client["digest_submission"](client_normalized),
            fixture["expected_digest"],
        )

    def test_js_contract_parity_fixture(self) -> None:
        _zip_bytes, _descriptor, kit = self._build()
        item_a = next(item for item in kit["items"] if item["item_id"] == "target-1")
        submission = validate_selector_submission(
            submission_for(
                kit,
                [
                    {
                        "item_id": "target-1",
                        "clustered": {
                            "selection_kind": "cluster",
                            "cluster_id": item_a["choices"][0]["cluster_id"],
                        },
                        "unclustered": {
                            "selection_kind": "exact",
                            "choice_id": item_a["choices"][0]["choice_id"],
                        },
                    },
                    none_item("target-2"),
                ],
                submission_id="00000000-0000-4000-8000-000000000001",
            ),
            kit,
        )
        self.assertEqual(submission["submission_id"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(len(submission["items"]), 2)
        self.assertEqual(
            digest_selector_submission(submission),
            digest_selector_submission(submission),
        )


if __name__ == "__main__":
    unittest.main()
