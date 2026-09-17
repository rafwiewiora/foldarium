"""Append-only Weekly lifecycle journal helpers (provider-neutral).

Records are one JSON file per event on a durable filesystem (Modal Volume in
deployment). Sanitization and size bounds keep operator logs safe to inspect.
"""

from __future__ import annotations

import json
import math
import os
import re
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

LIFECYCLE_JOURNAL_VOLUME_NAME = "foldarium-weekly-lifecycle-logs"
DEFAULT_JOURNAL_ROOT = "/var/foldarium/weekly-lifecycle-journal"

JournalPhase = Literal["started", "succeeded", "failed"]

_REDACT_KEY = re.compile(
    r"(secret|token|auth|password|cookie|credential)",
    re.IGNORECASE,
)
_FREEFORM_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer <redacted>",
    ),
    (
        re.compile(
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
        "<jwt-redacted>",
    ),
    (
        re.compile(r"sb_[a-z]+_[A-Za-z0-9_-]+"),
        "<supabase-key-redacted>",
    ),
    (
        re.compile(
            r"(?i)((?:api[_-]?key|access[_-]?token|secret|password|authorization)"
            r"\s*[:=]\s*)\S+"
        ),
        r"\1<redacted>",
    ),
)
_MAX_DEPTH = 8
_MAX_COLLECTION_ITEMS = 64
_MAX_STRING_CHARS = 2048
_MAX_TRACEBACK_CHARS = 8192
_MAX_EVENT_BYTES = 256 * 1024
_COMMIT_MAX_ATTEMPTS = 3
_FILENAME_ALLOCATION_ATTEMPTS = 8


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def redact_freeform_text(
    text: str,
    *,
    max_chars: int = _MAX_STRING_CHARS,
) -> str:
    """Redact likely credential material embedded in unstructured text."""

    redacted = text
    for pattern, replacement in _FREEFORM_REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    if len(redacted) > max_chars:
        return redacted[:max_chars] + "…<truncated>"
    return redacted


def dumps_journal_json(payload: Any) -> str:
    """Serialize journal payloads as strict JSON (no NaN/Infinity)."""

    return json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "<non-finite-float>"
        return value
    return value


def sanitize_for_journal(
    value: Any,
    *,
    depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """Return a JSON-safe, redacted, bounded copy of ``value``."""

    if depth > _MAX_DEPTH:
        return "<max-depth>"
    normalized = _normalize_scalar(value)
    if normalized != value or isinstance(normalized, (bool, int, float)):
        return normalized
    if value is None:
        return None
    if isinstance(value, str):
        return redact_freeform_text(value)
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)[:_MAX_COLLECTION_ITEMS]
        sanitized = [
            sanitize_for_journal(item, depth=depth + 1, _seen=_seen) for item in items
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            sanitized.append(f"<truncated {len(value) - _MAX_COLLECTION_ITEMS} items>")
        return sanitized
    if _is_mapping(value):
        if _seen is None:
            _seen = set()
        object_id = id(value)
        if object_id in _seen:
            return "<cycle>"
        _seen.add(object_id)
        items: list[tuple[str, Any]] = []
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                items.append(("<truncated-keys>", f"{len(value) - index} more"))
                break
            key_text = str(key)
            if _REDACT_KEY.search(key_text):
                items.append((key_text, "<redacted>"))
            else:
                items.append(
                    (
                        key_text,
                        sanitize_for_journal(item, depth=depth + 1, _seen=_seen),
                    )
                )
        return dict(items)
    return redact_freeform_text(str(value)[:_MAX_STRING_CHARS])


def bound_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure serialized event fits within ``_MAX_EVENT_BYTES``."""

    sanitized = sanitize_for_journal(dict(payload))
    if not isinstance(sanitized, dict):
        sanitized = {"payload": sanitized}
    encoded = dumps_journal_json(sanitized).encode("utf-8")
    if len(encoded) <= _MAX_EVENT_BYTES:
        return sanitized
    compact = {
        "truncated": True,
        "operation": sanitized.get("operation"),
        "phase": sanitized.get("phase"),
        "correlation_id": sanitized.get("correlation_id"),
        "event_id": sanitized.get("event_id"),
        "recorded_at": sanitized.get("recorded_at"),
        "original_bytes": len(encoded),
        "max_event_bytes": _MAX_EVENT_BYTES,
    }
    dumps_journal_json(compact)
    return compact


def journal_event_filename(
    *,
    recorded_at: str,
    correlation_id: str,
    phase: JournalPhase,
    event_id: str,
) -> str:
    safe_time = recorded_at.replace(":", "").replace("-", "")
    return f"{safe_time}_{correlation_id}_{phase}_{event_id}.json"


def is_transient_commit_error(exc: BaseException) -> bool:
    """Return True for commit failures that may succeed on an immediate retry."""

    return isinstance(exc, OSError)


def commit_journal_volume(commit: Callable[[], None] | None) -> None:
    """Commit durable journal storage with bounded immediate retries."""

    if commit is None:
        return
    last_error: BaseException | None = None
    for attempt in range(_COMMIT_MAX_ATTEMPTS):
        try:
            commit()
            return
        except BaseException as exc:
            last_error = exc
            if not is_transient_commit_error(exc) or attempt + 1 >= _COMMIT_MAX_ATTEMPTS:
                raise
    if last_error is not None:
        raise last_error


def _write_exclusive_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    file_descriptor = os.open(path, flags, 0o600)
    try:
        os.write(file_descriptor, payload)
    finally:
        os.close(file_descriptor)


def write_lifecycle_journal_event(
    root: Path,
    event: Mapping[str, Any],
    *,
    commit: Callable[[], None] | None = None,
) -> Path | None:
    """Write one append-only journal record and optionally commit durable storage."""

    bounded = bound_event_payload(event)
    recorded_at = str(bounded.get("recorded_at", utc_now_iso()))
    correlation_id = str(bounded.get("correlation_id", "unknown"))
    phase = bounded.get("phase", "started")
    event_id = str(bounded.get("event_id", uuid.uuid4().hex[:12]))
    if phase not in ("started", "succeeded", "failed"):
        phase = "started"
    root.mkdir(parents=True, exist_ok=True)
    written_path: Path | None = None
    for _ in range(_FILENAME_ALLOCATION_ATTEMPTS):
        filename = journal_event_filename(
            recorded_at=recorded_at,
            correlation_id=correlation_id,
            phase=phase,  # type: ignore[arg-type]
            event_id=event_id,
        )
        path = root / filename
        serialized = dumps_journal_json(bounded).encode("utf-8")
        try:
            _write_exclusive_bytes(path, serialized)
        except FileExistsError:
            event_id = uuid.uuid4().hex[:12]
            continue
        written_path = path
        break
    if written_path is None:
        raise OSError("could not allocate an exclusive lifecycle journal filename")
    commit_journal_volume(commit)
    return written_path


def format_failure_record(
    exc: BaseException,
    *,
    correlation_id: str,
    operation: str,
    started_at: str,
) -> dict[str, Any]:
    trace = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    if len(trace) > _MAX_TRACEBACK_CHARS:
        trace = trace[:_MAX_TRACEBACK_CHARS] + "\n…<traceback-truncated>"
    message = redact_freeform_text(str(exc))
    trace = redact_freeform_text(trace, max_chars=_MAX_TRACEBACK_CHARS)
    return bound_event_payload(
        {
            "schema": "foldarium.weekly-lifecycle-journal/v1",
            "phase": "failed",
            "operation": operation,
            "correlation_id": correlation_id,
            "event_id": uuid.uuid4().hex[:12],
            "recorded_at": utc_now_iso(),
            "started_at": started_at,
            "exception_type": type(exc).__name__,
            "exception_message": message,
            "traceback": trace,
        }
    )


def run_with_lifecycle_journal(
    operation: str,
    root: Path,
    fn: Callable[[], Any],
    *,
    args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
    commit: Callable[[], None] | None = None,
    on_journal_error: Callable[[str], None] | None = None,
) -> Any:
    """Execute ``fn`` and append started/succeeded/failed journal records."""

    correlation_id = new_correlation_id()
    started_at = utc_now_iso()
    safe_kwargs = {} if kwargs is None else dict(kwargs)
    started_event = bound_event_payload(
        {
            "schema": "foldarium.weekly-lifecycle-journal/v1",
            "phase": "started",
            "operation": operation,
            "correlation_id": correlation_id,
            "event_id": uuid.uuid4().hex[:12],
            "recorded_at": started_at,
            "started_at": started_at,
            "args": sanitize_for_journal(list(args)),
            "kwargs": sanitize_for_journal(safe_kwargs),
        }
    )

    def _emit(message: str) -> None:
        if on_journal_error is not None:
            on_journal_error(message)

    try:
        write_lifecycle_journal_event(root, started_event, commit=commit)
    except Exception as journal_exc:  # pragma: no cover - best-effort path
        _emit(
            f"foldarium.lifecycle_journal write-failed operation={operation} "
            f"phase=started error={type(journal_exc).__name__}"
        )

    try:
        outcome = fn()
    except BaseException as exc:
        try:
            write_lifecycle_journal_event(
                root,
                format_failure_record(
                    exc,
                    correlation_id=correlation_id,
                    operation=operation,
                    started_at=started_at,
                ),
                commit=commit,
            )
        except Exception as journal_exc:  # pragma: no cover
            _emit(
                f"foldarium.lifecycle_journal write-failed operation={operation} "
                f"phase=failed error={type(journal_exc).__name__}"
            )
        raise

    try:
        write_lifecycle_journal_event(
            root,
            bound_event_payload(
                {
                    "schema": "foldarium.weekly-lifecycle-journal/v1",
                    "phase": "succeeded",
                    "operation": operation,
                    "correlation_id": correlation_id,
                    "event_id": uuid.uuid4().hex[:12],
                    "recorded_at": utc_now_iso(),
                    "started_at": started_at,
                    "outcome": sanitize_for_journal(
                        outcome if isinstance(outcome, Mapping) else {"result": outcome}
                    ),
                }
            ),
            commit=commit,
        )
    except Exception as journal_exc:  # pragma: no cover
        _emit(
            f"foldarium.lifecycle_journal write-failed operation={operation} "
            f"phase=succeeded error={type(journal_exc).__name__}"
        )
    return outcome


def read_lifecycle_journal_events(
    root: Path,
    *,
    operation: str | None = None,
    correlation_id: str | None = None,
    phase: JournalPhase | None = None,
    since: str | None = None,
    limit: int = 50,
    reload: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` newest events matching optional filters."""

    if limit < 1 or limit > 500:
        raise ValueError("limit must be from 1 to 500")
    if reload is not None:
        reload()
    if not root.is_dir():
        return []
    since_dt = None
    if since is not None:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    events: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if operation is not None and payload.get("operation") != operation:
            continue
        if correlation_id is not None and payload.get("correlation_id") != correlation_id:
            continue
        if phase is not None and payload.get("phase") != phase:
            continue
        recorded_at = payload.get("recorded_at")
        if since_dt is not None and isinstance(recorded_at, str):
            try:
                event_time = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if event_time < since_dt:
                continue
        events.append({**payload, "_path": path.name})
    events.sort(key=lambda item: str(item.get("recorded_at", "")))
    return events[-limit:]


__all__ = [
    "DEFAULT_JOURNAL_ROOT",
    "LIFECYCLE_JOURNAL_VOLUME_NAME",
    "bound_event_payload",
    "commit_journal_volume",
    "dumps_journal_json",
    "format_failure_record",
    "is_transient_commit_error",
    "journal_event_filename",
    "new_correlation_id",
    "read_lifecycle_journal_events",
    "redact_freeform_text",
    "run_with_lifecycle_journal",
    "sanitize_for_journal",
    "utc_now_iso",
    "write_lifecycle_journal_event",
]
