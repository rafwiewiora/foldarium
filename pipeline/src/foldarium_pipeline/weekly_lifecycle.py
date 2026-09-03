"""Shared policy helpers for Weekly voting and retrospective timing."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

NEXT_WEEKLY_RETROSPECTIVE_POLICY = "next-weekly-activation"
RETROSPECTIVE_RELEASE_METADATA_KEY = "retrospective_release"


class WeeklyLifecycleError(ValueError):
    """Raised when stored Weekly lifecycle policy metadata is invalid."""


def delayed_retrospective_release(
    round_record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return validated opt-in delayed-release metadata, or ``None``."""

    metadata = round_record.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise WeeklyLifecycleError("weekly round metadata must be an object")
    raw = metadata.get(RETROSPECTIVE_RELEASE_METADATA_KEY)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise WeeklyLifecycleError("retrospective release metadata must be an object")
    policy = raw.get("policy")
    if policy != NEXT_WEEKLY_RETROSPECTIVE_POLICY:
        raise WeeklyLifecycleError("retrospective release policy is unsupported")
    for field in (
        "original_closes_at",
        "safety_closes_at",
        "configured_at",
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise WeeklyLifecycleError(
                f"retrospective release metadata has no {field}"
            )
    prepared = raw.get("prepared_evaluation")
    if prepared is not None and not isinstance(prepared, Mapping):
        raise WeeklyLifecycleError("prepared evaluation metadata must be an object")
    return deepcopy(dict(raw))


__all__ = [
    "NEXT_WEEKLY_RETROSPECTIVE_POLICY",
    "RETROSPECTIVE_RELEASE_METADATA_KEY",
    "WeeklyLifecycleError",
    "delayed_retrospective_release",
]
