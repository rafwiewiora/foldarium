"""Direct blind Messages API adapter with durable, conservative spend reservations.

No automatic retries: an ambiguous paid request remains reserved and blocks replay.
The caller must serialize one execution and durably commit each ledger write before
network dispatch. The host must provide a verified durable-storage callback.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from datetime import datetime, timezone, date
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

from ..weekly_llm_contract import sha256_hex
from ..weekly_selector import canonical_json
from ..weekly_selector_prompt import SELECTOR_SYSTEM_PROMPT, SELECTOR_MODEL_RESPONSE_SCHEMA
from . import ProviderResult, ProviderUsage

ALLOWLIST = ["api.anthropic.com"]
API_VERSION = "2023-06-01"
ENGINE_VERSION = "foldarium-messages-api/v2"
WIRE_SCHEMA_POLICY = "disjoint-selection-unions-and-described-bounds/v1"
# Official 4.6+ dateless IDs are pinned snapshots. Add only explicitly reviewed
# IDs here; earlier short forms such as claude-sonnet-4-5 remain aliases.
REVIEWED_DATELESS_MODEL_IDS = frozenset({"claude-sonnet-4-6"})


def _pinned_model_id(value: Any) -> bool:
    return isinstance(value, str) and (value in REVIEWED_DATELESS_MODEL_IDS or bool(re.fullmatch(r"claude-[a-z0-9-]+-[0-9]{8}", value)))


class ApiBudgetError(RuntimeError):
    """A sanitized configuration, provenance, or budget failure."""


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ApiBudgetError(f"{name} must be a positive integer")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ApiBudgetError(f"{name} must be an explicit decimal string")
    try:
        result = Decimal(value)
    except Exception:
        raise ApiBudgetError(f"{name} must be a positive decimal") from None
    if not result.is_finite() or result <= 0:
        raise ApiBudgetError(f"{name} must be a positive decimal")
    return result


@dataclass(frozen=True)
class ApiConfig:
    model_id: str
    model_input_token_limit: int
    max_output_tokens: int
    input_usd_per_million_upper: str
    output_usd_per_million_upper: str
    max_cost_usd: str
    timeout_seconds: int
    rate_card_valid_until: str

    def __post_init__(self) -> None:
        if not _pinned_model_id(self.model_id):
            raise ApiBudgetError("an explicit pinned Claude model ID is required")
        for field in ("model_input_token_limit", "max_output_tokens", "timeout_seconds"):
            _positive_int(getattr(self, field), field)
        for field in ("input_usd_per_million_upper", "output_usd_per_million_upper", "max_cost_usd"):
            _decimal(getattr(self, field), field)
        # A frozen configuration also describes already completed work. Its
        # expiry disables new spend, not verification/recovery of that work.
        self._rate_review_expiry()
        if self.timeout_seconds > 1800:
            raise ApiBudgetError("timeout_seconds exceeds 1800")
        if self.reserve_micro_usd > self.budget_micro_usd:
            raise ApiBudgetError("budget cannot cover one conservatively bounded request")

    def _rate_review_expiry(self) -> date:
        try:
            expiry = date.fromisoformat(self.rate_card_valid_until)
        except Exception:
            raise ApiBudgetError("explicit ISO rate_card_valid_until is required") from None
        if expiry.isoformat() != self.rate_card_valid_until:
            raise ApiBudgetError("explicit ISO rate_card_valid_until is required")
        return expiry

    def validate_rate_review(self) -> None:
        if self._rate_review_expiry() < datetime.now(timezone.utc).date():
            raise ApiBudgetError("reviewed price card has expired; new paid calls are disabled")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ApiConfig":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise ApiBudgetError("API configuration requires exactly the documented fields")
        return cls(**raw)

    def cost_micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        # Tokens * dollars/million equals microdollars. Always round upward.
        value = input_tokens * _decimal(self.input_usd_per_million_upper, "input rate") + output_tokens * _decimal(self.output_usd_per_million_upper, "output rate")
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    @property
    def reserve_micro_usd(self) -> int:
        return self.cost_micro_usd(self.model_input_token_limit, self.max_output_tokens)

    @property
    def budget_micro_usd(self) -> int:
        # Never round the allowed budget upward.
        return int(_decimal(self.max_cost_usd, "budget") * 1_000_000)


def wire_response_schema(schema: Mapping[str, Any] = SELECTOR_MODEL_RESPONSE_SCHEMA) -> dict[str, Any]:
    """Provider grammar projection only; the original validator stays authoritative.

    Anthropic does not support oneOf or numeric/string bounds. Selection branches
    have distinct required const discriminators, making anyOf equivalent here.
    Bounds remain in the unchanged prompt and are described on the wire as the
    official SDK does. This projection is separately hashed in model provenance.
    Unknown schema vocabulary fails closed instead of silently dropping rules.
    """
    supported = {"type", "additionalProperties", "required", "const", "pattern", "description", "title"}
    bounds = {"minimum", "maximum", "minLength", "maxLength"}
    allowed = supported | bounds | {"$schema", "properties", "oneOf"}
    if set(schema) - allowed:
        raise ApiBudgetError("response schema needs a reviewed wire projection")
    result = {key: value for key, value in schema.items() if key in supported}
    constrained = {key: schema[key] for key in sorted(bounds) if key in schema}
    if constrained:
        result["description"] = (str(result.get("description", "")) + " Original response constraints: " + canonical_json(constrained)).strip()
    if "properties" in schema:
        result["properties"] = {key: wire_response_schema(value) for key, value in schema["properties"].items()}
    if "oneOf" in schema:
        branches = schema["oneOf"]
        discriminators = []
        for branch in branches:
            discriminator = branch.get("properties", {}).get("selection_kind", {}).get("const")
            if branch.get("type") != "object" or "selection_kind" not in branch.get("required", []) or not isinstance(discriminator, str):
                raise ApiBudgetError("response union is not provably disjoint")
            discriminators.append(discriminator)
        if len(set(discriminators)) != len(discriminators) or not discriminators:
            raise ApiBudgetError("response union is not provably disjoint")
        result["anyOf"] = [wire_response_schema(branch) for branch in branches]
    return result


def provider_config(config: ApiConfig) -> dict[str, Any]:
    return {
        "schema_version": "foldarium.anthropic-api-config/v2",
        **config.__dict__,
        "api_version": API_VERSION, "engine_version": ENGINE_VERSION,
        "thinking": "disabled", "tools": [], "browser_enabled": False,
        "web_search_enabled": False, "external_retrieval_enabled": False,
        "shared_cache_enabled": False, "network_policy": "provider-api-only",
        "concurrency": 1, "billable_retries": 0,
        "wire_prompt_policy": "frozen-system-and-item-plus-json-schema/v1",
        "response_schema_sha256": sha256_hex(SELECTOR_MODEL_RESPONSE_SCHEMA),
        "wire_response_format": "json_schema",
        "wire_response_schema_policy": WIRE_SCHEMA_POLICY,
        "wire_response_schema_sha256": sha256_hex(wire_response_schema()),
        "budget_policy": "full-model-input-ceiling-durable-reservation/v1",
        "usage_cost_basis": "configured-reviewed-upper-rates-not-provider-invoice",
    }


def config_sha256(config: ApiConfig) -> str:
    allowlist_bytes = (canonical_json(ALLOWLIST) + "\n").encode()
    return sha256_hex({**provider_config(config), "network_allowlist_sha256": sha256_hex(ALLOWLIST),
        "network_allowlist_artifact": {"path": "private/network-allowlist.json", "sha256": sha256_hex(allowlist_bytes), "bytes": len(allowlist_bytes)}})


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def initialize_budget_ledger(path: Path, config: ApiConfig, execution_id: str) -> None:
    """Create once, ONLY after the DB's unique first-launch grant is acknowledged.

    Never call on resume. The service-side launch anchor survives complete Volume
    loss; a consumed or ambiguously acknowledged grant cannot initialize again.
    The host commits the execution volume before starting any inference process.
    """
    value = {"schema_version": 1, "execution_id": execution_id, "config_sha256": config_sha256(config), "entries": {}}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ApiBudgetError("budget ledger already exists; initialization refused") from None
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_budget_ledger(path: Path, config: ApiConfig, execution_id: str) -> None:
    """Require the exact existing ledger, with no network or mutation."""
    AnthropicApiProvider(config=config, ledger_path=path, execution_id=execution_id,
        transport=lambda *args: None, durable_commit=lambda: None)._load()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ApiBudgetError("provider redirect refused")


class MessagesTransport:
    """Fixed origin; no proxy environment, redirects, SDK retries, or error bodies."""
    def __init__(self, api_key: str, timeout_seconds: int):
        if not api_key or not isinstance(api_key, str):
            raise ApiBudgetError("ANTHROPIC_API_KEY is required")
        self._key = api_key
        self.timeout = timeout_seconds
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def __call__(self, method: str, path: str, payload: Any = None) -> dict[str, Any]:
        if not (method == "GET" and path.startswith("/v1/models/") and _pinned_model_id(path.removeprefix("/v1/models/")) or method == "POST" and path == "/v1/messages"):
            raise ApiBudgetError("unapproved provider route")
        body = None if payload is None else canonical_json(payload).encode()
        request = Request("https://api.anthropic.com" + path, data=body, method=method,
            headers={"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("oversized response")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("nonobject response")
            return parsed
        except Exception:
            # HTTP error bodies/headers can echo sensitive input. Never log them.
            raise ApiBudgetError("provider request failed; paid outcome may be unknown") from None


class AnthropicApiProvider:
    network_required = True
    network_policy = "provider-api-only"

    def __init__(self, *, config: ApiConfig, ledger_path: Path, execution_id: str,
                 transport: Callable[..., dict[str, Any]], durable_commit: Callable[[], None]):
        self.config = config
        self.ledger_path = ledger_path
        self.execution_id = execution_id
        self.transport = transport
        self.durable_commit = durable_commit
        self.ready = False

    def _load(self) -> dict[str, Any]:
        if self.ledger_path.is_symlink() or not self.ledger_path.is_file():
            raise ApiBudgetError("initialized budget ledger is missing; automatic restart refused")
        try:
            ledger = json.loads(self.ledger_path.read_text())
            if ledger["schema_version"] != 1 or ledger["execution_id"] != self.execution_id or ledger["config_sha256"] != config_sha256(self.config) or not isinstance(ledger["entries"], dict):
                raise ValueError()
            for item_id, entry in ledger["entries"].items():
                if not isinstance(item_id, str) or not item_id or not isinstance(entry, dict) or entry.get("status") not in {"reserved", "succeeded"} or type(entry.get("cost_micro_usd")) is not int or not isinstance(entry.get("request_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["request_sha256"]):
                    raise ValueError()
                if entry["status"] == "reserved":
                    if entry["cost_micro_usd"] != self.config.reserve_micro_usd:
                        raise ValueError()
                else:
                    if sha256_hex(entry["envelope"]) != entry["envelope_sha256"]:
                        raise ValueError()
                    result = self._result(entry["envelope"])
                    if entry["cost_micro_usd"] != self.config.cost_micro_usd(result.usage.input_tokens, result.usage.output_tokens):
                        raise ValueError()
            if sum(entry["cost_micro_usd"] for entry in ledger["entries"].values()) > self.config.budget_micro_usd:
                raise ValueError()
            return ledger
        except Exception:
            raise ApiBudgetError("budget ledger is invalid or belongs to another execution/config") from None

    def _save(self, ledger: dict[str, Any]) -> None:
        if self.ledger_path.is_symlink() or not self.ledger_path.is_file():
            raise ApiBudgetError("initialized budget ledger disappeared; automatic restart refused")
        atomic_json(self.ledger_path, ledger)
        self.durable_commit()  # Failure propagates BEFORE any new paid dispatch.

    def preflight(self) -> None:
        self._load()
        metadata = self.transport("GET", "/v1/models/" + self.config.model_id)
        if metadata.get("id") != self.config.model_id:
            raise ApiBudgetError("provider resolved a different model")
        maximum = metadata.get("max_input_tokens")
        if type(maximum) is not int or maximum <= 0 or maximum > self.config.model_input_token_limit:
            raise ApiBudgetError("provider model input ceiling is absent or exceeds reviewed reservation")
        output_maximum = metadata.get("max_tokens")
        if type(output_maximum) is not int or self.config.max_output_tokens > output_maximum:
            raise ApiBudgetError("provider output ceiling does not support configured bound")
        capabilities = metadata.get("capabilities")
        if not isinstance(capabilities, dict) or any(not isinstance(capabilities.get(key), dict) or capabilities[key].get("supported") is not True for key in ("structured_outputs", "image_input")):
            raise ApiBudgetError("provider structured output and image capabilities are required")
        self.ready = True

    def _result(self, envelope: dict[str, Any]) -> ProviderResult:
        if envelope.get("model") != self.config.model_id or envelope.get("stop_reason") != "end_turn":
            raise ApiBudgetError("provider model or completion provenance is invalid")
        content = envelope.get("content")
        if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
            raise ApiBudgetError("provider response must contain only one JSON text block")
        try:
            response = json.loads(content[0]["text"])
            if not isinstance(response, dict):
                raise ValueError()
        except Exception:
            raise ApiBudgetError("provider did not return a JSON object") from None
        usage = envelope.get("usage", {})
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if type(input_tokens) is not int or not 0 <= input_tokens <= self.config.model_input_token_limit or type(output_tokens) is not int or not 0 <= output_tokens <= self.config.max_output_tokens:
            raise ApiBudgetError("provider usage is missing or exceeds the reserved token ceiling")
        if usage.get("cache_creation_input_tokens", 0) != 0 or usage.get("cache_read_input_tokens", 0) != 0:
            raise ApiBudgetError("unexpected provider cache accounting")
        return ProviderResult(response=response, requested_id=self.config.model_id, observed_ids=(self.config.model_id,),
            requested_effort="default", applied_effort=None, effort_reporting="not_exposed",
            engine_name="anthropic-messages-api", engine_version=ENGINE_VERSION,
            run_id=None, session_id=None,
            usage=ProviderUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                cost_usd=self.config.cost_micro_usd(input_tokens, output_tokens) / 1_000_000),
            provider_config=provider_config(self.config), raw_envelope=envelope, raw_envelope_digest=sha256_hex(envelope))

    def score_item(self, *, item_id: str, prompt_text: str, image_paths: Sequence[str], workspace_dir: str) -> ProviderResult:
        if not self.ready:
            raise ApiBudgetError("provider preflight is required")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text + "\n\nResponse schema:\n" + canonical_json(SELECTOR_MODEL_RESPONSE_SCHEMA)}]
        for image_path in image_paths:
            image = Path(image_path).read_bytes()
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 5 * 1024 * 1024:
                raise ApiBudgetError("evidence image must be a bounded PNG")
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(image).decode("ascii")}})
        payload = {"model": self.config.model_id, "max_tokens": self.config.max_output_tokens,
            "thinking": {"type": "disabled"}, "system": SELECTOR_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
            "output_config": {"format": {"type": "json_schema", "schema": wire_response_schema()}}}
        if len(image_paths) > 100 or len(canonical_json(payload).encode()) > 32 * 1024 * 1024:
            raise ApiBudgetError("item exceeds reviewed API request limits")
        request_digest = sha256_hex(payload)
        ledger = self._load()
        previous = ledger["entries"].get(item_id)
        if previous:
            if previous.get("request_sha256") != request_digest:
                raise ApiBudgetError("resumed item inputs changed")
            if previous["status"] != "succeeded":
                raise ApiBudgetError("prior paid outcome is uncertain; automatic retry refused")
            envelope = previous["envelope"]
            if sha256_hex(envelope) != previous["envelope_sha256"]:
                raise ApiBudgetError("cached provider envelope digest mismatch")
            return self._result(envelope)
        # Exact successful responses may be replayed after expiry without a
        # paid call. Every new request still needs current spending authority.
        self.config.validate_rate_review()
        spent = sum(entry["cost_micro_usd"] for entry in ledger["entries"].values())
        if spent + self.config.reserve_micro_usd > self.config.budget_micro_usd:
            raise ApiBudgetError("execution budget cannot cover the next bounded request")
        ledger["entries"][item_id] = {"request_sha256": request_digest, "status": "reserved", "cost_micro_usd": self.config.reserve_micro_usd}
        self._save(ledger)
        envelope = self.transport("POST", "/v1/messages", payload)
        # Validate accounting and provenance before releasing any reservation.
        result = self._result(envelope)
        ledger["entries"][item_id].update(status="succeeded", cost_micro_usd=self.config.cost_micro_usd(result.usage.input_tokens, result.usage.output_tokens),
            envelope=envelope, envelope_sha256=sha256_hex(envelope))
        self._save(ledger)
        return result
