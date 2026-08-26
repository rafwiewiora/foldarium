"""Cursor Python SDK adapter for weekly selector scoring."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage
from ..weekly_llm_catalog import (
    CatalogModel,
    catalog_model_from_mapping,
    resolve_sol_high_model,
)
from ..weekly_llm_config import cursor_provider_config
from ..weekly_llm_contract import sha256_hex
from ..weekly_llm_provenance import canonical_private_json
from ..weekly_selector_prompt import SELECTOR_SYSTEM_PROMPT

try:
    from cursor_sdk import Agent, AgentOptions, Cursor, LocalAgentOptions, SDKImage, UserMessage
    from cursor_sdk.types import ModelParameterValue, ModelSelection
except ImportError:  # pragma: no cover - optional dependency
    Agent = None  # type: ignore[assignment,misc]
    AgentOptions = None  # type: ignore[assignment,misc]
    Cursor = None  # type: ignore[assignment,misc]
    LocalAgentOptions = None  # type: ignore[assignment,misc]
    SDKImage = None  # type: ignore[assignment,misc]
    UserMessage = None  # type: ignore[assignment,misc]
    ModelParameterValue = None  # type: ignore[assignment,misc]
    ModelSelection = None  # type: ignore[assignment,misc]

CURSOR_SDK_PACKAGE = "cursor-sdk"
CURSOR_SDK_VERSION = "1.0.28"
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


def list_cursor_models(*, api_key: str | None = None) -> list[CatalogModel]:
    require_cursor_sdk()
    preflight_cursor_api_key()
    key = api_key or os.environ["CURSOR_API_KEY"].strip()
    return [catalog_model_from_mapping(model) for model in Cursor.models.list(api_key=key)]


def serialize_sdk_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return serialize_sdk_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): serialize_sdk_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_sdk_value(item) for item in value]
    if callable(value):
        return None
    return str(value)


def build_cursor_user_message(*, item_prompt_text: str) -> str:
    return (
        "SYSTEM PROMPT (canonical; Cursor SDK cannot set a proprietary system role):\n"
        f"{SELECTOR_SYSTEM_PROMPT.strip()}\n\n"
        "ITEM REQUEST:\n"
        f"{item_prompt_text.strip()}"
    )


def _sdk_model_params(params: Sequence[Any]) -> list[Any]:
    return [ModelParameterValue(id=param.id, value=param.value) for param in params]


def _applied_effort_from_model(model: Any, selected_params: Sequence[Any]) -> str | None:
    if model is None:
        return None
    selected = {param.id: param.value for param in selected_params}
    params = getattr(model, "params", None) or []
    for param in params:
        param_id = getattr(param, "id", None)
        value = getattr(param, "value", None)
        if param_id in selected and isinstance(value, str) and value == selected[param_id]:
            if HIGH_EFFORT_NEEDLE in value.lower():
                return "high"
    return None


def _observed_model_ids(result: Any) -> tuple[str, ...]:
    observed: set[str] = set()
    if result.model is not None:
        model_id = getattr(result.model, "id", None)
        if isinstance(model_id, str) and model_id.strip():
            observed.add(model_id.strip())
    if len(observed) != 1:
        raise CursorProviderError("cursor run must observe exactly one model identifier")
    return tuple(sorted(observed))


def _extract_billed_cost(
    billed: Any,
) -> tuple[float | None, float | None, float | None]:
    cost = getattr(billed, "cost", None)
    if cost is None:
        return None, None, None
    charged = getattr(cost, "charged_cents", None)
    raw = getattr(cost, "raw_cost_cents", None)
    if (
        isinstance(charged, (int, float))
        and not isinstance(charged, bool)
        and math.isfinite(float(charged))
        and charged >= 0
    ):
        charged_value = float(charged)
        raw_value = (
            float(raw)
            if isinstance(raw, (int, float))
            and not isinstance(raw, bool)
            and math.isfinite(float(raw))
            and raw >= 0
            else None
        )
        return charged_value / 100.0, charged_value, raw_value
    return None, None, None


def _provider_usage(result: Any, billed_usage: Any | None) -> ProviderUsage:
    usage = result.usage
    cost_usd = None
    charged_cents = None
    raw_cost_cents = None
    if billed_usage is not None:
        cost_usd, charged_cents, raw_cost_cents = _extract_billed_cost(billed_usage)
    if usage is None:
        return ProviderUsage(
            duration_ms=result.duration_ms,
            cost_usd=cost_usd,
            charged_cents=charged_cents,
            raw_cost_cents=raw_cost_cents,
        )
    return ProviderUsage(
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        cache_read_tokens=getattr(usage, "cache_read_tokens", None),
        cache_creation_tokens=getattr(usage, "cache_write_tokens", None),
        reasoning_tokens=getattr(usage, "reasoning_tokens", None),
        cost_usd=cost_usd,
        charged_cents=charged_cents,
        raw_cost_cents=raw_cost_cents,
        duration_ms=result.duration_ms,
    )


class CursorProvider:
    network_required = True
    network_policy = "provider-api-only"

    def __init__(self, *, api_key: str | None = None, dry_run: bool = False):
        require_cursor_sdk()
        self.api_key = (api_key or os.environ.get("CURSOR_API_KEY", "")).strip()
        self.dry_run = dry_run
        self.engine_version = CURSOR_SDK_VERSION
        self._model_id: str | None = None
        self._model_params: tuple[Any, ...] | None = None
        self._provider_config: dict[str, Any] | None = None

    def preflight(self) -> None:
        preflight_cursor_api_key()
        models = list_cursor_models(api_key=self.api_key)
        self._model_id, self._model_params = resolve_sol_high_model(models)
        self._provider_config = cursor_provider_config(
            engine_version=self.engine_version,
            model_id=self._model_id,
            model_params=[{"id": param.id, "value": param.value} for param in self._model_params],
        )

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult:
        del item_id
        if self._model_id is None or self._model_params is None or self._provider_config is None:
            self.preflight()
        if self.dry_run:
            raise CursorProviderError("dry-run Cursor scoring is disabled; use fake provider")
        images = [SDKImage.from_file(path) for path in image_paths]
        message = UserMessage(text=build_cursor_user_message(item_prompt_text=prompt_text), images=images)
        sdk_params = _sdk_model_params(self._model_params)
        with Agent.create(
            AgentOptions(
                api_key=self.api_key,
                model=ModelSelection(id=self._model_id, params=sdk_params),
                local=LocalAgentOptions(cwd=workspace_dir, setting_sources=[]),
                tools=[],
            )
        ) as agent:
            run = agent.send(message)
            result = run.wait()
            if result.status != "finished":
                raise CursorProviderError(f"cursor run failed with status {result.status}")
            try:
                response = json.loads(result.result)
            except json.JSONDecodeError as error:
                raise CursorProviderError("cursor result is not valid JSON") from error
            observed_ids = _observed_model_ids(result)
            applied_effort = _applied_effort_from_model(result.model, self._model_params)
            billed_usage = None
            if hasattr(agent, "get_usage"):
                try:
                    billed_usage = agent.get_usage()
                except Exception:
                    billed_usage = None
            usage = _provider_usage(result, billed_usage)
            envelope = {
                "agent_id": result.agent_id,
                "run_id": result.id,
                "status": result.status,
                "model": serialize_sdk_value(result.model),
                "usage": serialize_sdk_value(result.usage),
                "billed_usage": serialize_sdk_value(billed_usage),
            }
            return ProviderResult(
                response=response,
                requested_id=self._model_id,
                observed_ids=observed_ids,
                requested_effort="high",
                applied_effort=applied_effort,
                effort_reporting="reported" if applied_effort is not None else "not_exposed",
                engine_name="cursor-sdk",
                engine_version=self.engine_version,
                run_id=result.id,
                session_id=result.agent_id,
                usage=usage,
                provider_config=self._provider_config or {},
                raw_envelope=envelope,
                raw_envelope_digest=sha256_hex(canonical_private_json(envelope)),
            )


__all__ = [
    "CURSOR_SDK_VERSION",
    "CursorProvider",
    "CursorProviderError",
    "build_cursor_user_message",
    "list_cursor_models",
    "preflight_cursor_api_key",
    "resolve_sol_high_model",
    "serialize_sdk_value",
]
