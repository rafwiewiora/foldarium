"""Cursor Python SDK adapter for weekly selector scoring."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage

try:
    from cursor_sdk import Agent, AgentOptions, Cursor, LocalAgentOptions, SDKImage, UserMessage
    from cursor_sdk.types import ModelParameterDefinition, ModelParameterValue, ModelSelection, ModelVariant, SDKModel
except ImportError:  # pragma: no cover - optional dependency
    Agent = None  # type: ignore[assignment,misc]
    AgentOptions = None  # type: ignore[assignment,misc]
    Cursor = None  # type: ignore[assignment,misc]
    LocalAgentOptions = None  # type: ignore[assignment,misc]
    SDKImage = None  # type: ignore[assignment,misc]
    UserMessage = None  # type: ignore[assignment,misc]
    ModelParameterDefinition = object  # type: ignore[assignment,misc]
    ModelParameterValue = object  # type: ignore[assignment,misc]
    ModelSelection = object  # type: ignore[assignment,misc]
    ModelVariant = object  # type: ignore[assignment,misc]
    SDKModel = object  # type: ignore[assignment,misc]

CURSOR_SDK_PACKAGE = "cursor-sdk"
CURSOR_SDK_VERSION = "1.0.28"
SOL_DISPLAY_NEEDLE = "gpt-5.6 sol"
HIGH_EFFORT_NEEDLE = "high"


class CursorProviderError(RuntimeError):
    """Raised when Cursor SDK preflight or scoring fails."""


def require_cursor_sdk() -> None:
    if Cursor is None:
        raise CursorProviderError(
            f"{CURSOR_SDK_PACKAGE}=={CURSOR_SDK_VERSION} is required for Cursor scoring"
        )


def preflight_cursor_api_key() -> None:
    require_cursor_sdk()
    if not os.environ.get("CURSOR_API_KEY", "").strip():
        raise CursorProviderError("CURSOR_API_KEY must be set for Cursor scoring")


def list_cursor_models(*, api_key: str | None = None) -> list[SDKModel]:
    require_cursor_sdk()
    preflight_cursor_api_key()
    key = api_key or os.environ["CURSOR_API_KEY"].strip()
    models = Cursor.models.list(api_key=key)
    return list(models)


def resolve_sol_high_model(models: Sequence[SDKModel]) -> tuple[str, list[ModelParameterValue]]:
    candidates: list[SDKModel] = []
    for model in models:
        haystack = f"{model.id} {model.display_name}".lower()
        if "gpt-5.6" in haystack and "sol" in haystack:
            candidates.append(model)
    if len(candidates) != 1:
        raise CursorProviderError("exact accessible GPT-5.6 Sol model could not be resolved")
    model = candidates[0]
    params = _resolve_high_reasoning_parameters(model)
    return model.id, params


def _resolve_high_reasoning_parameters(model: SDKModel) -> list[ModelParameterValue]:
    for parameter in model.parameters:
        haystack = f"{parameter.id} {parameter.display_name}".lower()
        if "reason" in haystack or "effort" in haystack:
            for value in parameter.values:
                label = f"{value.value} {value.display_name}".lower()
                if HIGH_EFFORT_NEEDLE in label:
                    return [ModelParameterValue(id=parameter.id, value=value.value)]
            high_values = [
                value
                for value in parameter.values
                if HIGH_EFFORT_NEEDLE in f"{value.value} {value.display_name}".lower()
            ]
            if len(high_values) == 1:
                value = high_values[0]
                return [ModelParameterValue(id=parameter.id, value=value.value)]
    for variant in model.variants:
        if HIGH_EFFORT_NEEDLE in variant.display_name.lower() or any(
            HIGH_EFFORT_NEEDLE in param.value.lower() for param in variant.params
        ):
            return list(variant.params)
    raise CursorProviderError("exact high-reasoning parameter for GPT-5.6 Sol could not be resolved")


class CursorProvider:
    def __init__(self, *, api_key: str | None = None, dry_run: bool = False):
        require_cursor_sdk()
        self.api_key = (api_key or os.environ.get("CURSOR_API_KEY", "")).strip()
        self.dry_run = dry_run
        self.engine_version = CURSOR_SDK_VERSION
        self._model_id: str | None = None
        self._model_params: list[Any] | None = None

    def preflight(self) -> None:
        preflight_cursor_api_key()
        models = list_cursor_models(api_key=self.api_key)
        self._model_id, self._model_params = resolve_sol_high_model(models)

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult:
        del item_id
        if self._model_id is None or self._model_params is None:
            self.preflight()
        if self.dry_run:
            raise CursorProviderError("dry-run Cursor scoring is disabled; use fake provider")
        images = [SDKImage.from_file(path) for path in image_paths]
        message = UserMessage(text=prompt_text, images=images)
        result = Agent.prompt(
            message,
            AgentOptions(
                api_key=self.api_key,
                model=ModelSelection(id=self._model_id, params=self._model_params),
                local=LocalAgentOptions(cwd=workspace_dir, setting_sources=[]),
                tools=[],
            ),
        )
        if result.status != "finished":
            raise CursorProviderError(f"cursor run failed with status {result.status}")
        try:
            response = json.loads(result.result)
        except json.JSONDecodeError as error:
            raise CursorProviderError("cursor result is not valid JSON") from error
        observed_ids = _observed_model_ids(result)
        usage = _provider_usage(result)
        applied_effort = _applied_effort(result)
        return ProviderResult(
            response=response,
            requested_id=f"{self._model_id}-high",
            observed_ids=observed_ids,
            requested_effort="high",
            applied_effort=applied_effort,
            effort_reporting="reported" if applied_effort is not None else "not_exposed",
            engine_name="cursor-sdk",
            engine_version=self.engine_version,
            run_id=result.id,
            session_id=result.agent_id,
            usage=usage,
            raw_envelope={
                "run_id": result.id,
                "agent_id": result.agent_id,
                "status": result.status,
                "model": result.model.model_dump() if hasattr(result.model, "model_dump") else result.model,
                "usage": result.usage.model_dump() if result.usage and hasattr(result.usage, "model_dump") else result.usage,
            },
        )


def _observed_model_ids(result: Any) -> tuple[str, ...]:
    observed: set[str] = set()
    if result.model is not None:
        model_id = getattr(result.model, "id", None) or getattr(result.model, "model", None)
        if isinstance(model_id, str) and model_id.strip():
            observed.add(model_id.strip())
    if len(observed) != 1:
        raise CursorProviderError("cursor run must observe exactly one model identifier")
    return tuple(sorted(observed))


def _provider_usage(result: Any) -> ProviderUsage:
    usage = result.usage
    if usage is None:
        return ProviderUsage(duration_ms=result.duration_ms)
    return ProviderUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_read_tokens=getattr(usage, "cache_read_tokens", None),
        cache_creation_tokens=getattr(usage, "cache_write_tokens", None),
        reasoning_tokens=getattr(usage, "reasoning_tokens", None),
        cost_usd=None,
        duration_ms=result.duration_ms,
    )


def _applied_effort(result: Any) -> str | None:
    model = result.model
    if model is None:
        return None
    params = getattr(model, "params", None) or []
    for param in params:
        value = getattr(param, "value", None)
        if isinstance(value, str) and HIGH_EFFORT_NEEDLE in value.lower():
            return "high"
    return "high"


__all__ = [
    "CURSOR_SDK_VERSION",
    "CursorProvider",
    "CursorProviderError",
    "list_cursor_models",
    "preflight_cursor_api_key",
    "resolve_sol_high_model",
]
