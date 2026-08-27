"""Claude Code CLI adapter for weekly selector scoring."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage
from ..weekly_llm_config import claude_provider_config
from ..weekly_llm_contract import sha256_hex
from ..weekly_llm_provenance import canonical_private_json
from ..weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA, SELECTOR_SYSTEM_PROMPT

_CLAUDE_MODEL_ALIAS = "opus"
_DEFAULT_TIMEOUT_SECONDS = 600


class ClaudeProviderError(RuntimeError):
    """Raised when Claude CLI preflight or scoring fails."""


@dataclass(frozen=True)
class ClaudeParseResult:
    response: dict[str, Any]
    usage: ProviderUsage
    observed_ids: tuple[str, ...]
    session_id: str | None
    run_id: str | None
    applied_effort: str | None


def claude_cli_version() -> str:
    executable = shutil.which("claude")
    if not executable:
        raise ClaudeProviderError("claude CLI is not installed")
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ClaudeProviderError("unable to determine claude CLI version")
    return completed.stdout.strip() or completed.stderr.strip()


def preflight_claude_auth() -> dict[str, Any]:
    executable = shutil.which("claude")
    if not executable:
        raise ClaudeProviderError("claude CLI is not installed")
    completed = subprocess.run(
        [executable, "auth", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ClaudeProviderError(completed.stderr.strip() or "claude auth status failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ClaudeProviderError("claude auth status returned invalid JSON") from error
    if payload.get("loggedIn") is not True:
        raise ClaudeProviderError("claude auth status loggedIn must be true")
    if payload.get("authMethod") != "claude.ai":
        raise ClaudeProviderError("claude authMethod must be claude.ai")
    if payload.get("apiProvider") != "firstParty":
        raise ClaudeProviderError("claude apiProvider must be firstParty")
    if not payload.get("subscriptionType"):
        raise ClaudeProviderError("claude subscriptionType must be present")
    return {
        "logged_in": True,
        "auth_method": payload.get("authMethod"),
        "api_provider": payload.get("apiProvider"),
        "subscription_type": payload.get("subscriptionType"),
    }


def build_claude_command(
    *,
    prompt_text: str,
    mcp_config_path: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    del timeout_seconds  # enforced by subprocess.run timeout, recorded in config
    executable = shutil.which("claude")
    if not executable:
        raise ClaudeProviderError("claude CLI is not installed")
    schema = json.dumps(SELECTOR_MODEL_RESPONSE_SCHEMA, separators=(",", ":"), ensure_ascii=True)
    return [
        executable,
        "-p",
        "--model",
        _CLAUDE_MODEL_ALIAS,
        "--output-format",
        "json",
        "--json-schema",
        schema,
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config_path,
        "--system-prompt",
        SELECTOR_SYSTEM_PROMPT,
        "--setting-sources",
        "",
        "--tools",
        "",
        "--no-session-persistence",
        prompt_text,
    ]


def parse_claude_json_output(payload: Mapping[str, Any]) -> ClaudeParseResult:
    structured = payload.get("structured_output")
    if isinstance(structured, Mapping):
        response = dict(structured)
    else:
        result_raw = payload.get("result")
        if isinstance(result_raw, str):
            try:
                response = json.loads(result_raw)
            except json.JSONDecodeError as error:
                raise ClaudeProviderError("claude result is not valid JSON") from error
        elif isinstance(result_raw, Mapping):
            response = dict(result_raw)
        else:
            raise ClaudeProviderError("claude result is missing")

    model_usage = _first_mapping(payload.get("modelUsage"), payload.get("usage"))
    observed_ids = _extract_observed_model_ids(model_usage, payload)
    applied_effort = None
    if isinstance(payload.get("effort"), str):
        applied_effort = payload["effort"]
    elif isinstance(model_usage, Mapping) and isinstance(model_usage.get("effort"), str):
        applied_effort = model_usage["effort"]

    usage = ProviderUsage(
        input_tokens=_int_or_none(_lookup_usage(model_usage, "input_tokens", "inputTokens")),
        output_tokens=_int_or_none(_lookup_usage(model_usage, "output_tokens", "outputTokens")),
        cache_read_tokens=_int_or_none(
            _lookup_usage(
                model_usage,
                "cache_read_tokens",
                "cacheReadTokens",
                "cacheReadInputTokens",
            )
        ),
        cache_creation_tokens=_int_or_none(
            _lookup_usage(
                model_usage,
                "cache_creation_tokens",
                "cacheCreationTokens",
                "cacheCreationInputTokens",
            )
        ),
        reasoning_tokens=_int_or_none(_lookup_usage(model_usage, "reasoning_tokens", "reasoningTokens")),
        cost_usd=_extract_cost_usd(model_usage, payload),
        duration_ms=_int_or_none(_first_present(payload.get("duration_ms"), payload.get("durationMs"))),
    )
    session_id = payload.get("session_id") or payload.get("sessionId")
    run_id = payload.get("run_id") or payload.get("runId")
    return ClaudeParseResult(
        response=response,
        usage=usage,
        observed_ids=observed_ids,
        session_id=session_id if isinstance(session_id, str) else None,
        run_id=run_id if isinstance(run_id, str) else None,
        applied_effort=applied_effort,
    )


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and not isinstance(value, bool):
            return value
    return None


def _extract_cost_usd(model_usage: Any, payload: Mapping[str, Any]) -> float | None:
    total = _float_or_none(
        _first_present(payload.get("total_cost_usd"), payload.get("cost_usd"))
    )
    if total is not None:
        return total
    if isinstance(model_usage, Mapping):
        for value in model_usage.values():
            if isinstance(value, Mapping):
                nested = _float_or_none(
                    _first_present(
                        value.get("costUSD"),
                        value.get("cost_usd"),
                        value.get("costUsd"),
                    )
                )
                if nested is not None:
                    return nested
    return None


def _extract_observed_model_ids(model_usage: Any, payload: Mapping[str, Any]) -> tuple[str, ...]:
    observed: set[str] = set()
    if isinstance(model_usage, Mapping):
        for key in ("model", "model_id", "modelId"):
            value = model_usage.get(key)
            if isinstance(value, str) and value.strip():
                observed.add(value.strip())
        nested = model_usage.get("models")
        if isinstance(nested, Mapping):
            observed.update(str(key) for key in nested)
    for key in ("model", "model_id", "modelId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            observed.add(value.strip())
    models = payload.get("models")
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, str) and entry.strip():
                observed.add(entry.strip())
    if not observed and isinstance(model_usage, Mapping):
        for key, value in model_usage.items():
            if re.fullmatch(r"[A-Za-z0-9._:-]+", str(key)) and isinstance(value, Mapping):
                observed.add(str(key))
    return tuple(sorted(observed))


def _lookup_usage(model_usage: Any, *keys: str) -> Any:
    if not isinstance(model_usage, Mapping):
        return None
    for key in keys:
        if key in model_usage:
            return model_usage[key]
    for value in model_usage.values():
        if isinstance(value, Mapping):
            nested = _lookup_usage(value, *keys)
            if nested is not None:
                return nested
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ClaudeProvider:
    network_required = True
    network_policy = "provider-api-only"

    def __init__(self, *, dry_run: bool = False, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS):
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.engine_version = claude_cli_version()
        self._provider_config = claude_provider_config(
            engine_version=self.engine_version,
            subprocess_timeout_seconds=self.timeout_seconds,
            cli_flags={
                "safe_mode": True,
                "strict_mcp_config": True,
                "setting_sources": [],
                "tools": [],
                "no_session_persistence": True,
                "effort_omitted": True,
            },
        )

    def preflight(self) -> None:
        preflight_claude_auth()

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult:
        del item_id, image_paths
        mcp_config_path = str(Path(workspace_dir) / ".empty-mcp-config.json")
        Path(mcp_config_path).write_text("{}", encoding="utf-8")
        command = build_claude_command(
            prompt_text=prompt_text,
            mcp_config_path=mcp_config_path,
            timeout_seconds=self.timeout_seconds,
        )
        if "--effort" in command:
            raise ClaudeProviderError("default Claude effort must omit --effort")
        if "--add-dir" in command:
            raise ClaudeProviderError("claude command must not include --add-dir")
        if self.dry_run:
            raise ClaudeProviderError("dry-run Claude scoring is disabled; use fake provider")
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=workspace_dir,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise ClaudeProviderError(completed.stderr.strip() or "claude scoring failed")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ClaudeProviderError("claude output is not valid JSON") from error
        parsed = parse_claude_json_output(envelope)
        if len(parsed.observed_ids) != 1:
            raise ClaudeProviderError("claude run must observe exactly one model identifier")
        return ProviderResult(
            response=parsed.response,
            requested_id=_CLAUDE_MODEL_ALIAS,
            observed_ids=parsed.observed_ids,
            requested_effort="default",
            applied_effort=parsed.applied_effort,
            effort_reporting="reported" if parsed.applied_effort is not None else "not_exposed",
            engine_name="claude-cli",
            engine_version=self.engine_version,
            run_id=parsed.run_id,
            session_id=parsed.session_id,
            usage=parsed.usage,
            provider_config=self._provider_config,
            raw_envelope=envelope,
            raw_envelope_digest=sha256_hex(canonical_private_json(envelope)),
        )


__all__ = [
    "ClaudeParseResult",
    "ClaudeProvider",
    "ClaudeProviderError",
    "build_claude_command",
    "claude_cli_version",
    "parse_claude_json_output",
    "preflight_claude_auth",
]
