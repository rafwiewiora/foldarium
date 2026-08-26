"""Provider adapters for weekly LLM scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ProviderUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    charged_cents: float | None = None
    raw_cost_cents: float | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ProviderResult:
    response: dict[str, Any]
    requested_id: str
    observed_ids: tuple[str, ...]
    requested_effort: str
    applied_effort: str | None
    effort_reporting: str
    engine_name: str
    engine_version: str
    run_id: str | None
    session_id: str | None
    usage: ProviderUsage
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    raw_envelope: Mapping[str, Any] = field(repr=False, default_factory=dict)
    raw_envelope_digest: str | None = None


class WeeklyLlmProvider(Protocol):
    network_required: bool
    network_policy: str

    def preflight(self) -> None: ...

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult: ...


__all__ = [
    "ProviderResult",
    "ProviderUsage",
    "WeeklyLlmProvider",
]
