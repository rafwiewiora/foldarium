"""Claude Code CLI adapter for weekly selector scoring."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage
from ..weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA, SELECTOR_SYSTEM_PROMPT

_CLAUDE_MODEL_ALIAS = "opus"


class ClaudeProviderError(RuntimeError):
    """Raised when Claude CLI preflight or scoring fails."""


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
    workspace_dir: str,
    mcp_config_path: str,
) -> list[str]:
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
        "--tools",
        "",
        "--no-session-persistence",
        "--add-dir",
        workspace_dir,
        prompt_text,
    ]


def parse_claude_json_output(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ProviderUsage, tuple[str, ...], str | None, str | None]:
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

    model_usage = payload.get("modelUsage") or payload.get("usage") or {}
    observed_ids = _extract_observed_model_ids(model_usage, payload)
    applied_effort = None
    effort_reporting = "not_exposed"
    if isinstance(payload.get("effort"), str):
        applied_effort = payload["effort"]
        effort_reporting = "reported"
    elif isinstance(model_usage, Mapping) and isinstance(model_usage.get("effort"), str):
        applied_effort = model_usage["effort"]
        effort_reporting = "reported"

    usage = ProviderUsage(
        input_tokens=_int_or_none(_lookup_usage(model_usage, "input_tokens", "inputTokens")),
        output_tokens=_int_or_none(_lookup_usage(model_usage, "output_tokens", "outputTokens")),
        cache_read_tokens=_int_or_none(_lookup_usage(model_usage, "cache_read_tokens", "cacheReadTokens")),
        cache_creation_tokens=_int_or_none(
            _lookup_usage(model_usage, "cache_creation_tokens", "cacheCreationTokens")
        ),
        reasoning_tokens=_int_or_none(_lookup_usage(model_usage, "reasoning_tokens", "reasoningTokens")),
        cost_usd=_float_or_none(payload.get("total_cost_usd") or payload.get("cost_usd")),
        duration_ms=_int_or_none(payload.get("duration_ms") or payload.get("durationMs")),
    )
    session_id = payload.get("session_id") or payload.get("sessionId")
    run_id = payload.get("run_id") or payload.get("runId")
    return response, usage, observed_ids, session_id, applied_effort if effort_reporting == "reported" else None


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
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ClaudeProvider:
    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.engine_version = claude_cli_version()

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
        del item_id, image_paths  # Claude scoring uses text-only canonical prompt/evidence.
        mcp_config_path = str(Path(workspace_dir) / ".empty-mcp-config.json")
        Path(mcp_config_path).write_text("{}", encoding="utf-8")
        command = build_claude_command(
            prompt_text=prompt_text,
            workspace_dir=workspace_dir,
            mcp_config_path=mcp_config_path,
        )
        if "--effort" in command:
            raise ClaudeProviderError("default Claude effort must omit --effort")
        if self.dry_run:
            raise ClaudeProviderError("dry-run Claude scoring is disabled; use fake provider")
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=workspace_dir,
        )
        if completed.returncode != 0:
            raise ClaudeProviderError(completed.stderr.strip() or "claude scoring failed")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ClaudeProviderError("claude output is not valid JSON") from error
        response, usage, observed_ids, session_id, applied_effort = parse_claude_json_output(envelope)
        return ProviderResult(
            response=response,
            requested_id=_CLAUDE_MODEL_ALIAS,
            observed_ids=observed_ids,
            requested_effort="default",
            applied_effort=applied_effort,
            effort_reporting="reported" if applied_effort is not None else "not_exposed",
            engine_name="claude-cli",
            engine_version=self.engine_version,
            run_id=None,
            session_id=session_id if isinstance(session_id, str) else None,
            usage=usage,
            raw_envelope=envelope,
        )


__all__ = [
    "ClaudeProvider",
    "ClaudeProviderError",
    "build_claude_command",
    "claude_cli_version",
    "parse_claude_json_output",
    "preflight_claude_auth",
]
