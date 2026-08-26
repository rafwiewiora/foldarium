"""Strict validation for canonical model responses."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from .weekly_selector import WeeklySelectorError, assert_no_forbidden_content
from .weekly_selector_prompt import SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class WeeklyLlmResponseError(WeeklySelectorError):
    """Raised when a provider response violates the canonical schema."""


def validate_model_response(
    raw: Mapping[str, Any],
    *,
    item_id: str,
    allowed_cluster_ids: set[str],
    allowed_choice_ids: set[str],
) -> dict[str, Any]:
    assert_no_forbidden_content(raw, path="model_response")
    _exact_keys(
        raw,
        {"schema_version", "item_id", "clustered", "unclustered"},
        "model_response",
    )
    if raw.get("schema_version") != SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION:
        raise WeeklyLlmResponseError("model_response.schema_version is invalid")
    if raw.get("item_id") != item_id:
        raise WeeklyLlmResponseError("model_response.item_id mismatch")
    clustered = _normalize_mode(
        raw.get("clustered"),
        mode="clustered",
        allowed_cluster_ids=allowed_cluster_ids,
        allowed_choice_ids=allowed_choice_ids,
    )
    unclustered = _normalize_mode(
        raw.get("unclustered"),
        mode="unclustered",
        allowed_cluster_ids=allowed_cluster_ids,
        allowed_choice_ids=allowed_choice_ids,
    )
    return {
        "schema_version": SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION,
        "item_id": item_id,
        "clustered": clustered,
        "unclustered": unclustered,
    }


def model_response_to_submission_item(validated: Mapping[str, Any]) -> dict[str, Any]:
    clustered = validated["clustered"]
    unclustered = validated["unclustered"]
    return {
        "item_id": validated["item_id"],
        "clustered": _submission_decision(clustered, mode="clustered"),
        "unclustered": _submission_decision(unclustered, mode="unclustered"),
    }


def _submission_decision(decision: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    if decision["selection_kind"] == "none":
        return {"selection_kind": "none"}
    if mode == "clustered":
        return {"selection_kind": "cluster", "cluster_id": decision["cluster_id"]}
    return {"selection_kind": "exact", "choice_id": decision["choice_id"]}


def _normalize_mode(
    raw: Any,
    *,
    mode: str,
    allowed_cluster_ids: set[str],
    allowed_choice_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WeeklyLlmResponseError(f"model_response.{mode} must be an object")
    selection_kind = raw.get("selection_kind")
    confidence = raw.get("confidence")
    evidence = raw.get("evidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        raise WeeklyLlmResponseError(f"model_response.{mode}.confidence is invalid")
    if confidence < 0 or confidence > 1:
        raise WeeklyLlmResponseError(f"model_response.{mode}.confidence is out of range")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240:
        raise WeeklyLlmResponseError(f"model_response.{mode}.evidence is invalid")
    if selection_kind == "none":
        _exact_keys(raw, {"selection_kind", "confidence", "evidence"}, f"model_response.{mode}")
        return {
            "selection_kind": "none",
            "confidence": confidence,
            "evidence": evidence.strip(),
        }
    if mode == "clustered" and selection_kind == "cluster":
        _exact_keys(
            raw,
            {"selection_kind", "cluster_id", "confidence", "evidence"},
            f"model_response.{mode}",
        )
        cluster_id = raw.get("cluster_id")
        if not isinstance(cluster_id, str) or not _ID_RE.fullmatch(cluster_id):
            raise WeeklyLlmResponseError(f"model_response.{mode}.cluster_id is invalid")
        if cluster_id not in allowed_cluster_ids:
            raise WeeklyLlmResponseError(f"cluster_id is not valid for item")
        return {
            "selection_kind": "cluster",
            "cluster_id": cluster_id,
            "confidence": confidence,
            "evidence": evidence.strip(),
        }
    if mode == "unclustered" and selection_kind == "exact":
        _exact_keys(
            raw,
            {"selection_kind", "choice_id", "confidence", "evidence"},
            f"model_response.{mode}",
        )
        choice_id = raw.get("choice_id")
        if not isinstance(choice_id, str) or not _ID_RE.fullmatch(choice_id):
            raise WeeklyLlmResponseError(f"model_response.{mode}.choice_id is invalid")
        if choice_id not in allowed_choice_ids:
            raise WeeklyLlmResponseError("choice_id is not valid for item")
        return {
            "selection_kind": "exact",
            "choice_id": choice_id,
            "confidence": confidence,
            "evidence": evidence.strip(),
        }
    raise WeeklyLlmResponseError(f"model_response.{mode}.selection_kind is invalid")


def _exact_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    missing = allowed - set(value)
    if unknown or missing:
        raise WeeklyLlmResponseError(f"{label} keys are not exact")


__all__ = [
    "WeeklyLlmResponseError",
    "model_response_to_submission_item",
    "validate_model_response",
]
