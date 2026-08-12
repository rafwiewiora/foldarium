"""Deterministic, offline archives for weekly quiz research traces.

This module deliberately has no Supabase or object-storage client.  Its input is
an explicitly exported, server-safe JSON snapshot for one quiz session.  It
writes a canonical JSONL stream plus a deterministic gzip envelope and then
verifies both the archive and its sidecar manifest before reporting success.

The source vote rows stay useful without the archive: choice revisions,
comments, trace bindings, and compact app state remain hot in Postgres.  The
archive is for bulky continuous trace batches and legacy embedded vote traces.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ARCHIVE_FORMAT_VERSION = "foldarium.weekly-session-trace-archive/v1"
EXPORTER_VERSION = "foldarium-pipeline/trace-archive-v1"
COMPRESSION = "gzip"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_FORBIDDEN_IDENTITY_KEYS = {
    "display_name",
    "email",
    "participant_name",
    "player_name",
    "refresh_token",
    "service_role_key",
    "user_id",
    "username",
}


class TraceArchiveError(ValueError):
    """The snapshot or archive does not satisfy the lossless archive contract."""


@dataclass(frozen=True)
class ExportResult:
    archive_path: Path
    manifest_path: Path
    manifest: dict[str, Any]
    reused_existing: bool


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise TraceArchiveError(f"value is not canonical JSON: {error}") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uuid(value: Any, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise TraceArchiveError(f"{label} must be a UUID") from error


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise TraceArchiveError(f"{label} must be non-empty text of at most {maximum} characters")
    return value


def _safe_id(value: Any, label: str) -> str:
    result = _text(value, label)
    if not _SAFE_ID_RE.fullmatch(result):
        raise TraceArchiveError(f"{label} contains unsupported characters")
    return result


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise TraceArchiveError(f"{label} must be a lowercase SHA-256/HMAC value")
    return value


def _timestamp(value: Any, label: str) -> str:
    # PostgreSQL emits ISO 8601 timestamps.  Keep the exact value so replay
    # ordering and provenance remain lossless; reject ambiguous local times.
    result = _text(value, label, maximum=64)
    if "T" not in result or not (result.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", result)):
        raise TraceArchiveError(f"{label} must be an ISO 8601 timestamp with an offset")
    return result


def _strict_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise TraceArchiveError(f"{label} has unsupported fields: {sorted(unknown)}")


def _reject_identity_fields(value: Any, label: str = "payload") -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_identity_fields(child, f"{label}[{index}]")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_IDENTITY_KEYS:
                raise TraceArchiveError(f"{label} contains forbidden identity field {key!r}")
            _reject_identity_fields(child, f"{label}.{key}")


def _reject_nested_vote_comment(value: Any, label: str) -> None:
    """Keep comments solely in the dedicated hot vote-attempt column."""

    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_nested_vote_comment(child, f"{label}[{index}]")
    elif isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() == "vote_comment":
                raise TraceArchiveError(f"{label} contains forbidden nested vote_comment")
            _reject_nested_vote_comment(child, f"{label}.{key}")


def _json_object(value: Any, label: str, *, nullable: bool = True) -> dict[str, Any] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, Mapping):
        raise TraceArchiveError(f"{label} must be a JSON object")
    result = dict(value)
    _reject_identity_fields(result, label)
    _canonical_json(result)
    return result


def _normalize_session(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceArchiveError("session must be a JSON object")
    allowed = {
        "session_id",
        "round_id",
        "participant_hash",
        "display_name_hash",
        "started_at",
        "completed_at",
    }
    _strict_keys(value, allowed, "session")
    completed = value.get("completed_at")
    return {
        "session_id": _uuid(value.get("session_id"), "session.session_id"),
        "round_id": _safe_id(value.get("round_id"), "session.round_id"),
        "participant_hash": _hash(value.get("participant_hash"), "session.participant_hash"),
        "display_name_hash": _hash(
            value.get("display_name_hash"), "session.display_name_hash"
        ),
        "started_at": _timestamp(value.get("started_at"), "session.started_at"),
        "completed_at": (
            _timestamp(completed, "session.completed_at") if completed is not None else None
        ),
    }


def _normalize_vote(value: Any, session: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceArchiveError("each vote_attempt must be a JSON object")
    allowed = {
        "vote_attempt_id",
        "session_id",
        "round_id",
        "item_id",
        "question_index",
        "choice_id",
        "picked_none",
        "viewer_trace",
        "app_state",
        "active_pane_id",
        "vote_comment",
        "submitted_at",
        "created_at",
    }
    if set(value) & _FORBIDDEN_IDENTITY_KEYS:
        raise TraceArchiveError("vote_attempt contains forbidden identity fields")
    _strict_keys(value, allowed, "vote_attempt")
    session_id = _uuid(value.get("session_id"), "vote_attempt.session_id")
    round_id = _safe_id(value.get("round_id"), "vote_attempt.round_id")
    if session_id != session["session_id"] or round_id != session["round_id"]:
        raise TraceArchiveError("vote_attempt is not a member of the archived session")
    question_index = value.get("question_index")
    if isinstance(question_index, bool) or not isinstance(question_index, int) or question_index < 0:
        raise TraceArchiveError("vote_attempt.question_index must be a non-negative integer")
    picked_none = value.get("picked_none")
    choice_id = value.get("choice_id")
    if not isinstance(picked_none, bool):
        raise TraceArchiveError("vote_attempt.picked_none must be boolean")
    if picked_none:
        if choice_id is not None:
            raise TraceArchiveError("a none vote cannot have a choice_id")
    else:
        choice_id = _safe_id(choice_id, "vote_attempt.choice_id")
    comment = value.get("vote_comment")
    if comment is not None:
        if not isinstance(comment, str) or len(comment) > 4000 or len(comment.encode("utf-8")) > 16384:
            raise TraceArchiveError("vote_attempt.vote_comment exceeds the hot comment bound")
    active_pane_id = value.get("active_pane_id")
    if active_pane_id is not None:
        active_pane_id = _safe_id(active_pane_id, "vote_attempt.active_pane_id")
    app_state = _json_object(value.get("app_state"), "vote_attempt.app_state")
    _reject_nested_vote_comment(app_state, "vote_attempt.app_state")
    result = {
        "record_type": "vote_attempt",
        "vote_attempt_id": _uuid(value.get("vote_attempt_id"), "vote_attempt.vote_attempt_id"),
        "session_id": session_id,
        "round_id": round_id,
        "item_id": _safe_id(value.get("item_id"), "vote_attempt.item_id"),
        "question_index": question_index,
        "choice_id": choice_id,
        "picked_none": picked_none,
        # Legacy rows may still contain a complete trace.  New rows bind the
        # compact app_state to continuous batches and leave this null.
        "viewer_trace": _json_object(value.get("viewer_trace"), "vote_attempt.viewer_trace"),
        "app_state": app_state,
        "active_pane_id": active_pane_id,
        "vote_comment": comment,
        "submitted_at": _timestamp(value.get("submitted_at"), "vote_attempt.submitted_at"),
    }
    if value.get("created_at") is not None:
        result["created_at"] = _timestamp(value["created_at"], "vote_attempt.created_at")
    return result


def _normalize_batch(value: Any, session: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceArchiveError("each trace_batch must be a JSON object")
    allowed = {
        "trace_batch_id",
        "session_id",
        "round_id",
        "item_id",
        "question_index",
        "visit_id",
        "first_sequence",
        "last_sequence",
        "flush_reason",
        "trace",
        "app_state",
        "submitted_at",
        "created_at",
    }
    _strict_keys(value, allowed, "trace_batch")
    session_id = _uuid(value.get("session_id"), "trace_batch.session_id")
    round_id = _safe_id(value.get("round_id"), "trace_batch.round_id")
    if session_id != session["session_id"] or round_id != session["round_id"]:
        raise TraceArchiveError("trace_batch is not a member of the archived session")
    question_index = value.get("question_index")
    first = value.get("first_sequence")
    last = value.get("last_sequence")
    for number, label in (
        (question_index, "question_index"),
        (first, "first_sequence"),
        (last, "last_sequence"),
    ):
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise TraceArchiveError(f"trace_batch.{label} must be a non-negative integer")
    if last < first:
        raise TraceArchiveError("trace_batch sequence range is reversed")
    visit_id = _uuid(value.get("visit_id"), "trace_batch.visit_id")
    trace = _json_object(value.get("trace"), "trace_batch.trace", nullable=False)
    entries = trace.get("entries")
    if trace.get("version") != 1 or trace.get("visit_id") != visit_id or not isinstance(entries, list):
        raise TraceArchiveError("trace_batch.trace does not match its visit identity")
    spans = [_entry_span(entry, visit_id) for entry in entries]
    if not spans:
        raise TraceArchiveError("trace_batch entries must contain integer sequences")
    if spans[0][0] != first or spans[-1][1] != last:
        raise TraceArchiveError("trace_batch entry endpoints do not match its sequence range")
    expected = first
    for entry_first, entry_last, _ in spans:
        if entry_first != expected:
            raise TraceArchiveError("trace_batch entries have an unexplained sequence gap")
        expected = entry_last + 1
    result = {
        "record_type": "trace_batch",
        "trace_batch_id": _uuid(value.get("trace_batch_id"), "trace_batch.trace_batch_id"),
        "session_id": session_id,
        "round_id": round_id,
        "item_id": _safe_id(value.get("item_id"), "trace_batch.item_id"),
        "question_index": question_index,
        "visit_id": visit_id,
        "first_sequence": first,
        "last_sequence": last,
        "flush_reason": _safe_id(value.get("flush_reason"), "trace_batch.flush_reason"),
        "trace": trace,
        "app_state": _json_object(value.get("app_state"), "trace_batch.app_state"),
        "submitted_at": _timestamp(value.get("submitted_at"), "trace_batch.submitted_at"),
    }
    if value.get("created_at") is not None:
        result["created_at"] = _timestamp(value["created_at"], "trace_batch.created_at")
    return result


def _entry_span(entry: Any, visit_id: str) -> tuple[int, int, str]:
    if not isinstance(entry, Mapping):
        raise TraceArchiveError(f"visit {visit_id} contains a non-object trace entry")
    sequence = entry.get("seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise TraceArchiveError(f"visit {visit_id} contains an invalid trace sequence")
    kind = entry.get("kind")
    if kind not in {"omitted", "dead_letter"}:
        if "accounted_first_sequence" in entry or "accounted_last_sequence" in entry:
            raise TraceArchiveError(
                f"visit {visit_id} uses an accounted range on a normal trace entry"
            )
        return sequence, sequence, "event"
    first = entry.get("accounted_first_sequence", sequence)
    last = entry.get("accounted_last_sequence", sequence)
    if (
        isinstance(first, bool)
        or isinstance(last, bool)
        or not isinstance(first, int)
        or not isinstance(last, int)
        or first != sequence
        or last < first
    ):
        raise TraceArchiveError(f"visit {visit_id} contains an invalid accounted omission range")
    reason = entry.get("reason")
    omitted_bytes = entry.get("omitted_bytes")
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > 64
        or isinstance(omitted_bytes, bool)
        or not isinstance(omitted_bytes, int)
        or omitted_bytes < 0
    ):
        raise TraceArchiveError(f"visit {visit_id} omission is missing bounded integrity metadata")
    return first, last, str(kind)


def _validate_visit_coverage(batches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    visits: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for batch in batches:
        visits[str(batch["visit_id"])].append(batch)
    event_count = 0
    omitted_entry_count = 0
    dead_letter_entry_count = 0
    accounted_omitted_sequence_count = 0
    for visit_id, members in visits.items():
        ordered = sorted(
            members,
            key=lambda row: (int(row["first_sequence"]), int(row["last_sequence"]), row["trace_batch_id"]),
        )
        expected = 0
        questions = {(row["item_id"], row["question_index"]) for row in ordered}
        if len(questions) != 1:
            raise TraceArchiveError(f"visit {visit_id} crosses quiz questions")
        for batch in ordered:
            if batch["first_sequence"] != expected:
                raise TraceArchiveError(
                    f"visit {visit_id} has a sequence gap or overlap before {batch['first_sequence']}"
                )
            entries = batch["trace"]["entries"]
            for entry in entries:
                first, last, kind = _entry_span(entry, visit_id)
                if first != expected:
                    raise TraceArchiveError(
                        f"visit {visit_id} has an unexplained sequence gap or overlap before {first}"
                    )
                expected = last + 1
                event_count += 1
                if kind == "omitted":
                    omitted_entry_count += 1
                    accounted_omitted_sequence_count += last - first + 1
                elif kind == "dead_letter":
                    dead_letter_entry_count += 1
                    accounted_omitted_sequence_count += last - first + 1
            if expected - 1 != batch["last_sequence"]:
                raise TraceArchiveError(
                    f"visit {visit_id} batch endpoint excludes an accounted trace range"
                )
    return {
        "visit_count": len(visits),
        "trace_entry_count": event_count,
        "omitted_entry_count": omitted_entry_count,
        "dead_letter_entry_count": dead_letter_entry_count,
        "accounted_omitted_sequence_count": accounted_omitted_sequence_count,
        "sequence_gaps": 0,
    }


def canonical_records(source: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate a server-safe session snapshot and return canonical records/stats."""

    if not isinstance(source, Mapping):
        raise TraceArchiveError("archive source must be a JSON object")
    _strict_keys(source, {"session", "vote_attempts", "trace_batches"}, "archive source")
    session = _normalize_session(source.get("session"))
    votes_value = source.get("vote_attempts")
    batches_value = source.get("trace_batches")
    if not isinstance(votes_value, list) or not isinstance(batches_value, list):
        raise TraceArchiveError("vote_attempts and trace_batches must be arrays")
    votes = [_normalize_vote(value, session) for value in votes_value]
    batches = [_normalize_batch(value, session) for value in batches_value]
    vote_ids = [row["vote_attempt_id"] for row in votes]
    batch_ids = [row["trace_batch_id"] for row in batches]
    if len(vote_ids) != len(set(vote_ids)) or len(batch_ids) != len(set(batch_ids)):
        raise TraceArchiveError("source membership contains duplicate primary keys")
    coverage = _validate_visit_coverage(batches)
    votes.sort(key=lambda row: (row["submitted_at"], row["vote_attempt_id"]))
    header = {
        "record_type": "session",
        "format_version": ARCHIVE_FORMAT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        **session,
    }
    # Record types stay grouped for streaming replay.  Within each type we sort
    # by semantic visit/revision order rather than ingestion time.
    batches.sort(
        key=lambda row: (
            row["question_index"],
            row["visit_id"],
            row["first_sequence"],
            row["trace_batch_id"],
        )
    )
    records = [header, *votes, *batches]
    submitted = [row["submitted_at"] for row in [*votes, *batches]]
    archive_record_ordinals = {
        (
            record["record_type"],
            record[
                "vote_attempt_id"
                if record["record_type"] == "vote_attempt"
                else "trace_batch_id"
            ],
        ): ordinal
        for ordinal, record in enumerate(records[1:])
    }
    visit_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for batch in batches:
        visit_rows[batch["visit_id"]].append(batch)
    ordered_visit_ids = sorted(
        visit_rows,
        key=lambda visit_id: (
            min(row["submitted_at"] for row in visit_rows[visit_id]),
            min(row["question_index"] for row in visit_rows[visit_id]),
            visit_id,
        ),
    )
    visit_ordinals = {visit_id: ordinal for ordinal, visit_id in enumerate(ordered_visit_ids)}
    batch_ordinals = {
        row["trace_batch_id"]: ordinal
        for visit_id in ordered_visit_ids
        for ordinal, row in enumerate(
            sorted(
                visit_rows[visit_id],
                key=lambda item: (item["first_sequence"], item["trace_batch_id"]),
            )
        )
    }
    question_revision_ordinals: dict[tuple[int, str], int] = {}
    revision_counts: dict[int, int] = defaultdict(int)
    for vote in votes:
        revision = revision_counts[vote["question_index"]]
        revision_counts[vote["question_index"]] += 1
        question_revision_ordinals[(vote["question_index"], vote["vote_attempt_id"])] = revision
    members = []
    for record in [*votes, *batches]:
        kind = record["record_type"]
        canonical = _canonical_json(record).encode("utf-8")
        member = {
            "source_kind": kind,
            "source_id": record[
                "vote_attempt_id" if kind == "vote_attempt" else "trace_batch_id"
            ],
            "item_id": record["item_id"],
            "question_index": record["question_index"],
            "submitted_at": record["submitted_at"],
            "payload_sha256": _sha256(canonical),
            "payload_bytes": len(canonical),
            "archive_record_ordinal": archive_record_ordinals[(kind, record[
                "vote_attempt_id" if kind == "vote_attempt" else "trace_batch_id"
            ])],
        }
        if kind == "trace_batch":
            member.update(
                {
                    "visit_id": record["visit_id"],
                    "first_sequence": record["first_sequence"],
                    "last_sequence": record["last_sequence"],
                    "visit_ordinal": visit_ordinals[record["visit_id"]],
                    "visit_batch_ordinal": batch_ordinals[record["trace_batch_id"]],
                }
            )
        else:
            member["question_revision_ordinal"] = question_revision_ordinals[
                (record["question_index"], record["vote_attempt_id"])
            ]
        members.append(member)
    members.sort(key=lambda row: (row["source_kind"], row["source_id"]))
    visit_index = []
    indexed_visits: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for batch in batches:
        indexed_visits[batch["visit_id"]].append(batch)
    for visit_id in ordered_visit_ids:
        rows = indexed_visits[visit_id]
        ordered = sorted(rows, key=lambda row: row["first_sequence"])
        visit_entries = [entry for row in ordered for entry in row["trace"]["entries"]]
        spans = [_entry_span(entry, visit_id) for entry in visit_entries]
        visit_index.append(
            {
                "visit_ordinal": visit_ordinals[visit_id],
                "visit_id": visit_id,
                "item_id": ordered[0]["item_id"],
                "question_index": ordered[0]["question_index"],
                "first_sequence": ordered[0]["first_sequence"],
                "last_sequence": ordered[-1]["last_sequence"],
                "batch_count": len(ordered),
                "trace_entry_count": sum(len(row["trace"]["entries"]) for row in ordered),
                "omitted_entry_count": sum(kind == "omitted" for _, _, kind in spans),
                "dead_letter_entry_count": sum(kind == "dead_letter" for _, _, kind in spans),
                "accounted_omitted_sequence_count": sum(
                    last - first + 1
                    for first, last, kind in spans
                    if kind in {"omitted", "dead_letter"}
                ),
                "first_submitted_at": min(row["submitted_at"] for row in ordered),
                "last_submitted_at": max(row["submitted_at"] for row in ordered),
            }
        )
    stats = {
        "session_id": session["session_id"],
        "round_id": session["round_id"],
        "participant_hash": session["participant_hash"],
        "display_name_hash": session["display_name_hash"],
        "vote_attempt_count": len(votes),
        "trace_batch_count": len(batches),
        **coverage,
        "first_submitted_at": min(submitted) if submitted else None,
        "last_submitted_at": max(submitted) if submitted else None,
        "members": members,
        "membership_sha256": _sha256(_canonical_json(members).encode("utf-8")),
        "visits": visit_index,
    }
    return records, stats


def _jsonl(records: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join((_canonical_json(record) + "\n").encode("utf-8") for record in records)


def _gzip(data: bytes) -> bytes:
    # GzipFile with no filename and mtime=0 produces a stable envelope for the
    # same canonical JSONL in the same supported runtime, without dependencies.
    import io

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0, compresslevel=9) as handle:
        handle.write(data)
    return buffer.getvalue()


def build_archive(source: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    records, stats = canonical_records(source)
    content = _jsonl(records)
    archive = _gzip(content)
    content_sha = _sha256(content)
    archive_sha = _sha256(archive)
    archive_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ARCHIVE_FORMAT_VERSION}:{content_sha}"))
    manifest = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "exporter_version": EXPORTER_VERSION,
        "archive_id": archive_id,
        "compression": COMPRESSION,
        "content_sha256": content_sha,
        "archive_sha256": archive_sha,
        "uncompressed_bytes": len(content),
        "archive_bytes": len(archive),
        **stats,
    }
    return archive, manifest


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def export_session_archive(source: Mapping[str, Any], output_dir: Path | str) -> ExportResult:
    """Write or idempotently reuse one verified local per-session archive."""

    archive, manifest = build_archive(source)
    output = Path(output_dir)
    stem = manifest["session_id"]
    archive_path = output / f"{stem}.jsonl.gz"
    manifest_path = output / f"{stem}.manifest.json"
    manifest_bytes = (_canonical_json(manifest) + "\n").encode("utf-8")
    existing = archive_path.exists() or manifest_path.exists()
    if existing:
        if not archive_path.is_file() or not manifest_path.is_file():
            raise TraceArchiveError("an incomplete or non-file archive output already exists")
        if archive_path.read_bytes() != archive or manifest_path.read_bytes() != manifest_bytes:
            raise TraceArchiveError("archive output exists with different content; refusing overwrite")
        verify_session_archive(archive_path, manifest_path, source=source)
        return ExportResult(archive_path, manifest_path, manifest, True)
    _write_atomic(archive_path, archive)
    _write_atomic(manifest_path, manifest_bytes)
    try:
        verify_session_archive(archive_path, manifest_path, source=source)
    except Exception:
        # This only removes files written by this failed local operation.  It is
        # not a database/source purge and avoids presenting an unverified pair.
        archive_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise
    return ExportResult(archive_path, manifest_path, manifest, False)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TraceArchiveError(f"manifest is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise TraceArchiveError("manifest must be a JSON object")
    if raw != (_canonical_json(value) + "\n").encode("utf-8"):
        raise TraceArchiveError("manifest is not canonical JSON")
    return value


def _read_gzip_bounded(path: Path, expected_bytes: int) -> bytes:
    if expected_bytes < 0 or expected_bytes > MAX_ARCHIVE_BYTES:
        raise TraceArchiveError("manifest uncompressed size is outside the safety bound")
    result = bytearray()
    try:
        with gzip.open(path, "rb") as handle:
            while chunk := handle.read(min(1024 * 1024, expected_bytes + 1 - len(result))):
                result.extend(chunk)
                if len(result) > expected_bytes:
                    raise TraceArchiveError("archive expands beyond its declared size")
    except (OSError, EOFError) as error:
        raise TraceArchiveError(f"archive compression is corrupt: {error}") from error
    if len(result) != expected_bytes:
        raise TraceArchiveError("archive uncompressed byte count does not match its manifest")
    return bytes(result)


def _parse_canonical_jsonl(content: bytes) -> list[dict[str, Any]]:
    if not content.endswith(b"\n"):
        raise TraceArchiveError("archive JSONL must end with a newline")
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        if not raw_line or len(raw_line) > MAX_JSONL_LINE_BYTES:
            raise TraceArchiveError(f"archive JSONL line {line_number} has an invalid size")
        try:
            value = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TraceArchiveError(f"archive JSONL line {line_number} is invalid: {error}") from error
        if not isinstance(value, dict):
            raise TraceArchiveError(f"archive JSONL line {line_number} is not an object")
        if raw_line != _canonical_json(value).encode("utf-8"):
            raise TraceArchiveError(f"archive JSONL line {line_number} is not canonical")
        records.append(value)
    return records


def _source_from_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records or records[0].get("record_type") != "session":
        raise TraceArchiveError("archive must start with exactly one session record")
    session_record = dict(records[0])
    if session_record.pop("record_type", None) != "session":
        raise TraceArchiveError("archive session record is invalid")
    if session_record.pop("format_version", None) != ARCHIVE_FORMAT_VERSION:
        raise TraceArchiveError("archive format version is unsupported")
    if session_record.pop("exporter_version", None) != EXPORTER_VERSION:
        raise TraceArchiveError("archive exporter version is unsupported")
    votes: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    seen_batch = False
    for record in records[1:]:
        kind = record.get("record_type")
        value = dict(record)
        value.pop("record_type", None)
        if kind == "vote_attempt" and not seen_batch:
            votes.append(value)
        elif kind == "trace_batch":
            seen_batch = True
            batches.append(value)
        else:
            raise TraceArchiveError("archive records are out of canonical type order")
    return {"session": session_record, "vote_attempts": votes, "trace_batches": batches}


def verify_session_archive(
    archive_path: Path | str,
    manifest_path: Path | str,
    *,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify bytes, canonical content, exact membership, and visit sequences."""

    archive_file = Path(archive_path)
    manifest_file = Path(manifest_path)
    manifest = _load_manifest(manifest_file)
    if manifest.get("format_version") != ARCHIVE_FORMAT_VERSION:
        raise TraceArchiveError("manifest format version is unsupported")
    if manifest.get("compression") != COMPRESSION:
        raise TraceArchiveError("manifest compression is unsupported")
    archive_size = archive_file.stat().st_size
    if archive_size != manifest.get("archive_bytes"):
        raise TraceArchiveError("archive byte count does not match its manifest")
    if _file_sha256(archive_file) != manifest.get("archive_sha256"):
        raise TraceArchiveError("archive checksum does not match its manifest")
    expected_uncompressed = manifest.get("uncompressed_bytes")
    if isinstance(expected_uncompressed, bool) or not isinstance(expected_uncompressed, int):
        raise TraceArchiveError("manifest uncompressed byte count is invalid")
    content = _read_gzip_bounded(archive_file, expected_uncompressed)
    if _sha256(content) != manifest.get("content_sha256"):
        raise TraceArchiveError("archive content checksum does not match its manifest")
    records = _parse_canonical_jsonl(content)
    reconstructed = _source_from_records(records)
    canonical, stats = canonical_records(reconstructed)
    if content != _jsonl(canonical):
        raise TraceArchiveError("archive record ordering or content is not canonical")
    for key in (
        "session_id",
        "round_id",
        "participant_hash",
        "display_name_hash",
        "vote_attempt_count",
        "trace_batch_count",
        "visit_count",
        "trace_entry_count",
        "omitted_entry_count",
        "dead_letter_entry_count",
        "accounted_omitted_sequence_count",
        "sequence_gaps",
        "first_submitted_at",
        "last_submitted_at",
        "members",
        "membership_sha256",
        "visits",
    ):
        if manifest.get(key) != stats.get(key):
            raise TraceArchiveError(f"manifest {key} does not match archive content")
    expected_archive_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{ARCHIVE_FORMAT_VERSION}:{manifest['content_sha256']}")
    )
    if manifest.get("archive_id") != expected_archive_id:
        raise TraceArchiveError("manifest archive_id is not content-derived")
    if source is not None:
        expected_records, expected_stats = canonical_records(source)
        if _jsonl(expected_records) != content or expected_stats["members"] != stats["members"]:
            raise TraceArchiveError("archive membership/content has drifted from the source snapshot")
        # A verification succeeds only when every eligible source row belongs to
        # this one session and appears exactly once in the manifest membership.
        # Re-check counts explicitly for a clear, auditable acceptance report.
        expected_vote_ids = {
            row["source_id"]
            for row in expected_stats["members"]
            if row["source_kind"] == "vote_attempt"
        }
        expected_batch_ids = {
            row["source_id"]
            for row in expected_stats["members"]
            if row["source_kind"] == "trace_batch"
        }
        if len(expected_vote_ids) != len(source["vote_attempts"]) or len(expected_batch_ids) != len(
            source["trace_batches"]
        ):
            raise TraceArchiveError("source membership is incomplete or duplicated")
    return {
        "verified": True,
        "archive_id": manifest["archive_id"],
        "session_id": stats["session_id"],
        "vote_attempt_count": stats["vote_attempt_count"],
        "trace_batch_count": stats["trace_batch_count"],
        "trace_entry_count": stats["trace_entry_count"],
        "membership_sha256": stats["membership_sha256"],
    }


def read_source(path: Path | str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TraceArchiveError(f"source snapshot is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise TraceArchiveError("source snapshot must be a JSON object")
    return value
