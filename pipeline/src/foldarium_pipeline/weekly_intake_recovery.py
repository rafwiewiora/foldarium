"""Guardrails for operator-driven exact-date Weekly intake replay."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS = 120


def validate_intake_replay_release_date(
    release_date: str,
    *,
    now: datetime | None = None,
    max_age_days: int = DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS,
) -> str:
    """Return a validated ISO Saturday within the bounded recovery window."""

    try:
        selected = date.fromisoformat(release_date)
    except ValueError as exc:
        raise ValueError("release_date must be an ISO date") from exc
    if selected.weekday() != 5:
        raise ValueError("release_date must be a Saturday")
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if selected > current.date():
        raise ValueError("release_date must not be in the future")
    if max_age_days < 1:
        raise ValueError("max_age_days must be positive")
    oldest = current.date() - timedelta(days=max_age_days)
    if selected < oldest:
        raise ValueError(
            f"release_date is outside the {max_age_days}-day recovery window"
        )
    return selected.isoformat()


__all__ = [
    "DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS",
    "validate_intake_replay_release_date",
]
