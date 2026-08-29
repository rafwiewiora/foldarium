"""Publish scorer-aligned Weekly training-system overlays."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from .supabase import SupabaseCoordinator
from .training_similarity import (
    SCORER_VERSION,
    TRAINING_OVERLAY_MEDIA_TYPE,
    file_sha256,
    training_overlay_cache_path,
)
from .weekly_training_audit import AUDIT_FORMAT

OVERLAY_MANIFEST_FORMAT = "foldarium.weekly-training-overlay-manifest/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PDB_ID = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_COMPONENT_ID = re.compile(r"^[A-Za-z0-9]{1,8}$")
_WEEK = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OBJECT_URI = re.compile(
    r"^supabase://([^/?#]+)/sha256/([0-9a-f]{2})/([0-9a-f]{64})$"
)


class WeeklyTrainingOverlayError(RuntimeError):
    """Raised when an overlay cache or publication contract is invalid."""


class OverlayPublisher(Protocol):
    """Minimal public-storage interface used by the resumable publisher."""

    def require_public_bucket(self) -> None: ...

    def store_bytes(self, content: bytes, media_type: str) -> dict[str, Any]: ...


def _json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            allow_nan=False,
        )
        + ("\n" if pretty else "")
    ).encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_json_bytes(value, pretty=True))
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_exact_audit(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path)
    try:
        content = source.read_bytes()
        audit = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeeklyTrainingOverlayError("exact audit is invalid") from exc
    if (
        not isinstance(audit, dict)
        or audit.get("format_version") != AUDIT_FORMAT
        or audit.get("mode") != "exact"
        or audit.get("scorer_version") != SCORER_VERSION
        or not isinstance(audit.get("records"), list)
    ):
        raise WeeklyTrainingOverlayError("exact audit contract is invalid")
    return audit, sha256(content).hexdigest()


def _expected_rows(audit: Mapping[str, Any], audit_digest: str) -> dict[tuple[str, str], dict[str, Any]]:
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in audit.get("records", []):
        if not isinstance(record, Mapping) or record.get("status") != "complete":
            continue
        train_pdb = record.get("train_pdb")
        train_het = record.get("train_het")
        score = record.get("train_shape_overlap")
        status = record.get("training_system_overlay_status")
        unavailable_reason = record.get(
            "training_system_overlay_unavailable_reason"
        )
        cache = record.get("training_system_overlay_cache")
        if train_pdb is None and train_het is None and score is None:
            if (
                status != "not-applicable"
                or cache is not None
                or unavailable_reason is not None
            ):
                raise WeeklyTrainingOverlayError(
                    "exact audit no-winner overlay state is invalid"
                )
            continue
        item_id = record.get("item_id")
        week = record.get("blind_week")
        if (
            not isinstance(item_id, str)
            or not _PDB_ID.fullmatch(item_id)
            or not isinstance(week, str)
            or not _WEEK.fullmatch(week)
            or not isinstance(train_pdb, str)
            or not _PDB_ID.fullmatch(train_pdb)
            or not isinstance(train_het, str)
            or not _COMPONENT_ID.fullmatch(train_het)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0.0 <= float(score) <= 1.0
            or record.get("scorer_version") != audit.get("scorer_version")
        ):
            raise WeeklyTrainingOverlayError(
                f"exact audit overlay provenance is invalid for {item_id!r}"
            )
        if status == "unavailable":
            if (
                cache is not None
                or not isinstance(unavailable_reason, str)
                or not unavailable_reason
                or len(unavailable_reason) > 300
            ):
                raise WeeklyTrainingOverlayError(
                    f"exact audit unavailable overlay state is invalid for {item_id}"
                )
            continue
        if (
            status != "available"
            or unavailable_reason is not None
            or not isinstance(cache, Mapping)
            or not isinstance(cache.get("sha256"), str)
            or not _SHA256.fullmatch(cache["sha256"])
            or isinstance(cache.get("size_bytes"), bool)
            or not isinstance(cache.get("size_bytes"), int)
            or cache["size_bytes"] <= 0
            or cache.get("media_type") != TRAINING_OVERLAY_MEDIA_TYPE
        ):
            raise WeeklyTrainingOverlayError(
                f"exact audit available overlay state is invalid for {item_id}"
            )
        key = (week, item_id)
        if key in expected:
            raise WeeklyTrainingOverlayError("exact audit has duplicate item/week records")
        expected[key] = {
            "exact_audit_sha256": audit_digest,
            "scorer_version": audit["scorer_version"],
            "item_id": item_id,
            "week": week,
            "train_pdb": train_pdb,
            "train_het": train_het,
            "train_shape_overlap": float(score),
            "sha256": cache["sha256"],
            "size_bytes": cache["size_bytes"],
            "media_type": cache["media_type"],
        }
    return expected


def _validated_manifest_records(
    manifest: Mapping[str, Any],
    *,
    audit: Mapping[str, Any],
    audit_digest: str,
    require_complete: bool,
) -> dict[tuple[str, str], dict[str, Any]]:
    if (
        manifest.get("format_version") != OVERLAY_MANIFEST_FORMAT
        or manifest.get("exact_audit_sha256") != audit_digest
        or manifest.get("scorer_version") != audit.get("scorer_version")
        or not isinstance(manifest.get("records"), list)
    ):
        raise WeeklyTrainingOverlayError("overlay manifest contract is invalid")
    expected = _expected_rows(audit, audit_digest)
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in manifest["records"]:
        if not isinstance(raw, dict):
            raise WeeklyTrainingOverlayError("overlay manifest record is invalid")
        key = (raw.get("week"), raw.get("item_id"))
        expected_row = expected.get(key)
        if expected_row is None:
            raise WeeklyTrainingOverlayError(
                "overlay manifest record is not in the exact audit"
            )
        for field, value in expected_row.items():
            if raw.get(field) != value:
                raise WeeklyTrainingOverlayError(
                    f"overlay manifest {field} does not match the exact audit"
                )
        object_uri = raw.get("object_uri")
        match = _OBJECT_URI.fullmatch(object_uri) if isinstance(object_uri, str) else None
        if (
            match is None
            or match.group(2) != raw["sha256"][:2]
            or match.group(3) != raw["sha256"]
        ):
            raise WeeklyTrainingOverlayError(
                "overlay manifest object URI is not content-addressed"
            )
        if key in records:
            raise WeeklyTrainingOverlayError(
                "overlay manifest has duplicate item/week records"
            )
        records[key] = raw
    complete = len(records) == len(expected)
    if manifest.get("expected_record_count") != len(expected):
        raise WeeklyTrainingOverlayError("overlay manifest expected count is invalid")
    if manifest.get("published_record_count") != len(records):
        raise WeeklyTrainingOverlayError("overlay manifest published count is invalid")
    if manifest.get("complete") is not complete:
        raise WeeklyTrainingOverlayError("overlay manifest completion state is invalid")
    if require_complete and not complete:
        raise WeeklyTrainingOverlayError("overlay manifest publication is incomplete")
    return records


def load_overlay_manifest(
    path: str | Path,
    *,
    audit: Mapping[str, Any],
    audit_digest: str,
    require_complete: bool = True,
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    source = Path(path)
    try:
        content = source.read_bytes()
        manifest = json.loads(content)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeeklyTrainingOverlayError("overlay manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise WeeklyTrainingOverlayError("overlay manifest is not an object")
    records = _validated_manifest_records(
        manifest,
        audit=audit,
        audit_digest=audit_digest,
        require_complete=require_complete,
    )
    return records, sha256(content).hexdigest()


def _manifest(
    audit_digest: str,
    records: Mapping[tuple[str, str], Mapping[str, Any]],
    expected_count: int,
    generated_at: str,
) -> dict[str, Any]:
    complete = len(records) == expected_count
    return {
        "format_version": OVERLAY_MANIFEST_FORMAT,
        "exact_audit_sha256": audit_digest,
        "scorer_version": SCORER_VERSION,
        "generated_at": generated_at,
        "expected_record_count": expected_count,
        "published_record_count": len(records),
        "complete": complete,
        "records": [
            dict(records[key])
            for key in sorted(records)
        ],
    }


def publish_overlays(
    *,
    exact_audit_path: str | Path,
    cache_directory: str | Path,
    manifest_path: str | Path,
    publisher: OverlayPublisher,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resume publication of every locally cached winning exact overlay."""

    if limit is not None and (isinstance(limit, bool) or limit <= 0):
        raise WeeklyTrainingOverlayError("publication limit must be positive")
    audit, audit_digest = load_exact_audit(exact_audit_path)
    expected = _expected_rows(audit, audit_digest)
    destination = Path(manifest_path)
    records: dict[tuple[str, str], dict[str, Any]] = {}
    existing_manifest: dict[str, Any] | None = None
    if destination.is_file():
        try:
            candidate = json.loads(destination.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WeeklyTrainingOverlayError("overlay manifest is invalid") from exc
        if not isinstance(candidate, dict):
            raise WeeklyTrainingOverlayError("overlay manifest is not an object")
        existing_manifest = candidate
        records, _digest = load_overlay_manifest(
            destination,
            audit=audit,
            audit_digest=audit_digest,
            require_complete=False,
        )
        if len(records) == len(expected):
            return existing_manifest
    generated_at = datetime.now(timezone.utc).isoformat()
    uploaded = 0
    if any(key not in records for key in expected):
        publisher.require_public_bucket()
    for key, row in sorted(expected.items()):
        if key in records:
            continue
        if limit is not None and uploaded >= limit:
            break
        overlay_path = training_overlay_cache_path(
            cache_directory, row["sha256"]
        )
        try:
            content = overlay_path.read_bytes()
        except OSError as exc:
            raise WeeklyTrainingOverlayError(
                f"cached overlay is unavailable for {row['item_id']}"
            ) from exc
        if (
            len(content) != row["size_bytes"]
            or file_sha256(overlay_path) != row["sha256"]
        ):
            raise WeeklyTrainingOverlayError(
                f"cached overlay failed verification for {row['item_id']}"
            )
        stored = publisher.store_bytes(content, row["media_type"])
        if (
            not isinstance(stored, Mapping)
            or stored.get("sha256") != row["sha256"]
            or stored.get("size_bytes") != row["size_bytes"]
            or stored.get("media_type") != row["media_type"]
            or not isinstance(stored.get("object_uri"), str)
        ):
            raise WeeklyTrainingOverlayError(
                "Supabase stored overlay descriptor is inconsistent"
            )
        record = {**row, "object_uri": stored["object_uri"]}
        records[key] = record
        uploaded += 1
        _write_json(
            destination,
            _manifest(audit_digest, records, len(expected), generated_at),
        )
    manifest = _manifest(audit_digest, records, len(expected), generated_at)
    _write_json(destination, manifest)
    return manifest


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exact", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    options = parser.parse_args(arguments)
    if options.limit is not None and options.limit <= 0:
        parser.error("--limit must be positive")
    manifest = publish_overlays(
        exact_audit_path=options.exact,
        cache_directory=options.cache_dir,
        manifest_path=options.manifest,
        publisher=SupabaseCoordinator.from_env(),
        limit=options.limit,
    )
    print(
        json.dumps(
            {
                "complete": manifest["complete"],
                "expected": manifest["expected_record_count"],
                "published": manifest["published_record_count"],
            },
            sort_keys=True,
        )
    )
    return 0 if manifest["complete"] else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "OVERLAY_MANIFEST_FORMAT",
    "WeeklyTrainingOverlayError",
    "load_exact_audit",
    "load_overlay_manifest",
    "main",
    "publish_overlays",
]
