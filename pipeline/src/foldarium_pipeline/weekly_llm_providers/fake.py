"""Deterministic fake provider for tests and dry runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import ProviderResult, ProviderUsage
from ..weekly_llm_config import fake_provider_config
from ..weekly_llm_contract import sha256_hex
from ..weekly_llm_provenance import canonical_private_json
from ..weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION


class FakeProvider:
    network_required = False
    network_policy = "none"

    def __init__(self, *, fixture_path: str | Path | None = None, fixture: Mapping[str, Any] | None = None):
        if fixture_path is not None:
            loaded = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("fake provider fixture must be an object")
            fixture = loaded
        if fixture is None:
            raise ValueError("fake provider requires fixture or fixture_path")
        self._fixture = fixture
        self.engine_version = str(fixture.get("engine_version", "fake-1.0.0"))
        self._provider_config = fake_provider_config()

    def preflight(self) -> None:
        if self._fixture.get("preflight_error"):
            raise RuntimeError(str(self._fixture["preflight_error"]))

    def score_item(
        self,
        *,
        item_id: str,
        prompt_text: str,
        image_paths: Sequence[str],
        workspace_dir: str,
    ) -> ProviderResult:
        del workspace_dir
        items = self._fixture.get("items")
        if not isinstance(items, dict):
            raise RuntimeError("fake provider fixture missing items map")
        item_fixture = items.get(item_id)
        if not isinstance(item_fixture, dict):
            raise RuntimeError(f"fake provider missing fixture for item {item_id}")
        if item_fixture.get("error"):
            raise RuntimeError(str(item_fixture["error"]))
        response = dict(item_fixture["response"])
        response.setdefault("schema_version", SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION)
        response.setdefault("item_id", item_id)
        observed = item_fixture.get("observed_ids", ["fake-model-stable"])
        if not isinstance(observed, list) or len(observed) != 1:
            raise RuntimeError("fake provider observed_ids must contain exactly one model")
        envelope = {
            "fixture": True,
            "item_id": item_id,
            "prompt_bytes": len(prompt_text),
            "images": len(image_paths),
        }
        return ProviderResult(
            response=response,
            requested_id=str(item_fixture.get("requested_id", "fake-model")),
            observed_ids=tuple(str(value) for value in observed),
            requested_effort=str(item_fixture.get("requested_effort", "default")),
            applied_effort=item_fixture.get("applied_effort"),
            effort_reporting=str(item_fixture.get("effort_reporting", "not_exposed")),
            engine_name=str(self._fixture.get("engine_name", "fake-provider")),
            engine_version=self.engine_version,
            run_id=item_fixture.get("run_id"),
            session_id=item_fixture.get("session_id"),
            usage=ProviderUsage(
                input_tokens=int(item_fixture.get("input_tokens", 100)),
                output_tokens=int(item_fixture.get("output_tokens", 50)),
                cache_read_tokens=0,
                cache_creation_tokens=0,
                reasoning_tokens=item_fixture.get("reasoning_tokens"),
                cost_usd=float(item_fixture.get("cost_usd", 0)),
                duration_ms=int(item_fixture.get("duration_ms", 100)),
            ),
            provider_config=self._provider_config,
            raw_envelope=envelope,
            raw_envelope_digest=sha256_hex(canonical_private_json(envelope)),
        )


__all__ = ["FakeProvider"]
