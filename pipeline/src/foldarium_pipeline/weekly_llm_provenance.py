"""Provenance manifests and digests for weekly LLM scoring."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .weekly_llm_contract import sha256_hex
from .weekly_selector import canonical_json
from .weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA, SELECTOR_PROMPT_SHA256

MODEL_RESPONSE_MANIFEST_SCHEMA = "foldarium.selector-model-response-manifest/v1"
INPUT_MANIFEST_SCHEMA = "foldarium.selector-input-manifest/v2"
RUNTIME_MANIFEST_SCHEMA = "foldarium.selector-runtime-manifest/v1"


def response_schema_digest() -> str:
    return sha256_hex(SELECTOR_MODEL_RESPONSE_SCHEMA)


def canonical_tools_manifest() -> list[str]:
    return []


def tools_sha256() -> str:
    return sha256_hex(canonical_tools_manifest())


def build_output_manifest(*, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": MODEL_RESPONSE_MANIFEST_SCHEMA,
        "items": sorted(
            [
                {
                    "item_id": item["item_id"],
                    "response_sha256": item["response_sha256"],
                    "validated_response_artifact": item["validated_response_artifact"],
                }
                for item in items
            ],
            key=lambda row: row["item_id"],
        ),
    }


def build_input_manifest(
    *,
    prompt_profile_id: str,
    kit_zip_sha256: str,
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": INPUT_MANIFEST_SCHEMA,
        "prompt_profile_id": prompt_profile_id,
        "prompt_sha256": SELECTOR_PROMPT_SHA256,
        "kit_zip_sha256": kit_zip_sha256,
        "items": sorted(list(items), key=lambda row: row["item_id"]),
    }


def build_runtime_manifest(*, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_MANIFEST_SCHEMA,
        "items": sorted(list(items), key=lambda row: row["item_id"]),
    }


def digest_manifest(manifest: Mapping[str, Any]) -> str:
    return sha256_hex(manifest)


def serialize_private_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): serialize_private_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_private_value(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return serialize_private_value(asdict(value))
    if isinstance(value, set):
        return sorted(serialize_private_value(item) for item in value)
    return str(value)


def canonical_private_json(value: Any) -> str:
    return canonical_json(serialize_private_value(value))


__all__ = [
    "INPUT_MANIFEST_SCHEMA",
    "MODEL_RESPONSE_MANIFEST_SCHEMA",
    "RUNTIME_MANIFEST_SCHEMA",
    "build_input_manifest",
    "build_output_manifest",
    "build_runtime_manifest",
    "canonical_private_json",
    "digest_manifest",
    "response_schema_digest",
    "tools_sha256",
]
