"""Canonical inference configuration and provenance digests."""

from __future__ import annotations

import platform
import sys
from typing import Any, Mapping

from .weekly_llm_contract import EMPTY_NETWORK_ALLOWLIST_SHA256, sha256_hex

METHOD_NAME = "blind-pose-selector"
METHOD_VERSION = "weekly-pose-selector-v1"


def canonical_tools_manifest() -> list[str]:
    return []


def canonical_inference_config(*, provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "temperature": 0,
        "top_p": 1,
        "seed": None,
        "max_tokens": 1024,
        "stop_sequences": [],
        "reasoning_enabled": False,
        "tools": [],
        "mcp_servers": [],
        "setting_sources": [],
        "browser_enabled": False,
        "web_search_enabled": False,
        "external_retrieval_enabled": False,
        "shared_cache_enabled": False,
        "concurrency": 1,
        "timeout_seconds": 600,
        "retry_policy": {"max_attempts": 1},
    }


def provenance_digests(
    *,
    provider: str,
    input_manifest: Mapping[str, Any],
    engine_version: str,
) -> dict[str, str]:
    return {
        "tools_sha256": sha256_hex(canonical_tools_manifest()),
        "config_sha256": sha256_hex(canonical_inference_config(provider=provider)),
        "runtime_sha256": sha256_hex(
            {
                "python_version": sys.version.split()[0],
                "platform": platform.platform(),
                "engine_version": engine_version,
            }
        ),
        "input_manifest_sha256": sha256_hex(input_manifest),
    }


def empty_tools_sha256() -> str:
    return EMPTY_NETWORK_ALLOWLIST_SHA256


__all__ = [
    "METHOD_NAME",
    "METHOD_VERSION",
    "canonical_inference_config",
    "canonical_tools_manifest",
    "empty_tools_sha256",
    "provenance_digests",
]
