"""Blindness attestation and reviewed network allowlist handling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .weekly_llm_contract import (
    EMPTY_NETWORK_ALLOWLIST_SHA256,
    WeeklyLlmContractError,
    build_blindness_attestation,
    sha256_hex,
    validate_blindness_attestation,
)


def load_network_allowlist(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise WeeklyLlmContractError("network allowlist must be a non-empty JSON array")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload):
        if not isinstance(entry, str) or not entry.strip():
            raise WeeklyLlmContractError(f"network allowlist[{index}] must be a non-empty string")
        value = entry.strip()
        if value in seen:
            raise WeeklyLlmContractError("network allowlist entries must be unique")
        seen.add(value)
        normalized.append(value)
    normalized.sort()
    return normalized


def network_allowlist_digest(allowlist: list[str]) -> str:
    if not allowlist:
        return EMPTY_NETWORK_ALLOWLIST_SHA256
    return sha256_hex(allowlist)


def build_provider_blindness_attestation(*, allowlist: list[str]) -> dict[str, Any]:
    digest = network_allowlist_digest(allowlist)
    if digest == EMPTY_NETWORK_ALLOWLIST_SHA256:
        raise WeeklyLlmContractError("live provider attestation requires a non-empty allowlist")
    return validate_blindness_attestation(
        {
            "schema_version": "foldarium.selector-blindness-attestation/v1",
            "workspace_policy": "verified-kit-only",
            "network_policy": "provider-api-only",
            "network_allowlist_sha256": digest,
            "browser_enabled": False,
            "web_search_enabled": False,
            "external_retrieval_enabled": False,
            "shared_cache_enabled": False,
        }
    )


def require_live_blindness_inputs(
    *,
    network_allowlist_path: Path | None,
    egress_enforcement_asserted: bool,
) -> list[str]:
    if network_allowlist_path is None:
        raise WeeklyLlmContractError("live provider run requires --network-allowlist")
    if not egress_enforcement_asserted:
        raise WeeklyLlmContractError(
            "live provider run requires --assert-provider-egress-enforced"
        )
    return load_network_allowlist(network_allowlist_path)


__all__ = [
    "build_provider_blindness_attestation",
    "load_network_allowlist",
    "network_allowlist_digest",
    "require_live_blindness_inputs",
]
