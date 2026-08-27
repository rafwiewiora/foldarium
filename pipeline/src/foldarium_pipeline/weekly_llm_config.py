"""Provider-specific inference configuration snapshots."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .weekly_llm_provenance import response_schema_digest, tools_sha256


def fake_provider_config() -> dict[str, Any]:
    return {
        "provider": "fake",
        "engine": "fake-provider",
        "tools": [],
        "setting_sources": [],
        "mcp_servers": [],
        "browser_enabled": False,
        "web_search_enabled": False,
        "external_retrieval_enabled": False,
        "shared_cache_enabled": False,
        "concurrency": 1,
        "retry_policy": {"max_attempts": 1},
        "response_schema_digest": response_schema_digest(),
        "provider_system_prompt_control": "fixture",
        "network_policy": "none",
    }


def claude_provider_config(
    *,
    engine_version: str,
    subprocess_timeout_seconds: int,
    cli_flags: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "engine": "claude-cli",
        "engine_version": engine_version,
        "model_alias": "opus",
        "requested_effort": "default",
        "tools": [],
        "setting_sources": [],
        "mcp_servers": [],
        "browser_enabled": False,
        "web_search_enabled": False,
        "external_retrieval_enabled": False,
        "shared_cache_enabled": False,
        "concurrency": 1,
        "retry_policy": {"max_attempts": 1},
        "subprocess_timeout_seconds": subprocess_timeout_seconds,
        "response_schema_digest": response_schema_digest(),
        "provider_system_prompt_control": "exact_system_prompt",
        "cli_flags": dict(cli_flags),
        "network_policy": "provider-api-only",
    }


def cursor_provider_config(
    *,
    engine_version: str,
    model_id: str,
    model_params: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        "provider": "cursor",
        "engine": "cursor-sdk",
        "engine_version": engine_version,
        "model_id": model_id,
        "model_params": [dict(param) for param in model_params],
        "requested_effort": "high",
        "tools": [],
        "setting_sources": [],
        "mcp_servers": [],
        "browser_enabled": False,
        "web_search_enabled": False,
        "external_retrieval_enabled": False,
        "shared_cache_enabled": False,
        "concurrency": 1,
        "retry_policy": {"max_attempts": 1},
        "response_schema_digest": response_schema_digest(),
        "provider_system_prompt_control": "user_message_only",
        "network_policy": "provider-api-only",
    }


METHOD_NAME = "blind-pose-selector"
METHOD_VERSION = "weekly-pose-selector-v1"


__all__ = [
    "METHOD_NAME",
    "METHOD_VERSION",
    "claude_provider_config",
    "cursor_provider_config",
    "fake_provider_config",
    "tools_sha256",
]
