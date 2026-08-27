"""Cross-language post-close benchmark and blindness contracts."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from .weekly_selector import (
    WeeklySelectorError,
    canonical_json,
    digest_selector_submission,
    validate_selector_submission,
)
from .weekly_selector_prompt import SELECTOR_PROMPT_PROFILE_ID, SELECTOR_PROMPT_SHA256

BENCHMARK_SCHEMA_VERSION = "foldarium.selector-post-close-benchmark/v1"
BENCHMARK_RECEIPT_SCHEMA_VERSION = "foldarium.selector-post-close-benchmark-receipt/v1"
BLINDNESS_ATTESTATION_SCHEMA_VERSION = "foldarium.selector-blindness-attestation/v1"
EMPTY_NETWORK_ALLOWLIST_SHA256 = (
    "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_EFFORTS = frozenset({"default", "low", "medium", "high", "max"})
_EFFORT_REPORTING = frozenset({"reported", "not_exposed"})

EXECUTION_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "supersedes_execution_id",
        "run_class",
        "environment",
        "round_id",
        "blind_manifest_sha256",
        "kit_sha256",
        "display_name",
        "method_name",
        "method_version",
        "provider",
        "engine",
        "model",
        "provenance",
        "blindness_attestation",
        "blindness_attestation_sha256",
        "usage",
        "started_at",
        "finished_at",
        "reasoning_trace_retained",
        "output_sha256",
        "payload",
    }
)
ENGINE_KEYS = frozenset({"name", "version", "run_id", "session_id"})
MODEL_KEYS = frozenset(
    {"requested_id", "observed_ids", "requested_effort", "applied_effort", "effort_reporting"}
)
PROVENANCE_KEYS = frozenset(
    {
        "prompt_profile_id",
        "prompt_sha256",
        "input_manifest_sha256",
        "tools_sha256",
        "config_sha256",
        "runtime_sha256",
    }
)
USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "reasoning_tokens",
        "cost_usd",
        "duration_ms",
    }
)
BLINDNESS_ATTESTATION_KEYS = frozenset(
    {
        "schema_version",
        "workspace_policy",
        "network_policy",
        "network_allowlist_sha256",
        "browser_enabled",
        "web_search_enabled",
        "external_retrieval_enabled",
        "shared_cache_enabled",
    }
)


class WeeklyLlmContractError(WeeklySelectorError):
    """Raised when a benchmark envelope violates the public contract."""


def sha256_hex(value: Any) -> str:
    from hashlib import sha256

    if isinstance(value, (bytes, bytearray)):
        payload = bytes(value)
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = canonical_json(value).encode("utf-8")
    return sha256(payload).hexdigest()


def build_blindness_attestation(*, network_policy: str = "none") -> dict[str, Any]:
    if network_policy not in {"none", "provider-api-only"}:
        raise WeeklyLlmContractError("network_policy must be none or provider-api-only")
    allowlist = EMPTY_NETWORK_ALLOWLIST_SHA256
    if network_policy == "provider-api-only":
        raise WeeklyLlmContractError(
            "provider-api-only requires an explicit reviewed allowlist digest"
        )
    return validate_blindness_attestation(
        {
            "schema_version": BLINDNESS_ATTESTATION_SCHEMA_VERSION,
            "workspace_policy": "verified-kit-only",
            "network_policy": network_policy,
            "network_allowlist_sha256": allowlist,
            "browser_enabled": False,
            "web_search_enabled": False,
            "external_retrieval_enabled": False,
            "shared_cache_enabled": False,
        }
    )


def validate_blindness_attestation(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("blindness_attestation must be an object")
    _exact_keys(raw, BLINDNESS_ATTESTATION_KEYS, "blindness_attestation")
    if raw.get("schema_version") != BLINDNESS_ATTESTATION_SCHEMA_VERSION:
        raise WeeklyLlmContractError("unsupported blindness_attestation schema_version")
    if raw.get("workspace_policy") != "verified-kit-only":
        raise WeeklyLlmContractError("blindness_attestation workspace_policy is invalid")
    network_policy = raw.get("network_policy")
    if network_policy not in {"none", "provider-api-only"}:
        raise WeeklyLlmContractError("blindness_attestation network_policy is invalid")
    allowlist = _required_digest(
        raw.get("network_allowlist_sha256"),
        "blindness_attestation network_allowlist_sha256",
    )
    if network_policy == "none" and allowlist != EMPTY_NETWORK_ALLOWLIST_SHA256:
        raise WeeklyLlmContractError(
            "blindness_attestation network_allowlist_sha256 must identify the canonical empty allowlist"
        )
    if network_policy == "provider-api-only" and allowlist == EMPTY_NETWORK_ALLOWLIST_SHA256:
        raise WeeklyLlmContractError(
            "blindness_attestation provider-api-only network policy requires a non-empty allowlist digest"
        )
    for capability in (
        "browser_enabled",
        "web_search_enabled",
        "external_retrieval_enabled",
        "shared_cache_enabled",
    ):
        if raw.get(capability) is not False:
            raise WeeklyLlmContractError(f"blindness_attestation {capability} must be false")
    return {
        "schema_version": BLINDNESS_ATTESTATION_SCHEMA_VERSION,
        "workspace_policy": "verified-kit-only",
        "network_policy": network_policy,
        "network_allowlist_sha256": allowlist,
        "browser_enabled": False,
        "web_search_enabled": False,
        "external_retrieval_enabled": False,
        "shared_cache_enabled": False,
    }


def validate_post_close_benchmark(
    raw: Mapping[str, Any],
    *,
    kit: Mapping[str, Any],
    context_environment: str | None = None,
    context_round_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("benchmark execution must be an object")
    _exact_keys(raw, EXECUTION_KEYS, "benchmark execution")
    if raw.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise WeeklyLlmContractError("benchmark schema_version is invalid")
    execution_id = raw.get("execution_id")
    if not isinstance(execution_id, str) or not _UUID_RE.fullmatch(execution_id):
        raise WeeklyLlmContractError("benchmark execution_id must be a canonical UUID")
    if raw.get("run_class") != "post_close_benchmark":
        raise WeeklyLlmContractError("benchmark run_class must be post_close_benchmark")

    environment = raw.get("environment")
    round_id = raw.get("round_id")
    expected_environment = context_environment or kit.get("environment")
    expected_round_id = context_round_id or kit.get("round_id")
    if environment != expected_environment:
        raise WeeklyLlmContractError("benchmark environment does not match deployment")
    if not isinstance(round_id, str) or round_id != expected_round_id:
        raise WeeklyLlmContractError("benchmark round_id does not match round")

    blind_digest = _required_digest(raw.get("blind_manifest_sha256"), "blind_manifest_sha256")
    kit_digest = _required_digest(raw.get("kit_sha256"), "kit_sha256")
    if blind_digest != kit.get("blind_manifest_sha256"):
        raise WeeklyLlmContractError("benchmark blind_manifest_sha256 does not match")
    if kit_digest != kit.get("kit_sha256"):
        raise WeeklyLlmContractError("benchmark kit_sha256 does not match")

    payload_raw = raw.get("payload")
    if not isinstance(payload_raw, Mapping):
        raise WeeklyLlmContractError("benchmark payload must be an object")
    payload = validate_selector_submission(dict(payload_raw), kit)

    normalized = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "execution_id": execution_id,
        "supersedes_execution_id": _optional_uuid(
            raw.get("supersedes_execution_id"), "supersedes_execution_id"
        ),
        "run_class": "post_close_benchmark",
        "environment": environment,
        "round_id": round_id,
        "blind_manifest_sha256": blind_digest,
        "kit_sha256": kit_digest,
        "display_name": _normalized_text(raw.get("display_name"), "display_name"),
        "method_name": _normalized_text(raw.get("method_name"), "method_name"),
        "method_version": _normalized_text(raw.get("method_version"), "method_version"),
        "provider": _normalized_text(raw.get("provider"), "provider"),
        "engine": _normalize_engine(raw.get("engine")),
        "model": _normalize_model(raw.get("model")),
        "provenance": _normalize_provenance(raw.get("provenance")),
        "blindness_attestation": validate_blindness_attestation(raw.get("blindness_attestation")),
        "blindness_attestation_sha256": _required_digest(
            raw.get("blindness_attestation_sha256"), "blindness_attestation_sha256"
        ),
        "usage": _normalize_usage(raw.get("usage")),
        "started_at": _normalized_timestamp(raw.get("started_at"), "started_at"),
        "finished_at": _normalized_timestamp(raw.get("finished_at"), "finished_at"),
        "reasoning_trace_retained": raw.get("reasoning_trace_retained"),
        "output_sha256": _required_digest(raw.get("output_sha256"), "output_sha256"),
        "payload": payload,
    }

    if normalized["reasoning_trace_retained"] is not False:
        raise WeeklyLlmContractError("benchmark reasoning_trace_retained must be false")
    attestation_digest = sha256_hex(normalized["blindness_attestation"])
    if normalized["blindness_attestation_sha256"] != attestation_digest:
        raise WeeklyLlmContractError("benchmark blindness attestation digest is inconsistent")
    if _parse_iso(normalized["finished_at"]) < _parse_iso(normalized["started_at"]):
        raise WeeklyLlmContractError("benchmark finished_at precedes started_at")
    if normalized["payload"]["submission_id"] != normalized["execution_id"]:
        raise WeeklyLlmContractError("benchmark payload submission_id must equal execution_id")
    if normalized["supersedes_execution_id"] == normalized["execution_id"]:
        raise WeeklyLlmContractError("benchmark execution cannot supersede itself")
    return normalized


def digest_post_close_benchmark(execution: Mapping[str, Any], *, kit: Mapping[str, Any]) -> str:
    normalized = validate_post_close_benchmark(execution, kit=kit)
    return sha256_hex(normalized)


def sanitize_public_benchmark(execution: Mapping[str, Any]) -> dict[str, Any]:
    """Strip runtime identifiers and private fields for public sharing."""

    forbidden = {
        "engine",
        "usage",
        "output_sha256",
        "blindness_attestation",
        "blindness_attestation_sha256",
        "payload",
    }
    public = {key: value for key, value in dict(execution).items() if key not in forbidden}
    public["run_class"] = "post_close_benchmark"
    return public


def sanitize_benchmark_receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise WeeklyLlmContractError("benchmark receipt is missing")
    return {
        "schema_version": BENCHMARK_RECEIPT_SCHEMA_VERSION,
        "execution_id": row.get("execution_id"),
        "run_class": "post_close_benchmark",
        "environment": row.get("environment"),
        "round_id": row.get("round_id"),
        "execution_sha256": row.get("execution_sha256"),
        "payload_digest": row.get("payload_digest"),
        "accepted_at": row.get("accepted_at"),
        "idempotent": row.get("idempotent") is True,
    }


def _exact_keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(value) - set(allowed)
    missing = set(allowed) - set(value)
    if unknown or missing:
        raise WeeklyLlmContractError(f"{label} keys are not exact")


def _normalized_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WeeklyLlmContractError(f"{label} must be text")
    normalized = " ".join(value.strip().split())
    if not normalized or len(normalized) > 160:
        raise WeeklyLlmContractError(f"{label} is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise WeeklyLlmContractError(f"{label} is invalid")
    return normalized


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _normalized_text(value, label)


def _required_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise WeeklyLlmContractError(f"{label} must be a lowercase SHA-256")
    return value


def _optional_uuid(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise WeeklyLlmContractError(f"benchmark {label} must be a canonical UUID")
    return value


def _normalized_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise WeeklyLlmContractError(f"benchmark {label} is invalid")
    parsed = _parse_iso(value)
    canonical = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if value != canonical:
        raise WeeklyLlmContractError(f"benchmark {label} must be canonical UTC")
    return canonical


def _parse_iso(value: str) -> datetime:
    if not value.endswith("Z"):
        raise WeeklyLlmContractError("timestamp must be UTC with Z suffix")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_engine(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("engine must be an object")
    _exact_keys(raw, ENGINE_KEYS, "engine")
    return {
        "name": _normalized_text(raw.get("name"), "engine.name"),
        "version": _normalized_text(raw.get("version"), "engine.version"),
        "run_id": _optional_text(raw.get("run_id"), "engine.run_id"),
        "session_id": _optional_text(raw.get("session_id"), "engine.session_id"),
    }


def _normalize_model(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("model must be an object")
    _exact_keys(raw, MODEL_KEYS, "model")
    requested_effort = _normalized_text(raw.get("requested_effort"), "model.requested_effort")
    effort_reporting = _normalized_text(raw.get("effort_reporting"), "model.effort_reporting")
    if requested_effort not in _EFFORTS or effort_reporting not in _EFFORT_REPORTING:
        raise WeeklyLlmContractError("benchmark model effort provenance is invalid")
    applied_effort = raw.get("applied_effort")
    if applied_effort is not None:
        applied_effort = _normalized_text(applied_effort, "model.applied_effort")
    if applied_effort is not None and applied_effort not in _EFFORTS:
        raise WeeklyLlmContractError("benchmark applied effort provenance is inconsistent")
    if effort_reporting == "reported" and applied_effort is None:
        raise WeeklyLlmContractError("benchmark applied effort provenance is inconsistent")
    if effort_reporting == "not_exposed" and applied_effort is not None:
        raise WeeklyLlmContractError("benchmark applied effort provenance is inconsistent")
    observed_raw = raw.get("observed_ids")
    if not isinstance(observed_raw, list) or len(observed_raw) != 1:
        raise WeeklyLlmContractError("benchmark model.observed_ids must contain exactly one model")
    observed_ids = [
        _normalized_text(value, f"model.observed_ids[{index}]")
        for index, value in enumerate(observed_raw)
    ]
    if len(set(observed_ids)) != len(observed_ids) or observed_ids != sorted(observed_ids):
        raise WeeklyLlmContractError("benchmark model.observed_ids must be sorted and unique")
    return {
        "requested_id": _normalized_text(raw.get("requested_id"), "model.requested_id"),
        "observed_ids": observed_ids,
        "requested_effort": requested_effort,
        "applied_effort": applied_effort,
        "effort_reporting": effort_reporting,
    }


def _normalize_provenance(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("provenance must be an object")
    _exact_keys(raw, PROVENANCE_KEYS, "provenance")
    if raw.get("prompt_profile_id") != SELECTOR_PROMPT_PROFILE_ID:
        raise WeeklyLlmContractError("benchmark prompt_profile_id is invalid")
    if raw.get("prompt_sha256") != SELECTOR_PROMPT_SHA256:
        raise WeeklyLlmContractError("benchmark prompt_sha256 does not match prompt profile")
    return {
        "prompt_profile_id": SELECTOR_PROMPT_PROFILE_ID,
        "prompt_sha256": SELECTOR_PROMPT_SHA256,
        "input_manifest_sha256": _required_digest(
            raw.get("input_manifest_sha256"), "provenance.input_manifest_sha256"
        ),
        "tools_sha256": _required_digest(raw.get("tools_sha256"), "provenance.tools_sha256"),
        "config_sha256": _required_digest(raw.get("config_sha256"), "provenance.config_sha256"),
        "runtime_sha256": _required_digest(raw.get("runtime_sha256"), "provenance.runtime_sha256"),
    }


def _normalize_usage(raw: Any) -> dict[str, Any | None]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmContractError("usage must be an object")
    _exact_keys(raw, USAGE_KEYS, "usage")
    normalized: dict[str, Any | None] = {}
    for key in USAGE_KEYS:
        value = raw.get(key)
        if value is None:
            normalized[key] = None
            continue
        if isinstance(value, bool):
            raise WeeklyLlmContractError(f"benchmark usage.{key} must be null or non-negative")
        if isinstance(value, float) and not math.isfinite(value):
            raise WeeklyLlmContractError(f"benchmark usage.{key} must be null or non-negative")
        if not isinstance(value, (int, float)) or value < 0:
            raise WeeklyLlmContractError(f"benchmark usage.{key} must be null or non-negative")
        if key != "cost_usd" and not isinstance(value, int):
            raise WeeklyLlmContractError(f"benchmark usage.{key} must be an integer")
        normalized[key] = value
    return normalized


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BLINDNESS_ATTESTATION_SCHEMA_VERSION",
    "EMPTY_NETWORK_ALLOWLIST_SHA256",
    "WeeklyLlmContractError",
    "build_blindness_attestation",
    "digest_post_close_benchmark",
    "digest_selector_submission",
    "sanitize_benchmark_receipt",
    "sanitize_public_benchmark",
    "sha256_hex",
    "validate_blindness_attestation",
    "validate_post_close_benchmark",
]
