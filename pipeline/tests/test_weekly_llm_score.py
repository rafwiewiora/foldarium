"""Tests for the audited weekly LLM scoring runner."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from foldarium_pipeline.contracts import SCHEMA_VERSION
from foldarium_pipeline.quiz import build_blind_manifest
from foldarium_pipeline.weekly_llm_contract import (
    BENCHMARK_SCHEMA_VERSION,
    EMPTY_NETWORK_ALLOWLIST_SHA256,
    digest_post_close_benchmark,
    sanitize_public_benchmark,
    validate_post_close_benchmark,
    validate_blindness_attestation,
)
from foldarium_pipeline.weekly_llm_evidence import (
    build_choice_evidence,
    parse_pdb_atoms,
    render_orthographic_png,
    write_png_rgb,
)
from foldarium_pipeline.weekly_llm_kit import WeeklyLlmKitError, extract_verified_kit
from foldarium_pipeline.weekly_llm_providers.claude import (
    build_claude_command,
    parse_claude_json_output,
    preflight_claude_auth,
)
from foldarium_pipeline.weekly_llm_providers.cursor import resolve_sol_high_model
from foldarium_pipeline.weekly_llm_providers.fake import FakeProvider
from foldarium_pipeline.weekly_llm_response import (
    WeeklyLlmResponseError,
    validate_model_response,
)
from foldarium_pipeline.weekly_llm_runner import (
    RunnerOptions,
    render_item_prompt,
    run_weekly_llm_score,
    submit_benchmark_execution,
)
from foldarium_pipeline.weekly_selector import build_selector_kit, verify_selector_kit_zip
from foldarium_pipeline.weekly_selector_prompt import SELECTOR_PROMPT_SHA256


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


def target_for(item_id: str) -> dict:
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
    return (
        f"ATOM      1  C   LIG L   1       1.000   2.000   3.000  1.00  0.00           C  \n"
        f"# {label}\n"
    ).encode("utf-8")


def build_fixture_assets(blind: dict) -> dict[tuple[str, str], dict[str, bytes]]:
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

try:
    from cursor_sdk.types import ModelParameterDefinition, ModelParameterDefinitionValue, ModelVariant, SDKModel
except ImportError:  # pragma: no cover
    SDKModel = object  # type: ignore[assignment,misc]


EXECUTION_ID = "00000000-0000-4000-8000-000000000123"
BLIND_SHA = "a" * 64
KIT_SHA = "b" * 64


def _build_kit_zip() -> tuple[bytes, dict]:
    round_id = "weekly-test"
    blind, _private = build_blind_manifest(round_id, source_items())
    targets = {item_id: target_for(item_id) for item_id in ("target-1", "target-2")}
    assets = build_fixture_assets(blind)
    zip_bytes, _descriptor = build_selector_kit(
        round_id=round_id,
        environment="preview",
        blind_manifest=blind,
        targets_by_item_id=targets,
        assets_by_choice=assets,
    )
    kit = verify_selector_kit_zip(zip_bytes)
    return zip_bytes, kit


def _fake_fixture(kit: dict) -> dict:
    items: dict[str, dict] = {}
    for item in kit["items"]:
        item_id = item["item_id"]
        cluster_pick = sorted({choice["cluster_id"] for choice in item["choices"]})[0]
        choice_pick = sorted(item["choices"], key=lambda row: row["choice_id"])[0]["choice_id"]
        if item_id == "target-2":
            clustered = {"selection_kind": "none", "confidence": 0.4, "evidence": "All clusters look implausible."}
            unclustered = {"selection_kind": "none", "confidence": 0.35, "evidence": "Every pose appears buried."}
        else:
            clustered = {
                "selection_kind": "cluster",
                "cluster_id": cluster_pick,
                "confidence": 0.7,
                "evidence": "Representative geometry looks least strained.",
            }
            unclustered = {
                "selection_kind": "exact",
                "choice_id": choice_pick,
                "confidence": 0.65,
                "evidence": "Pose contacts look most consistent.",
            }
        items[item_id] = {
            "requested_id": "fake-model",
            "observed_ids": ["fake-model-stable"],
            "requested_effort": "default",
            "applied_effort": None,
            "effort_reporting": "not_exposed",
            "response": {
                "schema_version": "foldarium.selector-model-response/v1",
                "item_id": item_id,
                "clustered": clustered,
                "unclustered": unclustered,
            },
        }
    return {
        "engine_name": "fake-provider",
        "engine_version": "fake-1.0.0",
        "items": items,
    }


class WeeklyLlmEvidenceTests(unittest.TestCase):
    def test_deterministic_png_and_metrics(self) -> None:
        pose = asset_bytes("pose")
        protein = asset_bytes("protein")
        pocket = asset_bytes("pocket")
        first, first_images = build_choice_evidence(
            choice_id="choice-a",
            cluster_id="cluster-a",
            is_rep=True,
            descriptors={
                "pose_uri": "uri://pose",
                "protein_uri": "uri://protein",
                "pocket_uri": "uri://pocket",
            },
            pose_bytes=pose,
            protein_bytes=protein,
            pocket_bytes=pocket,
        )
        second, second_images = build_choice_evidence(
            choice_id="choice-a",
            cluster_id="cluster-a",
            is_rep=True,
            descriptors={
                "pose_uri": "uri://pose",
                "protein_uri": "uri://protein",
                "pocket_uri": "uri://pocket",
            },
            pose_bytes=pose,
            protein_bytes=protein,
            pocket_bytes=pocket,
        )
        self.assertEqual(first, second)
        self.assertEqual(first_images, second_images)
        png = write_png_rgb(width=4, height=4, pixels=[(1, 2, 3)] * 16)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_rejects_non_finite_coordinates(self) -> None:
        bad = b"ATOM      1  C   LIG L   1       nan     2.000   3.000  1.00  0.00           C  \n"
        with self.assertRaisesRegex(Exception, "valid finite-coordinate"):
            parse_pdb_atoms(bad, label="pose")

    def test_render_orthographic_is_stable(self) -> None:
        atoms = parse_pdb_atoms(asset_bytes("pose"), label="pose")
        first = render_orthographic_png(receptor_atoms=atoms, ligand_atoms=atoms)
        second = render_orthographic_png(receptor_atoms=atoms, ligand_atoms=atoms)
        self.assertEqual(first, second)


class WeeklyLlmKitTests(unittest.TestCase):
    def test_rejects_zip_slip_paths(self) -> None:
        buffer = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        buffer.close()
        with zipfile.ZipFile(buffer.name, "w") as archive:
            archive.writestr("../evil.txt", "nope")
        with self.assertRaises(WeeklyLlmKitError):
            extract_verified_kit(
                Path(buffer.name).read_bytes(),
                output_dir=Path(tempfile.mkdtemp()),
            )


class WeeklyLlmResponseTests(unittest.TestCase):
    def test_rejects_unknown_choice(self) -> None:
        with self.assertRaises(WeeklyLlmResponseError):
            validate_model_response(
                {
                    "schema_version": "foldarium.selector-model-response/v1",
                    "item_id": "target-1",
                    "clustered": {
                        "selection_kind": "cluster",
                        "cluster_id": "missing",
                        "confidence": 0.5,
                        "evidence": "x",
                    },
                    "unclustered": {
                        "selection_kind": "none",
                        "confidence": 0.5,
                        "evidence": "x",
                    },
                },
                item_id="target-1",
                allowed_cluster_ids={"cluster-a"},
                allowed_choice_ids={"sample-1"},
            )


class WeeklyLlmClaudeTests(unittest.TestCase):
    def test_command_omits_effort_for_default(self) -> None:
        command = build_claude_command(
            prompt_text="prompt",
            workspace_dir="/tmp/workspace",
            mcp_config_path="/tmp/workspace/.empty-mcp-config.json",
        )
        self.assertNotIn("--effort", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--tools", command)
        tools_index = command.index("--tools")
        self.assertEqual(command[tools_index + 1], "")

    def test_parse_auth_status_without_persisting_identifiers(self) -> None:
        payload = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "team",
            "email": "secret@example.com",
            "orgId": "org",
        }
        with mock.patch(
            "foldarium_pipeline.weekly_llm_providers.claude.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=json.dumps(payload), stderr=""),
        ):
            status = preflight_claude_auth()
        self.assertEqual(status["subscription_type"], "team")
        self.assertNotIn("email", status)
        self.assertNotIn("orgId", status)

    def test_parse_claude_json_default_effort_not_exposed(self) -> None:
        response, usage, observed, session_id, applied = parse_claude_json_output(
            {
                "result": json.dumps(
                    {
                        "schema_version": "foldarium.selector-model-response/v1",
                        "item_id": "target-1",
                        "clustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                        "unclustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                    }
                ),
                "session_id": "session-1",
                "modelUsage": {"claude-opus-4-1-20260805": {"inputTokens": 10, "outputTokens": 5}},
                "duration_ms": 100,
            }
        )
        self.assertEqual(observed, ("claude-opus-4-1-20260805",))
        self.assertIsNone(applied)
        self.assertEqual(session_id, "session-1")
        self.assertEqual(usage.input_tokens, 10)


class WeeklyLlmCursorTests(unittest.TestCase):
    def test_resolve_sol_high_mode_from_catalog(self) -> None:
        model = SDKModel(
            id="gpt-5.6-sol-2026-08-20",
            display_name="GPT-5.6 Sol",
            description="",
            parameters=[
                ModelParameterDefinition(
                    id="reasoning",
                    display_name="Reasoning effort",
                    values=[
                        ModelParameterDefinitionValue(value="low", display_name="Low"),
                        ModelParameterDefinitionValue(value="high", display_name="High"),
                    ],
                )
            ],
            variants=[],
        )
        model_id, params = resolve_sol_high_model([model])
        self.assertEqual(model_id, "gpt-5.6-sol-2026-08-20")
        self.assertEqual(params[0].value, "high")

    def test_aborts_when_sol_model_ambiguous(self) -> None:
        models = [
            SDKModel(
                id="gpt-5.6-sol-a",
                display_name="GPT-5.6 Sol A",
                description="",
                parameters=[],
                variants=[],
            ),
            SDKModel(
                id="gpt-5.6-sol-b",
                display_name="GPT-5.6 Sol B",
                description="",
                parameters=[],
                variants=[],
            ),
        ]
        with self.assertRaisesRegex(RuntimeError, "exact accessible"):
            resolve_sol_high_model(models)


class WeeklyLlmBenchmarkContractTests(unittest.TestCase):
    def test_cross_language_fixture_normalizes(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        attestation = validate_blindness_attestation(
            {
                "schema_version": "foldarium.selector-blindness-attestation/v1",
                "workspace_policy": "verified-kit-only",
                "network_policy": "none",
                "network_allowlist_sha256": EMPTY_NETWORK_ALLOWLIST_SHA256,
                "browser_enabled": False,
                "web_search_enabled": False,
                "external_retrieval_enabled": False,
                "shared_cache_enabled": False,
            }
        )
        payload_items = []
        for item in kit["items"]:
            choice = sorted(item["choices"], key=lambda row: row["choice_id"])[0]
            payload_items.append(
                {
                    "item_id": item["item_id"],
                    "clustered": {"selection_kind": "cluster", "cluster_id": choice["cluster_id"]},
                    "unclustered": {"selection_kind": "exact", "choice_id": choice["choice_id"]},
                }
            )
        execution = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "execution_id": EXECUTION_ID,
            "supersedes_execution_id": None,
            "run_class": "post_close_benchmark",
            "environment": kit["environment"],
            "round_id": kit["round_id"],
            "blind_manifest_sha256": kit["blind_manifest_sha256"],
            "kit_sha256": kit["kit_sha256"],
            "display_name": "Claude Opus",
            "method_name": "blind-pose-selector",
            "method_version": "weekly-pose-selector-v1",
            "provider": "anthropic",
            "engine": {
                "name": "claude-cli",
                "version": "1.2.3",
                "run_id": None,
                "session_id": "session-1",
            },
            "model": {
                "requested_id": "opus",
                "observed_ids": ["claude-opus-4-1-20260805"],
                "requested_effort": "default",
                "applied_effort": None,
                "effort_reporting": "not_exposed",
            },
            "provenance": {
                "prompt_profile_id": "weekly-pose-selector-v1",
                "prompt_sha256": SELECTOR_PROMPT_SHA256,
                "input_manifest_sha256": "c" * 64,
                "tools_sha256": "d" * 64,
                "config_sha256": "e" * 64,
                "runtime_sha256": "f" * 64,
            },
            "blindness_attestation": attestation,
            "blindness_attestation_sha256": "0" * 64,
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 80,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "reasoning_tokens": None,
                "cost_usd": 0,
                "duration_ms": 9000,
            },
            "started_at": "2026-08-26T12:00:00.000Z",
            "finished_at": "2026-08-26T12:00:09.000Z",
            "reasoning_trace_retained": False,
            "output_sha256": "9" * 64,
            "payload": {
                "schema_version": "foldarium.selector-submission/v2",
                "submission_id": EXECUTION_ID,
                "environment": kit["environment"],
                "round_id": kit["round_id"],
                "blind_manifest_sha256": kit["blind_manifest_sha256"],
                "kit_sha256": kit["kit_sha256"],
                "items": payload_items,
            },
        }
        execution["blindness_attestation_sha256"] = __import__(
            "foldarium_pipeline.weekly_llm_contract", fromlist=["sha256_hex"]
        ).sha256_hex(attestation)
        normalized = validate_post_close_benchmark(execution, kit=kit)
        self.assertEqual(normalized["payload"]["submission_id"], EXECUTION_ID)
        self.assertRegex(digest_post_close_benchmark(normalized, kit=kit), r"^[0-9a-f]{64}$")
        del zip_bytes


class WeeklyLlmRunnerTests(unittest.TestCase):
    def test_fake_provider_end_to_end(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            output_dir = Path(tmp) / "out"
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(_fake_fixture(kit)), encoding="utf-8")
            result = run_weekly_llm_score(
                RunnerOptions(
                    kit_path=kit_path,
                    output_dir=output_dir,
                    provider=FakeProvider(fixture_path=fixture_path),
                    display_name="Fake Provider",
                    provider_name="fake",
                    execution_id=EXECUTION_ID,
                )
            )
            self.assertEqual(result.execution["execution_id"], EXECUTION_ID)
            self.assertEqual(result.execution["payload"]["submission_id"], EXECUTION_ID)
            self.assertTrue(result.benchmark_path.is_file())
            public = json.loads((output_dir / "benchmark.public.json").read_text(encoding="utf-8"))
            serialized = json.dumps(public)
            self.assertNotIn("session", serialized)
            self.assertNotIn("reasoning_tokens", serialized)
            self.assertNotIn('"raw"', serialized)
            private_mode = stat.S_IMODE(os.stat(result.private_dir).st_mode)
            self.assertEqual(private_mode, 0o700)
            del kit

    def test_aborts_on_mixed_models(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        fixture = _fake_fixture(kit)
        fixture["items"]["target-2"]["observed_ids"] = ["other-model"]
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "exactly one model"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=Path(tmp) / "out",
                        provider=FakeProvider(fixture_path=fixture_path),
                        display_name="Fake Provider",
                        provider_name="fake",
                    )
                )

    def test_submit_is_byte_idempotent(self) -> None:
        body_holder: dict[str, bytes] = {}

        def fake_urlopen(request, timeout=60):  # noqa: ANN001
            body_holder["body"] = request.data
            return mock.Mock(
                __enter__=lambda self: self,
                __exit__=lambda *args: None,
                read=lambda: json.dumps({"idempotent": False}).encode("utf-8"),
            )

        execution = {"schema_version": BENCHMARK_SCHEMA_VERSION, "execution_id": str(uuid.uuid4())}
        with mock.patch("foldarium_pipeline.weekly_llm_runner.urlopen", side_effect=fake_urlopen):
            submit_benchmark_execution("https://example.test/benchmarks", "token", execution)
            first = body_holder["body"]
            submit_benchmark_execution("https://example.test/benchmarks", "token", execution)
            second = body_holder["body"]
        self.assertEqual(first, second)

    def test_render_item_prompt_uses_canonical_template(self) -> None:
        prompt = render_item_prompt(item_id="target-1", candidate_evidence=[{"choice_id": "sample-1"}])
        self.assertIn("target-1", prompt)
        self.assertIn("sample-1", prompt)


if __name__ == "__main__":
    unittest.main()
