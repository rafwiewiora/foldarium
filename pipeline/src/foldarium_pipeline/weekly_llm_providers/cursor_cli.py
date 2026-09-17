"""Cursor Agent CLI adapter for weekly selector scoring (CLI login fallback)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage
from .cursor import build_cursor_user_message
from ..weekly_llm_config import cursor_cli_provider_config
from ..weekly_llm_contract import sha256_hex
from ..weekly_llm_provenance import canonical_private_json
from ..weekly_selector_prompt import SELECTOR_SYSTEM_PROMPT

CURSOR_CLI_EXECUTABLE = "cursor-agent"
CURSOR_CLI_MODEL_ID = "gpt-5.6-sol-high"
CONTACT_SHEET_FILENAME = "contact_sheet.png"
_DEFAULT_TIMEOUT_SECONDS = 600

_ALLOWED_STREAM_EVENT_TYPES = frozenset(
    {"system", "user", "assistant", "thinking", "result", "tool_call"}
)
_EXPLICIT_FORBIDDEN_EVENT_TYPES = frozenset(
    {"mcp", "shell", "connection", "retry", "plan", "interaction_query"}
)
_TOOL_CALL_STARTED_KEYS = frozenset(
    {"readToolCall", "hookAdditionalContexts", "toolCallId", "startedAtMs"}
)
_TOOL_CALL_COMPLETED_KEYS = _TOOL_CALL_STARTED_KEYS | {"completedAtMs"}
_READ_TOOL_CALL_STARTED_KEYS = frozenset({"args"})
_READ_TOOL_CALL_COMPLETED_KEYS = frozenset({"args", "result"})
_READ_TOOL_ARGS_KEYS = frozenset({"path"})
_FORBIDDEN_NESTED_TOOL_CALLS = frozenset(
    {
        "globToolCall",
        "shellToolCall",
        "mcpToolCall",
        "webSearchToolCall",
        "grepToolCall",
        "listToolCall",
    }
)

_CONTACT_SHEET_TOOL_POLICY_LINES = (
    "CONTACT SHEET TOOL POLICY (hard constraint):",
    "- The only permitted tool action is Read on each exact contact_sheet.png path listed below.",
    "- Do not Glob, search, list, or Read any workspace, JSON, PDB, rules, kit, or other files.",
    "- Use the candidate evidence JSON already in this prompt; do not explore the workspace.",
)


class CursorCliProviderError(RuntimeError):
    """Raised when Cursor CLI preflight or scoring fails."""


@dataclass(frozen=True)
class CursorCliParseResult:
    response: dict[str, Any]
    usage: ProviderUsage
    observed_ids: tuple[str, ...]
    observed_model_label: str
    session_id: str | None
    run_id: str | None
    request_id: str | None
    applied_effort: str | None
    engine_version: str | None
    raw_events: tuple[Mapping[str, Any], ...]
    discarded_thinking_event_count: int = 0


def cursor_cli_version() -> str:
    executable = shutil.which(CURSOR_CLI_EXECUTABLE)
    if not executable:
        raise CursorCliProviderError(f"{CURSOR_CLI_EXECUTABLE} CLI is not installed")
    completed = subprocess.run(
        [executable, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CursorCliProviderError("unable to determine cursor-agent CLI version")
    version = (completed.stdout or completed.stderr).strip()
    if not version:
        raise CursorCliProviderError("cursor-agent CLI version is empty")
    return version


def preflight_cursor_cli_auth() -> dict[str, Any]:
    version = cursor_cli_version()
    executable = shutil.which(CURSOR_CLI_EXECUTABLE)
    assert executable is not None
    completed = subprocess.run(
        [executable, "status", "--format", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CursorCliProviderError(completed.stderr.strip() or "cursor-agent status failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CursorCliProviderError("cursor-agent status returned invalid JSON") from error
    if payload.get("isAuthenticated") is not True and payload.get("status") != "authenticated":
        raise CursorCliProviderError("cursor-agent status must show authenticated login")
    models = list_cursor_cli_model_ids()
    if CURSOR_CLI_MODEL_ID not in models:
        raise CursorCliProviderError(
            f"cursor-agent model list must include exact id {CURSOR_CLI_MODEL_ID}"
        )
    return {
        "logged_in": True,
        "engine_version": version,
        "model_id": CURSOR_CLI_MODEL_ID,
    }


def list_cursor_cli_model_ids(*, executable: str | None = None) -> list[str]:
    path = executable or shutil.which(CURSOR_CLI_EXECUTABLE)
    if not path:
        raise CursorCliProviderError(f"{CURSOR_CLI_EXECUTABLE} CLI is not installed")
    completed = subprocess.run(
        [path, "models"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise CursorCliProviderError(completed.stderr.strip() or "cursor-agent models failed")
    return parse_cursor_cli_models_text(completed.stdout)


def parse_cursor_cli_models_text(text: str) -> list[str]:
    model_ids: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("available models"):
            continue
        if " - " not in stripped:
            continue
        model_id, _display = stripped.split(" - ", 1)
        model_id = model_id.strip()
        if model_id:
            model_ids.append(model_id)
    return model_ids


def model_display_matches_sol_high(label: str) -> bool:
    if not isinstance(label, str) or not label.strip():
        return False
    lower = label.lower()
    if any(token in lower for token in ("auto", "fast", "xhigh", "extra high")):
        return False
    return "gpt-5.6" in lower and "sol" in lower and "high" in lower


def resolve_verified_contact_sheet_paths(image_paths: Sequence[str]) -> tuple[str, ...]:
    if not image_paths:
        raise CursorCliProviderError("cursor-cli requires verified contact sheet image paths")
    resolved: list[str] = []
    evidence_roots: set[str] = set()
    for raw_path in image_paths:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise CursorCliProviderError("contact sheet path must be a non-empty string")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise CursorCliProviderError(f"contact sheet path does not exist: {path}")
        if path.name != CONTACT_SHEET_FILENAME or path.suffix.lower() != ".png":
            raise CursorCliProviderError(
                f"contact sheet must be {CONTACT_SHEET_FILENAME} under generated evidence"
            )
        parts = path.parts
        if "evidence" not in parts:
            raise CursorCliProviderError("contact sheet must live under generated evidence output")
        evidence_index = parts.index("evidence")
        if len(parts) != evidence_index + 4:
            raise CursorCliProviderError(
                "contact sheet path must be evidence/<item_id>/<choice_id>/contact_sheet.png"
            )
        evidence_roots.add(str(Path(*parts[: evidence_index + 2])))
        resolved.append(str(path))
    if len(evidence_roots) != 1:
        raise CursorCliProviderError("contact sheets must share one item evidence directory")
    return tuple(sorted(resolved))


def cursor_cli_tools_manifest(*, engine_version: str) -> dict[str, Any]:
    return {
        "provider": "cursor-cli",
        "engine": CURSOR_CLI_EXECUTABLE,
        "engine_version": engine_version,
        "requested_model_id": CURSOR_CLI_MODEL_ID,
        "execution_mode": "ask",
        "sandbox": "enabled",
        "workspace_binding": "verified-item-workspace-only",
        "vision_mechanism": "allowlisted_readToolCall_on_verified_contact_sheets",
        "required_contact_sheet_filename": CONTACT_SHEET_FILENAME,
        "all_supplied_contact_sheets_required": True,
        "allowed_tool": "readToolCall",
        "forbidden_tools": [
            "mcp",
            "shell",
            "web_search",
            "external_retrieval",
            "unapproved_file_read",
        ],
        "tool_event_policy": "abort_on_unapproved_tool_or_failed_read",
        "declared_execution_surface": (
            "cursor-agent Ask mode with sandbox; only hash-verified generated contact-sheet "
            "readToolCall events are permitted; every supplied sheet must be read successfully"
        ),
        "prompt_composition": (
            "canonical_selector_system_prompt_plus_rendered_item_request_plus_at_path_image_refs"
        ),
        "external_urls_in_runner_prompt": False,
        "reference_data_in_runner_prompt": False,
        "answer_information_in_runner_prompt": False,
        "reasoning_trace_retained": False,
        "thinking_stream_events": "validated_then_discarded_from_private_envelope",
    }


def _append_contact_sheet_prompt_section(*, base_prompt: str, image_paths: Sequence[str]) -> str:
    lines = [
        "",
        *_CONTACT_SHEET_TOOL_POLICY_LINES,
        "",
        "CONTACT SHEET IMAGES (read exactly these verified generated files for vision):",
        *[f"@{path}" for path in image_paths],
    ]
    return base_prompt + "\n".join(lines)


def build_cursor_cli_command(
    *,
    prompt_text: str,
    workspace_dir: str,
    image_paths: Sequence[str],
) -> list[str]:
    executable = shutil.which(CURSOR_CLI_EXECUTABLE)
    if not executable:
        raise CursorCliProviderError(f"{CURSOR_CLI_EXECUTABLE} CLI is not installed")
    if SELECTOR_SYSTEM_PROMPT.strip() not in build_cursor_user_message(item_prompt_text=prompt_text):
        raise CursorCliProviderError("cursor-cli prompt must include canonical selector system prompt")
    resolved_paths = resolve_verified_contact_sheet_paths(image_paths)
    full_prompt = _append_contact_sheet_prompt_section(
        base_prompt=build_cursor_user_message(item_prompt_text=prompt_text),
        image_paths=resolved_paths,
    )
    add_dirs = sorted({str(Path(path).parent) for path in resolved_paths})
    command = [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--mode",
        "ask",
        "--model",
        CURSOR_CLI_MODEL_ID,
        "--sandbox",
        "enabled",
        "--trust",
        "--workspace",
        workspace_dir,
    ]
    for add_dir in add_dirs:
        command.extend(["--add-dir", add_dir])
    command.append(full_prompt)
    return command


def _resolve_tool_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).expanduser().resolve())


def _validate_empty_hook_contexts(value: Any) -> None:
    if isinstance(value, list) and len(value) == 0:
        return
    if isinstance(value, dict) and len(value) == 0:
        return
    raise CursorCliProviderError("cursor-agent hookAdditionalContexts must be empty")


def _validate_ms_timestamp(value: Any, *, field_name: str) -> None:
    if isinstance(value, bool):
        raise CursorCliProviderError(f"cursor-agent {field_name} must be a timestamp")
    if isinstance(value, int):
        if value < 0:
            raise CursorCliProviderError(f"cursor-agent {field_name} must be non-negative")
        return
    if isinstance(value, str):
        if not value or not value.isdecimal():
            raise CursorCliProviderError(f"cursor-agent {field_name} must be a timestamp")
        return
    raise CursorCliProviderError(f"cursor-agent {field_name} must be a timestamp")


def _validate_read_tool_success(*, success: Any, args_path: str) -> None:
    if not isinstance(success, Mapping) or not success:
        raise CursorCliProviderError("cursor-agent readToolCall result.success is invalid")
    if success.get("error") is not None:
        raise CursorCliProviderError("cursor-agent contact sheet read did not succeed")
    if "path" in success:
        success_path = _resolve_tool_path(success.get("path"))
        if success_path is None or success_path != args_path:
            raise CursorCliProviderError("cursor-agent contact sheet read returned wrong path")
        return
    if "error" in success:
        raise CursorCliProviderError("cursor-agent contact sheet read did not succeed")


def _validate_read_tool_call_event(
    event: Mapping[str, Any],
    *,
    subtype: str,
    allowed: frozenset[str],
) -> tuple[str, bool]:
    tool_call = event.get("tool_call")
    if not isinstance(tool_call, Mapping):
        raise CursorCliProviderError("cursor-agent tool_call payload is missing")
    for forbidden_tool in _FORBIDDEN_NESTED_TOOL_CALLS:
        if forbidden_tool in tool_call:
            raise CursorCliProviderError("cursor-agent stream contained forbidden tool capability event")
    keys = set(tool_call.keys())
    expected_keys = (
        _TOOL_CALL_STARTED_KEYS if subtype == "started" else _TOOL_CALL_COMPLETED_KEYS
    )
    if keys != expected_keys:
        raise CursorCliProviderError("cursor-agent tool_call metadata keys are invalid")
    _validate_empty_hook_contexts(tool_call.get("hookAdditionalContexts"))
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id.strip():
        raise CursorCliProviderError("cursor-agent toolCallId must be present")
    _validate_ms_timestamp(tool_call.get("startedAtMs"), field_name="startedAtMs")
    if subtype == "completed":
        _validate_ms_timestamp(tool_call.get("completedAtMs"), field_name="completedAtMs")
    read_call = tool_call.get("readToolCall")
    if not isinstance(read_call, Mapping):
        raise CursorCliProviderError("cursor-agent readToolCall payload is missing")
    expected_read_keys = (
        _READ_TOOL_CALL_STARTED_KEYS
        if subtype == "started"
        else _READ_TOOL_CALL_COMPLETED_KEYS
    )
    if set(read_call.keys()) != expected_read_keys:
        raise CursorCliProviderError("cursor-agent readToolCall keys are invalid")
    args = read_call.get("args")
    if not isinstance(args, Mapping) or set(args.keys()) != _READ_TOOL_ARGS_KEYS:
        raise CursorCliProviderError("cursor-agent readToolCall args.path is required")
    args_path = _resolve_tool_path(args.get("path"))
    if args_path is None:
        raise CursorCliProviderError("cursor-agent readToolCall args.path is invalid")
    if args_path not in allowed:
        raise CursorCliProviderError("cursor-agent attempted unapproved contact sheet read")
    if subtype == "completed":
        result = read_call.get("result")
        if not isinstance(result, Mapping) or set(result.keys()) != {"success"}:
            raise CursorCliProviderError("cursor-agent readToolCall result.success is required")
        if result.get("error") is not None:
            raise CursorCliProviderError("cursor-agent contact sheet read did not succeed")
        _validate_read_tool_success(success=result.get("success"), args_path=args_path)
        return args_path, True
    return args_path, False


def _validate_assistant_event(event: Mapping[str, Any]) -> None:
    message = event.get("message")
    if not isinstance(message, Mapping):
        raise CursorCliProviderError("cursor-agent assistant message is missing")
    if message.get("role") != "assistant":
        raise CursorCliProviderError("cursor-agent assistant role is invalid")
    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise CursorCliProviderError("cursor-agent assistant content is invalid")
    for block in content:
        if not isinstance(block, Mapping):
            raise CursorCliProviderError("cursor-agent assistant content block is invalid")
        if set(block.keys()) != {"type", "text"} or block.get("type") != "text":
            raise CursorCliProviderError("cursor-agent assistant content must be text-only")
        if not isinstance(block.get("text"), str):
            raise CursorCliProviderError("cursor-agent assistant text is invalid")


def _validate_non_tool_stream_event(event: Mapping[str, Any]) -> None:
    event_type = event.get("type")
    if not isinstance(event_type, str):
        raise CursorCliProviderError("cursor-agent stream event type is missing")
    if event_type in _EXPLICIT_FORBIDDEN_EVENT_TYPES:
        raise CursorCliProviderError("cursor-agent stream contained forbidden event type")
    if event_type not in _ALLOWED_STREAM_EVENT_TYPES:
        raise CursorCliProviderError("cursor-agent stream contained unknown event type")
    if event_type == "system":
        if event.get("subtype") != "init":
            raise CursorCliProviderError("cursor-agent system event must be init")
    elif event_type == "assistant":
        _validate_assistant_event(event)
    elif event_type == "thinking":
        subtype = event.get("subtype")
        if subtype not in {"delta", "completed"}:
            raise CursorCliProviderError("cursor-agent thinking subtype is invalid")
    elif event_type == "user":
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "user":
            raise CursorCliProviderError("cursor-agent user message is invalid")
    elif event_type == "result":
        return


def _collect_model_labels(event: Mapping[str, Any]) -> set[str]:
    labels: set[str] = set()
    for key in ("model", "model_id", "modelId", "model_name", "modelName"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            labels.add(value.strip())
    return labels


def parse_cursor_cli_stream(
    stdout: str,
    *,
    workspace_dir: str,
    allowed_image_paths: Sequence[str],
    requested_model_id: str = CURSOR_CLI_MODEL_ID,
) -> CursorCliParseResult:
    del requested_model_id
    workspace_resolved = str(Path(workspace_dir).resolve())
    allowed = frozenset(resolve_verified_contact_sheet_paths(allowed_image_paths))
    init_events: list[Mapping[str, Any]] = []
    result_events: list[Mapping[str, Any]] = []
    parsed_events: list[Mapping[str, Any]] = []
    observed_labels: set[str] = set()
    engine_version: str | None = None
    successful_reads: set[str] = set()
    discarded_thinking_event_count = 0

    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise CursorCliProviderError(
                f"cursor-agent stream line {line_number} is not valid JSON"
            ) from error
        if not isinstance(event, Mapping):
            raise CursorCliProviderError(
                f"cursor-agent stream line {line_number} must be a JSON object"
            )
        event_dict = dict(event)
        event_type = event_dict.get("type")
        if event_type == "thinking":
            _validate_non_tool_stream_event(event_dict)
            discarded_thinking_event_count += 1
            observed_labels.update(_collect_model_labels(event_dict))
            continue
        parsed_events.append(event_dict)
        if event_type == "tool_call":
            subtype = event_dict.get("subtype")
            if subtype not in {"started", "completed"}:
                raise CursorCliProviderError("cursor-agent tool_call subtype is invalid")
            _args_path, read_succeeded = _validate_read_tool_call_event(
                event_dict,
                subtype=str(subtype),
                allowed=allowed,
            )
            if subtype == "completed" and read_succeeded:
                successful_reads.add(_args_path)
        else:
            _validate_non_tool_stream_event(event_dict)
        observed_labels.update(_collect_model_labels(event_dict))
        if event_dict.get("type") == "system" and event_dict.get("subtype") == "init":
            init_events.append(event_dict)
        if event_dict.get("type") == "result":
            result_events.append(event_dict)
        version_value = event_dict.get("version")
        if isinstance(version_value, str) and version_value.strip():
            engine_version = version_value.strip()

    if allowed and successful_reads != allowed:
        raise CursorCliProviderError(
            "cursor-agent must successfully read every verified contact sheet"
        )

    if len(init_events) != 1:
        raise CursorCliProviderError("cursor-agent stream must contain exactly one init event")
    init = init_events[0]
    init_cwd = init.get("cwd")
    if not isinstance(init_cwd, str) or Path(init_cwd).resolve() != Path(workspace_resolved):
        raise CursorCliProviderError("cursor-agent init cwd must match item workspace")
    init_model_label = init.get("model")
    if not isinstance(init_model_label, str) or not model_display_matches_sol_high(init_model_label):
        raise CursorCliProviderError("cursor-agent init model label must match GPT-5.6 Sol High")
    matching_labels = {label for label in observed_labels if model_display_matches_sol_high(label)}
    if len(matching_labels) != 1 or init_model_label not in matching_labels:
        raise CursorCliProviderError(
            "cursor-agent stream must observe exactly one GPT-5.6 Sol High model label"
        )
    forbidden_extra = {
        label
        for label in observed_labels
        if label != init_model_label and re.fullmatch(r"[A-Za-z0-9._:-]+", label)
    }
    if forbidden_extra:
        raise CursorCliProviderError("cursor-agent stream must not observe multiple model identifiers")

    success_results = [
        event
        for event in result_events
        if event.get("subtype") == "success" and event.get("is_error") is not True
    ]
    if len(success_results) != 1:
        raise CursorCliProviderError("cursor-agent stream must contain exactly one successful result event")
    result_event = success_results[0]
    result_raw = result_event.get("result")
    if not isinstance(result_raw, str) or not result_raw.strip():
        raise CursorCliProviderError("cursor-agent result payload is empty")
    try:
        response = json.loads(result_raw)
    except json.JSONDecodeError as error:
        raise CursorCliProviderError("cursor-agent result is not valid JSON") from error
    if not isinstance(response, Mapping):
        raise CursorCliProviderError("cursor-agent result JSON must be an object")

    usage_payload = result_event.get("usage")
    usage = _provider_usage_from_event(result_event, usage_payload)
    session_id = _str_or_none(init.get("session_id") or result_event.get("session_id"))
    request_id = _str_or_none(result_event.get("request_id"))
    run_id = request_id
    applied_effort = "high" if "high" in init_model_label.lower() else None

    return CursorCliParseResult(
        response=dict(response),
        usage=usage,
        observed_ids=(init_model_label,),
        observed_model_label=init_model_label,
        session_id=session_id,
        run_id=run_id,
        request_id=request_id,
        applied_effort=applied_effort,
        engine_version=engine_version,
        raw_events=tuple(parsed_events),
        discarded_thinking_event_count=discarded_thinking_event_count,
    )


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _provider_usage_from_event(
    result_event: Mapping[str, Any],
    usage_payload: Any,
) -> ProviderUsage:
    usage_mapping = usage_payload if isinstance(usage_payload, Mapping) else {}
    return ProviderUsage(
        input_tokens=_int_or_none(
            usage_mapping.get("input_tokens") or usage_mapping.get("inputTokens")
        ),
        output_tokens=_int_or_none(
            usage_mapping.get("output_tokens") or usage_mapping.get("outputTokens")
        ),
        cache_read_tokens=_int_or_none(
            usage_mapping.get("cache_read_tokens") or usage_mapping.get("cacheReadTokens")
        ),
        cache_creation_tokens=_int_or_none(
            usage_mapping.get("cache_creation_tokens") or usage_mapping.get("cacheCreationTokens")
        ),
        reasoning_tokens=_int_or_none(
            usage_mapping.get("reasoning_tokens") or usage_mapping.get("reasoningTokens")
        ),
        duration_ms=_int_or_none(result_event.get("duration_ms") or result_event.get("durationMs")),
    )


class CursorCliProvider:
    network_required = True
    network_policy = "provider-api-only"

    def __init__(self, *, dry_run: bool = False, timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS):
        self.dry_run = dry_run
        self.timeout_seconds = timeout_seconds
        self.engine_version = cursor_cli_version()
        self._tools_manifest = cursor_cli_tools_manifest(engine_version=self.engine_version)
        self._provider_config = cursor_cli_provider_config(
            engine_version=self.engine_version,
            model_id=CURSOR_CLI_MODEL_ID,
            cli_flags={
                "output_format": "stream-json",
                "mode": "ask",
                "sandbox": "enabled",
                "trust": True,
                "add_dir": "verified_contact_sheet_parents_only",
            },
        )

    def preflight(self) -> None:
        preflight_cursor_cli_auth()

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult:
        del item_id
        resolved_paths = resolve_verified_contact_sheet_paths(image_paths)
        command = build_cursor_cli_command(
            prompt_text=prompt_text,
            workspace_dir=workspace_dir,
            image_paths=resolved_paths,
        )
        forbidden = {"fast", "xhigh", "auto"}
        model_flag_index = command.index("--model") + 1
        if command[model_flag_index] != CURSOR_CLI_MODEL_ID:
            raise CursorCliProviderError("cursor-cli must request exact gpt-5.6-sol-high model id")
        if any(token in command[model_flag_index] for token in forbidden):
            raise CursorCliProviderError("cursor-cli must not use fast/xhigh/auto model aliases")
        if self.dry_run:
            raise CursorCliProviderError("dry-run cursor-cli scoring is disabled; use fake provider")
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=workspace_dir,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise CursorCliProviderError(completed.stderr.strip() or "cursor-agent scoring failed")
        parsed = parse_cursor_cli_stream(
            completed.stdout,
            workspace_dir=workspace_dir,
            allowed_image_paths=resolved_paths,
        )
        envelope = {
            "events": [dict(event) for event in parsed.raw_events],
            "requested_model_id": CURSOR_CLI_MODEL_ID,
            "observed_model_label": parsed.observed_model_label,
            "allowed_contact_sheet_paths": list(resolved_paths),
            "request_id": parsed.request_id,
            "session_id": parsed.session_id,
            "engine_version": parsed.engine_version or self.engine_version,
            "reasoning_trace_retained": False,
            "discarded_thinking_event_count": parsed.discarded_thinking_event_count,
            "usage": {
                "input_tokens": parsed.usage.input_tokens,
                "output_tokens": parsed.usage.output_tokens,
                "cache_read_tokens": parsed.usage.cache_read_tokens,
                "cache_creation_tokens": parsed.usage.cache_creation_tokens,
                "reasoning_tokens": parsed.usage.reasoning_tokens,
                "duration_ms": parsed.usage.duration_ms,
            },
        }
        return ProviderResult(
            response=parsed.response,
            requested_id=CURSOR_CLI_MODEL_ID,
            observed_ids=parsed.observed_ids,
            requested_effort="high",
            applied_effort=parsed.applied_effort,
            effort_reporting="reported" if parsed.applied_effort is not None else "not_exposed",
            engine_name="cursor-agent",
            engine_version=parsed.engine_version or self.engine_version,
            run_id=parsed.run_id,
            session_id=parsed.session_id,
            usage=parsed.usage,
            provider_config=self._provider_config,
            tools_manifest=self._tools_manifest,
            raw_envelope=envelope,
            raw_envelope_digest=sha256_hex(canonical_private_json(envelope)),
        )


__all__ = [
    "CONTACT_SHEET_FILENAME",
    "CURSOR_CLI_MODEL_ID",
    "CursorCliParseResult",
    "CursorCliProvider",
    "CursorCliProviderError",
    "build_cursor_cli_command",
    "cursor_cli_tools_manifest",
    "cursor_cli_version",
    "list_cursor_cli_model_ids",
    "model_display_matches_sol_high",
    "parse_cursor_cli_models_text",
    "parse_cursor_cli_stream",
    "preflight_cursor_cli_auth",
    "resolve_verified_contact_sheet_paths",
]
