"""Tests for the audited weekly LLM scoring runner."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from foldarium_pipeline.contracts import SCHEMA_VERSION
from foldarium_pipeline.quiz import build_blind_manifest
from foldarium_pipeline.weekly_llm_blindness import (
    build_provider_blindness_attestation,
    load_network_allowlist,
    network_allowlist_digest,
)
from foldarium_pipeline.weekly_llm_catalog import (
    CatalogModel,
    CatalogParameterDefinition,
    CatalogParameterSelection,
    CatalogParameterValue,
    CatalogVariant,
    resolve_sol_high_model,
)
from foldarium_pipeline.weekly_llm_contract import (
    BENCHMARK_SCHEMA_VERSION,
    EMPTY_NETWORK_ALLOWLIST_SHA256,
    WeeklyLlmContractError,
    digest_post_close_benchmark,
    sha256_hex,
    validate_post_close_benchmark,
    validate_blindness_attestation,
)
from foldarium_pipeline.weekly_llm_evidence import (
    MAX_CONTACT_ENTRIES,
    WeeklyLlmEvidenceError,
    build_choice_evidence,
    compute_geometry_metrics,
    parse_pdb_atoms,
    render_shared_frame_panels,
)
from foldarium_pipeline.weekly_llm_kit import WeeklyLlmKitError, build_item_workspace, extract_verified_kit
from foldarium_pipeline.weekly_llm_provenance import build_output_manifest, digest_manifest
from foldarium_pipeline.weekly_llm_providers.claude import (
    ClaudeParseResult,
    build_claude_command,
    parse_claude_json_output,
    preflight_claude_auth,
)
from foldarium_pipeline.weekly_llm_providers.cursor import (
    _extract_billed_cost,
    build_cursor_user_message,
    serialize_sdk_value,
)
from foldarium_pipeline.weekly_llm_providers.fake import FakeProvider
from foldarium_pipeline.weekly_llm_response import WeeklyLlmResponseError, validate_model_response
from foldarium_pipeline.weekly_llm_runner import (
    RunnerOptions,
    WeeklyLlmRunnerError,
    render_item_prompt,
    run_weekly_llm_score,
    submit_benchmark_execution,
)
from foldarium_pipeline.weekly_selector import build_selector_kit, canonical_json, verify_selector_kit_zip
from foldarium_pipeline.weekly_selector_prompt import SELECTOR_PROMPT_SHA256

EXECUTION_ID = "00000000-0000-4000-8000-000000000123"


def pdb_line(
    *,
    serial: int,
    x: float,
    y: float,
    z: float,
    res: str = "LIG",
    chain: str = "A",
    res_seq: int | None = None,
    atom_name: str = "C",
    element: str = "C",
    b_factor: float = 0.0,
    icode: str = " ",
) -> bytes:
    res_seq = serial if res_seq is None else res_seq
    return (
        f"ATOM  {serial:5d} {atom_name:>4s}{res:>3s} {chain}{res_seq:4d}{icode[0:1]}"
        f"   {x:8.3f}{y:8.3f}{z:8.3f}  1.00{b_factor:6.2f}          {element:>2s}\n"
    ).encode("utf-8")


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


def asset_bytes(label: str, *, offset: float = 0.0) -> bytes:
    return pdb_line(serial=1, x=1.0 + offset, y=2.0 + offset, z=3.0 + offset) + f"# {label}\n".encode("utf-8")


def build_fixture_assets(blind: dict) -> dict[tuple[str, str], dict[str, bytes]]:
    assets: dict[tuple[str, str], dict[str, bytes]] = {}
    for item in blind["items"]:
        item_id = item["id"]
        for choice in item["choices"]:
            choice_id = choice["id"]
            assets[(item_id, choice_id)] = {
                "pose": asset_bytes(f"{item_id}:{choice_id}:pose"),
                "protein": asset_bytes(f"{item_id}:{choice_id}:protein", offset=10.0),
                "pocket": asset_bytes(f"{item_id}:{choice_id}:pocket", offset=5.0),
            }
    return assets


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
            "run_id": f"run-{item_id}",
            "session_id": f"session-{item_id}",
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


@dataclass
class FakeModelParam:
    id: str
    value: str


@dataclass
class FakeRunModel:
    id: str
    params: list[FakeModelParam]


@dataclass
class FakeTokenUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int | None = 2


@dataclass
class FakeRunResult:
    id: str
    agent_id: str
    status: str
    result: str
    model: FakeRunModel
    duration_ms: int = 100
    usage: FakeTokenUsage | None = None


class WeeklyLlmEvidenceTests(unittest.TestCase):
    def test_pdb_parsing_uses_serial_residue_and_element_columns(self) -> None:
        content = (
            pdb_line(serial=42, res_seq=7, x=1.0, y=2.0, z=3.0, atom_name="CL", element="CL")
            + pdb_line(serial=99, res_seq=7, x=1.5, y=2.0, z=3.0, atom_name="1H", element="H")
            + pdb_line(serial=100, res_seq=8, x=2.0, y=2.0, z=3.0, atom_name="NA", element="NA")
        )
        atoms = parse_pdb_atoms(content, label="pose")
        self.assertEqual(len(atoms), 2)
        self.assertEqual(atoms[0].serial, 42)
        self.assertEqual(atoms[0].res_seq, 7)
        self.assertEqual(atoms[0].element, "CL")
        self.assertEqual(atoms[1].serial, 100)
        self.assertEqual(atoms[1].element, "NA")

    def test_pdb_parsing_accepts_a_separate_bounded_full_protein_limit(self) -> None:
        content = (
            pdb_line(serial=1, res_seq=1, x=1.0, y=2.0, z=3.0)
            + pdb_line(serial=2, res_seq=2, x=2.0, y=3.0, z=4.0)
        )
        with self.assertRaisesRegex(WeeklyLlmEvidenceError, "exceeds 1 heavy atoms"):
            parse_pdb_atoms(content, label="pocket", max_atoms=1)
        self.assertEqual(
            len(parse_pdb_atoms(content, label="protein", max_atoms=2)),
            2,
        )

    def test_shared_frame_changes_with_relative_translation(self) -> None:
        pose = pdb_line(serial=1, x=0.0, y=0.0, z=0.0)
        near_pocket = pdb_line(serial=1, x=2.0, y=0.0, z=0.0, res="POK")
        far_pocket = pdb_line(serial=1, x=20.0, y=0.0, z=0.0, res="POK")
        pose_atoms = parse_pdb_atoms(pose, label="pose")
        near_atoms = parse_pdb_atoms(near_pocket, label="near")
        far_atoms = parse_pdb_atoms(far_pocket, label="far")
        near_metrics = compute_geometry_metrics(
            pose_atoms=pose_atoms, protein_atoms=[], pocket_atoms=near_atoms
        )
        far_metrics = compute_geometry_metrics(
            pose_atoms=pose_atoms, protein_atoms=[], pocket_atoms=far_atoms
        )
        self.assertLess(near_metrics["centroid_distance_angstrom"], far_metrics["centroid_distance_angstrom"])
        near_png = render_shared_frame_panels(receptor_atoms=near_atoms, ligand_atoms=pose_atoms)
        far_png = render_shared_frame_panels(receptor_atoms=far_atoms, ligand_atoms=pose_atoms)
        self.assertNotEqual(near_png, far_png)

    def test_attachment_mapping_is_one_sheet_per_choice(self) -> None:
        pose = pdb_line(serial=1, x=1.0, y=2.0, z=3.0)
        pocket = pdb_line(serial=2, x=4.0, y=5.0, z=6.0, res="POK", res_seq=10)
        evidence, images = build_choice_evidence(
            choice_id="choice-a",
            cluster_id="cluster-a",
            is_rep=True,
            attachment_index=3,
            descriptors={"pose_uri": "u", "protein_uri": "u", "pocket_uri": "u"},
            pose_bytes=pose,
            protein_bytes=pocket,
            pocket_bytes=pocket,
        )
        self.assertEqual(evidence["attachment_index"], 3)
        self.assertEqual([row["attachment_index"] for row in evidence["attachments"]], [3])
        self.assertEqual(set(images), {"contact_sheet.png"})
        self.assertIn("nearest_contacts", evidence)
        self.assertIn("contact_summary", evidence)
        self.assertLessEqual(len(evidence["nearest_contacts"]), MAX_CONTACT_ENTRIES)

    def test_contact_table_ordering_is_stable(self) -> None:
        pose = b"".join(
            pdb_line(serial=index, x=float(index), y=0.0, z=0.0, atom_name=f"C{index}")
            for index in range(1, 4)
        )
        pocket = b"".join(
            pdb_line(serial=100 + index, res_seq=10 + index, x=float(index) + 0.5, y=0.0, z=0.0, res="POK")
            for index in range(1, 4)
        )
        first, _ = build_choice_evidence(
            choice_id="choice-a",
            cluster_id="cluster-a",
            is_rep=True,
            attachment_index=0,
            descriptors={"pose_uri": "u", "protein_uri": "u", "pocket_uri": "u"},
            pose_bytes=pose,
            protein_bytes=pocket,
            pocket_bytes=pocket,
        )
        second, _ = build_choice_evidence(
            choice_id="choice-a",
            cluster_id="cluster-a",
            is_rep=True,
            attachment_index=0,
            descriptors={"pose_uri": "u", "protein_uri": "u", "pocket_uri": "u"},
            pose_bytes=pose,
            protein_bytes=pocket,
            pocket_bytes=pocket,
        )
        self.assertEqual(first["nearest_contacts"], second["nearest_contacts"])

    def test_item_workspace_binds_target_once(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_dir = Path(tmp) / "kit"
            extract_verified_kit(zip_bytes, output_dir=kit_dir)
            item = sorted(kit["items"], key=lambda row: row["item_id"])[0]
            workspace = build_item_workspace(
                kit_dir=kit_dir,
                item=item,
                evidence_dir=Path(tmp) / "evidence" / item["item_id"],
            )
            self.assertIn("target", workspace["item_evidence"])
            self.assertEqual(workspace["item_evidence"]["target"]["target_id"], "cameo-target-1")
            self.assertEqual(len(workspace["item_evidence"]["candidates"]), len(item["choices"]))
            self.assertEqual(len(workspace["image_attachments"]), len(item["choices"]))

    def test_rejects_empty_pocket(self) -> None:
        pose = pdb_line(serial=1, x=1.0, y=2.0, z=3.0)
        with self.assertRaises(WeeklyLlmEvidenceError):
            build_choice_evidence(
                choice_id="choice-a",
                cluster_id="cluster-a",
                is_rep=True,
                attachment_index=0,
                descriptors={"pose_uri": "u", "protein_uri": "u", "pocket_uri": "u"},
                pose_bytes=pose,
                protein_bytes=b"",
                pocket_bytes=b"REMARK empty\n",
            )


class WeeklyLlmCatalogTests(unittest.TestCase):
    def test_resolve_sol_high_mode_from_catalog(self) -> None:
        model = CatalogModel(
            id="gpt-5.6-sol-2026-08-20",
            display_name="GPT-5.6 Sol",
            parameters=(
                CatalogParameterDefinition(
                    id="reasoning",
                    display_name="Reasoning effort",
                    values=(
                        CatalogParameterValue(value="low", display_name="Low"),
                        CatalogParameterValue(value="high", display_name="High"),
                    ),
                ),
            ),
        )
        model_id, params = resolve_sol_high_model([model])
        self.assertEqual(model_id, "gpt-5.6-sol-2026-08-20")
        self.assertEqual(params[0].value, "high")


class WeeklyLlmClaudeTests(unittest.TestCase):
    def test_command_omits_effort_and_add_dir(self) -> None:
        with mock.patch(
            "foldarium_pipeline.weekly_llm_providers.claude.shutil.which",
            return_value="/usr/local/bin/claude",
        ):
            command = build_claude_command(
                prompt_text="prompt",
                mcp_config_path="/tmp/.empty-mcp-config.json",
            )
        self.assertNotIn("--effort", command)
        self.assertNotIn("--add-dir", command)
        self.assertIn("--setting-sources", command)
        self.assertIn("", command)

    def test_parse_structured_output_and_usage(self) -> None:
        parsed = parse_claude_json_output(
            {
                "structured_output": {
                    "schema_version": "foldarium.selector-model-response/v1",
                    "item_id": "target-1",
                    "clustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                    "unclustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                },
                "run_id": "run-1",
                "session_id": "session-1",
                "modelUsage": {
                    "claude-opus-4-1-20260805": {
                        "inputTokens": 10,
                        "outputTokens": 5,
                        "cacheReadInputTokens": 2,
                        "cacheCreationInputTokens": 1,
                        "costUSD": 0.25,
                    }
                },
                "duration_ms": 100,
            }
        )
        self.assertIsInstance(parsed, ClaudeParseResult)
        self.assertEqual(parsed.run_id, "run-1")
        self.assertEqual(parsed.observed_ids, ("claude-opus-4-1-20260805",))
        self.assertEqual(parsed.usage.cache_read_tokens, 2)
        self.assertEqual(parsed.usage.cost_usd, 0.25)

    def test_parse_preserves_explicit_zero_cost_and_duration(self) -> None:
        parsed = parse_claude_json_output(
            {
                "structured_output": {
                    "schema_version": "foldarium.selector-model-response/v1",
                    "item_id": "target-1",
                    "clustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                    "unclustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                },
                "total_cost_usd": 0.0,
                "duration_ms": 0,
                "modelUsage": {
                    "claude-opus-4-1-20260805": {
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "costUSD": 0.0,
                    }
                },
            }
        )
        self.assertEqual(parsed.usage.cost_usd, 0.0)
        self.assertEqual(parsed.usage.duration_ms, 0)
        self.assertEqual(parsed.usage.input_tokens, 0)

    def test_parse_result_fallback(self) -> None:
        parsed = parse_claude_json_output(
            {
                "result": json.dumps(
                    {
                        "schema_version": "foldarium.selector-model-response/v1",
                        "item_id": "target-1",
                        "clustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                        "unclustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                    }
                ),
                "modelUsage": {"claude-opus-4-1-20260805": {"inputTokens": 10, "outputTokens": 5}},
            }
        )
        self.assertEqual(parsed.observed_ids, ("claude-opus-4-1-20260805",))


@dataclass
class FakeUsageCost:
    charged_cents: float
    raw_cost_cents: float | None = None


@dataclass
class FakeAgentUsage:
    cost: FakeUsageCost


class WeeklyLlmCursorCostTests(unittest.TestCase):
    def test_extract_billed_cost_zero_and_nonzero(self) -> None:
        zero = _extract_billed_cost(
            FakeAgentUsage(cost=FakeUsageCost(charged_cents=0.0, raw_cost_cents=0.0))
        )
        self.assertEqual(zero, (0.0, 0.0, 0.0))
        nonzero = _extract_billed_cost(
            FakeAgentUsage(cost=FakeUsageCost(charged_cents=125.5, raw_cost_cents=150.25))
        )
        self.assertEqual(nonzero, (1.255, 125.5, 150.25))


class WeeklyLlmCursorSerializationTests(unittest.TestCase):
    def test_serializes_dataclass_run_result(self) -> None:
        payload = serialize_sdk_value(
            FakeRunResult(
                id="run-1",
                agent_id="agent-1",
                status="finished",
                result="{}",
                model=FakeRunModel(id="gpt-5.6-sol", params=[FakeModelParam(id="reasoning", value="high")]),
                usage=FakeTokenUsage(),
            )
        )
        self.assertEqual(payload["model"]["params"][0]["value"], "high")

    def test_cursor_user_message_includes_canonical_system_text(self) -> None:
        message = build_cursor_user_message(item_prompt_text="Evaluate item.")
        self.assertIn("ITEM REQUEST:", message)
        self.assertIn("Evaluate item.", message)


class WeeklyLlmContractTests(unittest.TestCase):
    def test_rejects_boolean_usage(self) -> None:
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
        execution = _benchmark_execution(attestation=attestation)
        execution["usage"]["input_tokens"] = True
        with self.assertRaises(WeeklyLlmContractError):
            validate_post_close_benchmark(execution, kit=_build_kit_zip()[1])

    def test_provider_api_only_allowlist_digest(self) -> None:
        allowlist = ["api.example.test", "auth.example.test"]
        attestation = build_provider_blindness_attestation(allowlist=allowlist)
        self.assertEqual(attestation["network_policy"], "provider-api-only")
        self.assertEqual(attestation["network_allowlist_sha256"], network_allowlist_digest(allowlist))


def _benchmark_execution(*, attestation: dict) -> dict:
    zip_bytes, kit = _build_kit_zip()
    del zip_bytes
    payload_items = []
    output_items = []
    for item in kit["items"]:
        choice = sorted(item["choices"], key=lambda row: row["choice_id"])[0]
        payload_items.append(
            {
                "item_id": item["item_id"],
                "clustered": {"selection_kind": "cluster", "cluster_id": choice["cluster_id"]},
                "unclustered": {"selection_kind": "exact", "choice_id": choice["choice_id"]},
            }
        )
        output_items.append(
            {
                "item_id": item["item_id"],
                "response_sha256": "a" * 64,
                "validated_response_artifact": {
                    "path": f"private/items/{item['item_id']}/validated-response.json",
                    "sha256": "b" * 64,
                    "bytes": 128,
                },
            }
        )
    output_sha256 = digest_manifest(build_output_manifest(items=output_items))
    return {
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
        "engine": {"name": "claude-cli", "version": "1.2.3", "run_id": None, "session_id": None},
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
        "blindness_attestation_sha256": sha256_hex(attestation),
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
        "output_sha256": output_sha256,
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
            self.assertEqual(result.execution["engine"]["run_id"], None)
            self.assertEqual(result.execution["engine"]["session_id"], None)
            execution_dir = output_dir / EXECUTION_ID
            self.assertTrue(result.benchmark_path.is_relative_to(execution_dir))
            runtime = json.loads((result.private_dir / "runtime-manifest.json").read_text(encoding="utf-8"))
            item_runtime = runtime["items"][0]
            self.assertIn("prompt_artifact", item_runtime)
            self.assertIn("candidate_evidence_artifact", item_runtime)
            self.assertIn("validated_response_artifact", item_runtime)
            prompt_path = result.private_dir / "items" / "target-1" / "prompt.txt"
            evidence_path = result.private_dir / "items" / "target-1" / "candidate-evidence.json"
            validated_path = result.private_dir / "items" / "target-1" / "validated-response.json"
            self.assertTrue(prompt_path.is_file())
            self.assertTrue(evidence_path.is_file())
            self.assertTrue(validated_path.is_file())
            _assert_secure_permissions(prompt_path, 0o600)
            _assert_secure_permissions(evidence_path, 0o600)
            _assert_secure_permissions(validated_path, 0o600)
            self.assertEqual(len(runtime["items"]), 2)
            self.assertNotEqual(runtime["items"][0]["run_id"], runtime["items"][1]["run_id"])
            self.assertEqual(result.execution["output_sha256"], result.output_sha256)
            _assert_secure_permissions(result.private_dir, 0o700)

    def test_aborts_on_missing_model_for_one_item(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        fixture = _fake_fixture(kit)
        fixture["items"]["target-2"]["observed_ids"] = []
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

    def test_aborts_on_cross_item_model_mismatch(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        fixture = _fake_fixture(kit)
        fixture["items"]["target-2"]["observed_ids"] = ["other-model"]
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(WeeklyLlmRunnerError, "exactly one model identifier across items"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=Path(tmp) / "out",
                        provider=FakeProvider(fixture_path=fixture_path),
                        display_name="Fake Provider",
                        provider_name="fake",
                    )
                )

    def test_live_provider_requires_allowlist_from_provider_policy(self) -> None:
        class LiveStubProvider(FakeProvider):
            network_required = True
            network_policy = "provider-api-only"

        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(_fake_fixture(kit)), encoding="utf-8")
            with self.assertRaisesRegex(WeeklyLlmContractError, "--network-allowlist"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=Path(tmp) / "out",
                        provider=LiveStubProvider(fixture_path=fixture_path),
                        display_name="Claude Opus",
                        provider_name="anthropic",
                    )
                )

    def test_fake_provider_rejects_network_allowlist(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(_fake_fixture(kit)), encoding="utf-8")
            allowlist_path = Path(tmp) / "allowlist.json"
            allowlist_path.write_text(json.dumps(["api.example.test"]), encoding="utf-8")
            with self.assertRaisesRegex(WeeklyLlmRunnerError, "only valid for live providers"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=Path(tmp) / "out",
                        provider=FakeProvider(fixture_path=fixture_path),
                        display_name="Fake Provider",
                        provider_name="fake",
                        network_allowlist_path=allowlist_path,
                        egress_enforcement_asserted=True,
                    )
                )

    def test_repeated_execution_id_fails_on_nonempty_child_dir(self) -> None:
        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(_fake_fixture(kit)), encoding="utf-8")
            output_dir = Path(tmp) / "out"
            run_weekly_llm_score(
                RunnerOptions(
                    kit_path=kit_path,
                    output_dir=output_dir,
                    provider=FakeProvider(fixture_path=fixture_path),
                    display_name="Fake Provider",
                    provider_name="fake",
                    execution_id=EXECUTION_ID,
                )
            )
            with self.assertRaisesRegex(WeeklyLlmRunnerError, "must be empty before scoring"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=output_dir,
                        provider=FakeProvider(fixture_path=fixture_path),
                        display_name="Fake Provider",
                        provider_name="fake",
                        execution_id=EXECUTION_ID,
                    )
                )

    def test_live_attestation_fail_closed(self) -> None:
        class LiveStubProvider(FakeProvider):
            network_required = True
            network_policy = "provider-api-only"

        zip_bytes, kit = _build_kit_zip()
        with tempfile.TemporaryDirectory() as tmp:
            kit_path = Path(tmp) / "kit.zip"
            kit_path.write_bytes(zip_bytes)
            fixture_path = Path(tmp) / "fixture.json"
            fixture_path.write_text(json.dumps(_fake_fixture(kit)), encoding="utf-8")
            with self.assertRaisesRegex(WeeklyLlmContractError, "--network-allowlist"):
                run_weekly_llm_score(
                    RunnerOptions(
                        kit_path=kit_path,
                        output_dir=Path(tmp) / "out",
                        provider=LiveStubProvider(fixture_path=fixture_path),
                        display_name="Claude Opus",
                        provider_name="anthropic",
                    )
                )
        del kit, zip_bytes

    def test_rejects_boolean_confidence(self) -> None:
        with self.assertRaises(WeeklyLlmResponseError):
            validate_model_response(
                {
                    "schema_version": "foldarium.selector-model-response/v1",
                    "item_id": "target-1",
                    "clustered": {"selection_kind": "none", "confidence": True, "evidence": "x"},
                    "unclustered": {"selection_kind": "none", "confidence": 0.5, "evidence": "x"},
                },
                item_id="target-1",
                allowed_cluster_ids={"cluster-a"},
                allowed_choice_ids={"choice-a"},
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


def _assert_secure_permissions(path: Path, expected_mode: int) -> None:
    actual = stat.S_IMODE(os.stat(path).st_mode)
    if actual != expected_mode:
        raise AssertionError(f"{path} permissions are {oct(actual)}, expected {oct(expected_mode)}")


class WeeklyLlmKitSecurityTests(unittest.TestCase):
    def test_rejects_zip_slip_paths(self) -> None:
        buffer = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        buffer.close()
        with zipfile.ZipFile(buffer.name, "w") as archive:
            archive.writestr("../evil.txt", "nope")
        with self.assertRaises(WeeklyLlmKitError):
            extract_verified_kit(Path(buffer.name).read_bytes(), output_dir=Path(tempfile.mkdtemp()))

    def test_load_network_allowlist_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowlist.json"
            path.write_text(json.dumps(["auth.example.test", "api.example.test"]), encoding="utf-8")
            loaded = load_network_allowlist(path)
            self.assertEqual(loaded, ["api.example.test", "auth.example.test"])


if __name__ == "__main__":
    unittest.main()
