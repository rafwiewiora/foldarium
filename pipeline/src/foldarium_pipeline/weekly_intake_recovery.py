"""Guardrails for operator-driven exact-date Weekly intake replay."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS = 120


def live_intake_window_block(release_date: date, *, now: datetime) -> str | None:
    """Bound new acquisition from mutable live URLs, never stored-byte replay.

    wwPDB advertises prerelease availability by Saturday 03:00 UTC and released
    coordinates by Wednesday 00:00 UTC. Time bounds are not freshness proof;
    the live hook separately requires a validated, changed prior source pair.
    """
    if not isinstance(release_date, date) or release_date.weekday() != 5:
        raise ValueError("live intake release_date must be a Saturday")
    if now.tzinfo is None:
        raise ValueError("live intake clock requires a timezone")
    current = now.astimezone(timezone.utc)
    opens = datetime.combine(release_date, time(3), tzinfo=timezone.utc)
    closes = datetime.combine(release_date + timedelta(days=4), time(), tzinfo=timezone.utc)
    if current < opens:
        return "intake-before-prerelease-window"
    if current >= closes:
        return "intake-prerelease-window-closed"
    return None


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
    "live_intake_window_block",
]
