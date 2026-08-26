"""Orchestration for audited weekly LLM selector scoring."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .weekly_llm_blindness import (
    build_provider_blindness_attestation,
    load_network_allowlist,
    require_live_blindness_inputs,
)
from .weekly_llm_config import METHOD_NAME, METHOD_VERSION
from .weekly_llm_contract import (
    BENCHMARK_SCHEMA_VERSION,
    build_blindness_attestation,
    digest_post_close_benchmark,
    sha256_hex,
    validate_post_close_benchmark,
)
from .weekly_llm_kit import build_item_workspace, extract_verified_kit
from .weekly_llm_provenance import (
    build_input_manifest,
    build_output_manifest,
    build_runtime_manifest,
    digest_manifest,
    tools_sha256,
)
from .weekly_llm_providers import ProviderResult, WeeklyLlmProvider
from .weekly_llm_response import model_response_to_submission_item, validate_model_response
from .weekly_selector import (
    WeeklySelectorError,
    build_selector_submission,
    canonical_json,
    digest_selector_submission,
)
from .weekly_selector_prompt import (
    SELECTOR_ITEM_PROMPT_TEMPLATE,
    SELECTOR_PROMPT_SHA256,
    selector_prompt_profile,
)

MAX_PROMPT_BYTES = 256_000


class WeeklyLlmRunnerError(WeeklySelectorError):
    """Raised when the weekly LLM scoring runner fails."""


@dataclass(frozen=True)
class RunnerOptions:
    kit_path: Path
    output_dir: Path
    provider: WeeklyLlmProvider
    display_name: str
    provider_name: str
    network_allowlist_path: Path | None = None
    egress_enforcement_asserted: bool = False
    execution_id: str | None = None
    supersedes_execution_id: str | None = None
    submit_url: str | None = None
    submit_token: str | None = None
    dry_run_submit: bool = True


@dataclass(frozen=True)
class RunnerResult:
    execution: dict[str, Any]
    execution_digest: str
    payload_digest: str
    output_sha256: str
    private_dir: Path
    benchmark_path: Path
    submission_path: Path
    submit_receipt: dict[str, Any] | None


def utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def render_item_prompt(*, item_id: str, item_evidence: Mapping[str, Any]) -> str:
    evidence_json = canonical_json(dict(item_evidence))
    prompt = SELECTOR_ITEM_PROMPT_TEMPLATE.replace("{{item_id}}", item_id).replace(
        "{{candidate_evidence_json}}", evidence_json
    )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise WeeklyLlmRunnerError(f"rendered prompt for {item_id} exceeds {MAX_PROMPT_BYTES} bytes")
    return prompt


def _artifact_ref(*, relative_path: str, content: bytes) -> dict[str, Any]:
    return {
        "path": relative_path,
        "sha256": sha256_hex(content),
        "bytes": len(content),
    }


def _resolve_network_attestation(
    provider: WeeklyLlmProvider,
    *,
    network_allowlist_path: Path | None,
    egress_enforcement_asserted: bool,
) -> tuple[dict[str, Any], list[str] | None]:
    if provider.network_required:
        if provider.network_policy != "provider-api-only":
            raise WeeklyLlmRunnerError(
                f"unsupported live provider network policy: {provider.network_policy}"
            )
        allowlist = require_live_blindness_inputs(
            network_allowlist_path=network_allowlist_path,
            egress_enforcement_asserted=egress_enforcement_asserted,
        )
        return build_provider_blindness_attestation(allowlist=allowlist), allowlist
    if network_allowlist_path is not None:
        raise WeeklyLlmRunnerError("network allowlist is only valid for live providers")
    if egress_enforcement_asserted:
        raise WeeklyLlmRunnerError("egress enforcement attestation is only valid for live providers")
    if provider.network_policy != "none":
        raise WeeklyLlmRunnerError(
            f"offline provider {provider.network_policy!r} cannot use network policy none mismatch"
        )
    return build_blindness_attestation(network_policy="none"), None


def _chmod_or_raise(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as error:
        raise WeeklyLlmRunnerError(f"unable to set permissions on {path}: {error}") from error


def _assert_secure_permissions(path: Path, expected_mode: int) -> None:
    actual = stat.S_IMODE(os.stat(path).st_mode)
    if actual != expected_mode:
        raise WeeklyLlmRunnerError(f"{path} permissions are {oct(actual)}, expected {oct(expected_mode)}")


def _prepare_execution_output_dir(base_dir: Path, execution_id: str) -> Path:
    execution_dir = base_dir / execution_id
    if execution_dir.exists() and any(execution_dir.iterdir()):
        raise WeeklyLlmRunnerError(
            f"execution output directory {execution_dir} must be empty before scoring"
        )
    execution_dir.mkdir(parents=True, exist_ok=True)
    _chmod_or_raise(execution_dir, 0o700)
    _assert_secure_permissions(execution_dir, 0o700)
    return execution_dir


def _validate_item_results_consistent(results: list[ProviderResult]) -> tuple[str, ...]:
    if not results:
        raise WeeklyLlmRunnerError("provider run produced no item results")
    per_item_observed: list[tuple[str, ...]] = []
    for index, result in enumerate(results):
        if len(result.observed_ids) != 1:
            raise WeeklyLlmRunnerError(
                f"item result {index} must observe exactly one model identifier"
            )
        per_item_observed.append(result.observed_ids)
    observed_union = {model_id for ids in per_item_observed for model_id in ids}
    if len(observed_union) != 1:
        raise WeeklyLlmRunnerError(
            f"provider run must observe exactly one model identifier across items; saw {sorted(observed_union)}"
        )
    reference = results[0]
    for index, result in enumerate(results[1:], start=1):
        if result.requested_id != reference.requested_id:
            raise WeeklyLlmRunnerError(f"item {index} requested_id mismatch")
        if result.requested_effort != reference.requested_effort:
            raise WeeklyLlmRunnerError(f"item {index} requested_effort mismatch")
        if result.applied_effort != reference.applied_effort:
            raise WeeklyLlmRunnerError(f"item {index} applied_effort mismatch")
        if result.effort_reporting != reference.effort_reporting:
            raise WeeklyLlmRunnerError(f"item {index} effort_reporting mismatch")
        if result.engine_name != reference.engine_name or result.engine_version != reference.engine_version:
            raise WeeklyLlmRunnerError(f"item {index} engine provenance mismatch")
        if result.observed_ids != reference.observed_ids:
            raise WeeklyLlmRunnerError(f"item {index} observed model mismatch")
        if result.provider_config != reference.provider_config:
            raise WeeklyLlmRunnerError(f"item {index} provider config mismatch")
    return reference.observed_ids


def run_weekly_llm_score(options: RunnerOptions) -> RunnerResult:
    attestation, allowlist = _resolve_network_attestation(
        options.provider,
        network_allowlist_path=options.network_allowlist_path,
        egress_enforcement_asserted=options.egress_enforcement_asserted,
    )

    started_at = utc_now_iso()
    options.provider.preflight()

    zip_bytes = options.kit_path.read_bytes()
    kit_zip_sha256 = sha256_hex(zip_bytes)
    execution_id = options.execution_id or str(uuid.uuid4())
    output_dir = _prepare_execution_output_dir(options.output_dir, execution_id)
    private_dir = output_dir / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    _chmod_or_raise(private_dir, 0o700)
    _assert_secure_permissions(private_dir, 0o700)

    network_allowlist_artifact: dict[str, Any] | None = None
    if allowlist is not None:
        allowlist_bytes = (canonical_json(allowlist) + "\n").encode("utf-8")
        allowlist_path = private_dir / "network-allowlist.json"
        _write_private_bytes(allowlist_path, allowlist_bytes)
        network_allowlist_artifact = _artifact_ref(
            relative_path="private/network-allowlist.json",
            content=allowlist_bytes,
        )

    with tempfile.TemporaryDirectory(prefix="foldarium-kit-") as kit_tmp:
        kit_dir = Path(kit_tmp)
        manifest = extract_verified_kit(zip_bytes, output_dir=kit_dir)
        item_results: list[ProviderResult] = []
        submission_items: list[dict[str, Any]] = []
        input_manifest_items: list[dict[str, Any]] = []
        runtime_manifest_items: list[dict[str, Any]] = []
        output_manifest_items: list[dict[str, Any]] = []

        for item in sorted(manifest["items"], key=lambda row: row["item_id"]):
            item_id = item["item_id"]
            workspace = build_item_workspace(
                kit_dir=kit_dir,
                item=item,
                evidence_dir=output_dir / "evidence" / item_id,
            )
            prompt_text = render_item_prompt(
                item_id=item_id,
                item_evidence=workspace["item_evidence"],
            )
            prompt_bytes = prompt_text.encode("utf-8")
            prompt_bytes_sha256 = sha256_hex(prompt_text)
            evidence_json_bytes = (canonical_json(workspace["item_evidence"]) + "\n").encode("utf-8")
            item_private_dir = private_dir / "items" / item_id
            item_private_dir.mkdir(parents=True, exist_ok=True)
            _chmod_or_raise(item_private_dir, 0o700)
            prompt_artifact_path = item_private_dir / "prompt.txt"
            evidence_artifact_path = item_private_dir / "candidate-evidence.json"
            _write_private_bytes(prompt_artifact_path, prompt_bytes)
            _write_private_bytes(evidence_artifact_path, evidence_json_bytes)
            prompt_artifact = _artifact_ref(
                relative_path=f"private/items/{item_id}/prompt.txt",
                content=prompt_bytes,
            )
            evidence_artifact = _artifact_ref(
                relative_path=f"private/items/{item_id}/candidate-evidence.json",
                content=evidence_json_bytes,
            )
            item_workspace_dir = kit_dir / "items" / item_id
            image_paths = [attachment["path"] for attachment in workspace["image_attachments"]]
            provider_result = options.provider.score_item(
                item_id=item_id,
                prompt_text=prompt_text,
                image_paths=image_paths,
                workspace_dir=str(item_workspace_dir),
            )
            item_results.append(provider_result)
            _write_private_json(private_dir / f"{item_id}.raw.json", provider_result.raw_envelope)
            allowed_cluster_ids = {choice["cluster_id"] for choice in item["choices"]}
            allowed_choice_ids = {choice["choice_id"] for choice in item["choices"]}
            validated = validate_model_response(
                provider_result.response,
                item_id=item_id,
                allowed_cluster_ids=allowed_cluster_ids,
                allowed_choice_ids=allowed_choice_ids,
            )
            validated_json_bytes = (canonical_json(validated) + "\n").encode("utf-8")
            validated_artifact_path = item_private_dir / "validated-response.json"
            _write_private_bytes(validated_artifact_path, validated_json_bytes)
            validated_artifact = _artifact_ref(
                relative_path=f"private/items/{item_id}/validated-response.json",
                content=validated_json_bytes,
            )
            validated_digest = sha256_hex(validated)
            submission_items.append(model_response_to_submission_item(validated))
            input_manifest_items.append(
                {
                    "item_id": item_id,
                    "kit_zip_sha256": kit_zip_sha256,
                    "candidate_evidence_digest": workspace["candidate_evidence_digest"],
                    "rendered_prompt_sha256": prompt_bytes_sha256,
                    "rendered_prompt_bytes": len(prompt_bytes),
                    "prompt_artifact": prompt_artifact,
                    "candidate_evidence_artifact": evidence_artifact,
                    "attachments": [
                        {
                            "attachment_index": attachment["attachment_index"],
                            "choice_id": attachment["choice_id"],
                            "filename": attachment["filename"],
                            "sha256": attachment["sha256"],
                        }
                        for attachment in workspace["image_attachments"]
                    ],
                }
            )
            runtime_manifest_items.append(
                {
                    "item_id": item_id,
                    "rendered_prompt_sha256": prompt_bytes_sha256,
                    "rendered_prompt_bytes": len(prompt_bytes),
                    "prompt_artifact": prompt_artifact,
                    "candidate_evidence_digest": workspace["candidate_evidence_digest"],
                    "candidate_evidence_artifact": evidence_artifact,
                    "validated_response_sha256": validated_digest,
                    "validated_response_artifact": validated_artifact,
                    "raw_envelope_sha256": provider_result.raw_envelope_digest,
                    "observed_model_id": provider_result.observed_ids[0],
                    "run_id": provider_result.run_id,
                    "session_id": provider_result.session_id,
                    "usage": {
                        "input_tokens": provider_result.usage.input_tokens,
                        "output_tokens": provider_result.usage.output_tokens,
                        "cache_read_tokens": provider_result.usage.cache_read_tokens,
                        "cache_creation_tokens": provider_result.usage.cache_creation_tokens,
                        "reasoning_tokens": provider_result.usage.reasoning_tokens,
                        "cost_usd": provider_result.usage.cost_usd,
                        "charged_cents": provider_result.usage.charged_cents,
                        "raw_cost_cents": provider_result.usage.raw_cost_cents,
                        "duration_ms": provider_result.usage.duration_ms,
                    },
                }
            )
            output_manifest_items.append(
                {
                    "item_id": item_id,
                    "response_sha256": validated_digest,
                    "validated_response_artifact": validated_artifact,
                }
            )

        observed_ids = _validate_item_results_consistent(item_results)
        first = item_results[0]
        provider_config = dict(first.provider_config)
        if allowlist is not None:
            provider_config = {
                **provider_config,
                "network_allowlist_sha256": sha256_hex(allowlist),
            }
            if network_allowlist_artifact is not None:
                provider_config["network_allowlist_artifact"] = network_allowlist_artifact

        input_manifest = build_input_manifest(
            prompt_profile_id=selector_prompt_profile()["prompt_profile_id"],
            kit_zip_sha256=kit_zip_sha256,
            items=input_manifest_items,
        )
        runtime_manifest = build_runtime_manifest(items=runtime_manifest_items)
        output_manifest = build_output_manifest(items=output_manifest_items)
        config_sha256 = sha256_hex(provider_config)
        runtime_sha256 = digest_manifest(runtime_manifest)
        input_manifest_sha256 = digest_manifest(input_manifest)
        output_sha256 = digest_manifest(output_manifest)

        payload = build_selector_submission(
            manifest,
            submission_id=execution_id,
            items=submission_items,
        )
        payload_digest = digest_selector_submission(payload)
        finished_at = utc_now_iso()
        usage = _aggregate_usage(item_results, started_at=started_at, finished_at=finished_at)
        execution = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "execution_id": execution_id,
            "supersedes_execution_id": options.supersedes_execution_id,
            "run_class": "post_close_benchmark",
            "environment": manifest["environment"],
            "round_id": manifest["round_id"],
            "blind_manifest_sha256": manifest["blind_manifest_sha256"],
            "kit_sha256": manifest["kit_sha256"],
            "display_name": options.display_name,
            "method_name": METHOD_NAME,
            "method_version": METHOD_VERSION,
            "provider": options.provider_name,
            "engine": {
                "name": first.engine_name,
                "version": first.engine_version,
                "run_id": None,
                "session_id": None,
            },
            "model": {
                "requested_id": first.requested_id,
                "observed_ids": list(observed_ids),
                "requested_effort": first.requested_effort,
                "applied_effort": first.applied_effort,
                "effort_reporting": first.effort_reporting,
            },
            "provenance": {
                "prompt_profile_id": selector_prompt_profile()["prompt_profile_id"],
                "prompt_sha256": SELECTOR_PROMPT_SHA256,
                "input_manifest_sha256": input_manifest_sha256,
                "tools_sha256": tools_sha256(),
                "config_sha256": config_sha256,
                "runtime_sha256": runtime_sha256,
            },
            "blindness_attestation": attestation,
            "blindness_attestation_sha256": sha256_hex(attestation),
            "usage": usage,
            "started_at": started_at,
            "finished_at": finished_at,
            "reasoning_trace_retained": False,
            "output_sha256": output_sha256,
            "payload": payload,
        }
        normalized = validate_post_close_benchmark(execution, kit=manifest)
        execution_digest = digest_post_close_benchmark(normalized, kit=manifest)

        benchmark_path = output_dir / "benchmark.execution.json"
        submission_path = output_dir / "submission.json"
        public_path = output_dir / "benchmark.public.json"
        _write_json(benchmark_path, normalized)
        _write_json(submission_path, payload)
        _write_json(public_path, _public_benchmark(normalized))
        _write_json(output_dir / "input-manifest.json", input_manifest)
        _write_json(private_dir / "input-manifest.json", input_manifest)
        _write_json(private_dir / "runtime-manifest.json", runtime_manifest)
        _write_json(private_dir / "output-manifest.json", output_manifest)
        _write_json(private_dir / "provider-config.json", provider_config)

        submit_receipt = None
        if options.submit_url and options.submit_token and not options.dry_run_submit:
            submit_receipt = submit_benchmark_execution(
                options.submit_url,
                options.submit_token,
                normalized,
            )
            _write_private_json(private_dir / "submit-receipt.json", submit_receipt or {})

        return RunnerResult(
            execution=normalized,
            execution_digest=execution_digest,
            payload_digest=payload_digest,
            output_sha256=output_sha256,
            private_dir=private_dir,
            benchmark_path=benchmark_path,
            submission_path=submission_path,
            submit_receipt=submit_receipt,
        )


def submit_benchmark_execution(
    api_url: str,
    bearer_token: str,
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    body = canonical_json(dict(execution)).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code in {200, 201}:
            return json.loads(detail) if detail else {}
        raise WeeklyLlmRunnerError(f"benchmark submit failed ({error.code}): {detail}") from error
    except URLError as error:
        raise WeeklyLlmRunnerError(f"benchmark submit failed: {error}") from error


def _aggregate_usage(
    results: list[ProviderResult],
    *,
    started_at: str,
    finished_at: str,
) -> dict[str, Any | None]:
    def sum_int(getter) -> int | None:
        values = [getter(result.usage) for result in results if getter(result.usage) is not None]
        return sum(values) if values else None

    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    duration_ms = max(int((finished - started).total_seconds() * 1000), 0)
    cost_values = [
        result.usage.cost_usd
        for result in results
        if result.usage.cost_usd is not None
    ]
    return {
        "input_tokens": sum_int(lambda usage: usage.input_tokens),
        "output_tokens": sum_int(lambda usage: usage.output_tokens),
        "cache_read_tokens": sum_int(lambda usage: usage.cache_read_tokens),
        "cache_creation_tokens": sum_int(lambda usage: usage.cache_creation_tokens),
        "reasoning_tokens": sum_int(lambda usage: usage.reasoning_tokens),
        "cost_usd": sum(cost_values) if cost_values else None,
        "duration_ms": duration_ms,
    }


def _public_benchmark(execution: Mapping[str, Any]) -> dict[str, Any]:
    blocked = {
        "engine",
        "usage",
        "output_sha256",
        "blindness_attestation",
        "blindness_attestation_sha256",
        "payload",
    }
    public = {key: value for key, value in dict(execution).items() if key not in blocked}
    public["run_class"] = "post_close_benchmark"
    return public


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    _chmod_or_raise(path, 0o600)
    _assert_secure_permissions(path, 0o600)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_json(path, payload)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    _chmod_or_raise(path, 0o600)
    _assert_secure_permissions(path, 0o600)


__all__ = [
    "RunnerOptions",
    "RunnerResult",
    "WeeklyLlmRunnerError",
    "render_item_prompt",
    "run_weekly_llm_score",
    "submit_benchmark_execution",
    "utc_now_iso",
]
