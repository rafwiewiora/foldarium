"""Orchestration for audited weekly LLM selector scoring."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .weekly_llm_config import METHOD_NAME, METHOD_VERSION, provenance_digests
from .weekly_llm_contract import (
    BENCHMARK_SCHEMA_VERSION,
    build_blindness_attestation,
    digest_post_close_benchmark,
    sha256_hex,
    validate_post_close_benchmark,
)
from .weekly_llm_kit import build_item_workspace, extract_verified_kit
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
    private_dir: Path
    benchmark_path: Path
    submission_path: Path
    submit_receipt: dict[str, Any] | None


def utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def render_item_prompt(*, item_id: str, candidate_evidence: list[Mapping[str, Any]]) -> str:
    evidence_json = canonical_json(list(candidate_evidence))
    prompt = SELECTOR_ITEM_PROMPT_TEMPLATE.replace("{{item_id}}", item_id).replace(
        "{{candidate_evidence_json}}", evidence_json
    )
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise WeeklyLlmRunnerError(f"rendered prompt for {item_id} exceeds {MAX_PROMPT_BYTES} bytes")
    return prompt


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def run_weekly_llm_score(options: RunnerOptions) -> RunnerResult:
    started_at = utc_now_iso()
    options.provider.preflight()

    zip_bytes = options.kit_path.read_bytes()
    execution_id = options.execution_id or str(uuid.uuid4())
    _secure_dir(options.output_dir)
    private_dir = options.output_dir / "private"
    _secure_dir(private_dir)

    with tempfile.TemporaryDirectory(prefix="foldarium-kit-") as kit_tmp:
        kit_dir = Path(kit_tmp)
        manifest = extract_verified_kit(zip_bytes, output_dir=kit_dir)
        item_results: list[ProviderResult] = []
        submission_items: list[dict[str, Any]] = []
        input_manifest_items: list[dict[str, str]] = []

        for item in sorted(manifest["items"], key=lambda row: row["item_id"]):
            item_id = item["item_id"]
            workspace = build_item_workspace(
                kit_dir=kit_dir,
                item=item,
                evidence_dir=options.output_dir / "evidence" / item_id,
            )
            prompt_text = render_item_prompt(
                item_id=item_id,
                candidate_evidence=workspace["candidate_evidence"],
            )
            prompt_digest = sha256_hex({"item_id": item_id, "prompt": prompt_text})
            input_manifest_items.append({"item_id": item_id, "prompt_sha256": prompt_digest})
            item_workspace_dir = kit_dir / "items" / item_id
            provider_result = options.provider.score_item(
                item_id=item_id,
                prompt_text=prompt_text,
                image_paths=[str(path) for path in workspace["image_paths"]],
                workspace_dir=str(item_workspace_dir),
            )
            item_results.append(provider_result)
            _write_private_json(
                private_dir / f"{item_id}.raw.json",
                provider_result.raw_envelope,
            )
            allowed_cluster_ids = {choice["cluster_id"] for choice in item["choices"]}
            allowed_choice_ids = {choice["choice_id"] for choice in item["choices"]}
            validated = validate_model_response(
                provider_result.response,
                item_id=item_id,
                allowed_cluster_ids=allowed_cluster_ids,
                allowed_choice_ids=allowed_choice_ids,
            )
            submission_items.append(model_response_to_submission_item(validated))

        observed_models = {model_id for result in item_results for model_id in result.observed_ids}
        if len(observed_models) != 1:
            raise WeeklyLlmRunnerError(
                f"provider run must observe exactly one model identifier; saw {sorted(observed_models)}"
            )

        input_manifest = {
            "schema_version": "foldarium.selector-input-manifest/v1",
            "prompt_profile_id": selector_prompt_profile()["prompt_profile_id"],
            "prompt_sha256": SELECTOR_PROMPT_SHA256,
            "items": sorted(input_manifest_items, key=lambda row: row["item_id"]),
        }
        engine_version = item_results[0].engine_version
        digests = provenance_digests(
            provider=options.provider_name,
            input_manifest=input_manifest,
            engine_version=engine_version,
        )
        payload = build_selector_submission(
            manifest,
            submission_id=execution_id,
            items=submission_items,
        )
        payload_digest = digest_selector_submission(payload)
        finished_at = utc_now_iso()
        first = item_results[0]
        usage = _aggregate_usage(item_results, started_at=started_at, finished_at=finished_at)
        attestation = build_blindness_attestation(network_policy="none")
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
                "run_id": _single_or_none(result.run_id for result in item_results),
                "session_id": _single_or_none(result.session_id for result in item_results),
            },
            "model": {
                "requested_id": first.requested_id,
                "observed_ids": sorted(observed_models),
                "requested_effort": first.requested_effort,
                "applied_effort": first.applied_effort,
                "effort_reporting": first.effort_reporting,
            },
            "provenance": {
                "prompt_profile_id": selector_prompt_profile()["prompt_profile_id"],
                "prompt_sha256": SELECTOR_PROMPT_SHA256,
                **digests,
            },
            "blindness_attestation": attestation,
            "blindness_attestation_sha256": sha256_hex(attestation),
            "usage": usage,
            "started_at": started_at,
            "finished_at": finished_at,
            "reasoning_trace_retained": False,
            "output_sha256": payload_digest,
            "payload": payload,
        }
        normalized = validate_post_close_benchmark(execution, kit=manifest)
        execution_digest = digest_post_close_benchmark(normalized, kit=manifest)

        benchmark_path = options.output_dir / "benchmark.execution.json"
        submission_path = options.output_dir / "submission.json"
        public_path = options.output_dir / "benchmark.public.json"
        manifest_path = options.output_dir / "input-manifest.json"
        _write_json(benchmark_path, normalized)
        _write_json(submission_path, payload)
        _write_json(public_path, _public_benchmark(normalized))
        _write_json(manifest_path, input_manifest)
        _write_json(private_dir / "input-manifest.json", input_manifest)

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
        int(result.usage.cost_usd * 1_000_000)
        for result in results
        if result.usage.cost_usd is not None
    ]
    return {
        "input_tokens": sum_int(lambda usage: usage.input_tokens),
        "output_tokens": sum_int(lambda usage: usage.output_tokens),
        "cache_read_tokens": sum_int(lambda usage: usage.cache_read_tokens),
        "cache_creation_tokens": sum_int(lambda usage: usage.cache_creation_tokens),
        "reasoning_tokens": sum_int(lambda usage: usage.reasoning_tokens),
        "cost_usd": (sum(cost_values) / 1_000_000) if cost_values else None,
        "duration_ms": duration_ms,
    }


def _single_or_none(values) -> str | None:
    normalized = {value for value in values if value}
    if not normalized:
        return None
    if len(normalized) == 1:
        return next(iter(normalized))
    raise WeeklyLlmRunnerError("conflicting private runtime identifiers across item runs")


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
    _secure_file(path)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    _write_json(path, payload)


__all__ = [
    "RunnerOptions",
    "RunnerResult",
    "WeeklyLlmRunnerError",
    "render_item_prompt",
    "run_weekly_llm_score",
    "submit_benchmark_execution",
    "utc_now_iso",
]
