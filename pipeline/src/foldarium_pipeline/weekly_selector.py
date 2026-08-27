"""Blind weekly selector kit and submission contracts.

Participants download a deterministic, leak-safe ZIP containing normalized
targets, candidate assets, JSON schemas, and a standalone stdlib client.
Complete dual-mode submissions bind to the kit digest and round identity.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
import zipfile
from copy import deepcopy
from typing import Any, Mapping

from .contracts import ContractError, SCHEMA_VERSION, validate_target
from .quiz import QUIZ_SCHEMA_VERSION, manifest_sha256
from .weekly_selector_prompt import (
    SELECTOR_ITEM_PROMPT_TEMPLATE,
    SELECTOR_MODEL_RESPONSE_SCHEMA,
    SELECTOR_SYSTEM_PROMPT,
    selector_prompt_profile,
)

KIT_SCHEMA_VERSION = "foldarium.weekly-selector-kit/v2"
SUBMISSION_SCHEMA_VERSION = "foldarium.selector-submission/v2"
SELECTOR_ENVIRONMENTS = ("production", "preview", "development")

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

FORBIDDEN_KEYS = frozenset(
    {
        "accepted_correct",
        "answer",
        "answer_metadata",
        "artifact_sha256",
        "correct",
        "coordinates",
        "crystal",
        "private_index",
        "reference",
        "reference_uri",
        "reveal_manifest",
        "rmsd",
        "run_id",
        "sample_id",
        "score",
    }
)
FORBIDDEN_COORDINATE_KEYS = frozenset(
    {
        "atom_positions",
        "coordinates",
        "coords",
        "heavy_atom_coords",
        "positions",
        "xyz",
    }
)

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_MODE = 0o644 << 16
ASSET_KINDS = ("pose", "protein", "pocket")
ASSET_EXTENSIONS = {"pose": "pdb", "protein": "pdb", "pocket": "pdb"}
ASSET_MEDIA_TYPES = {
    "pose": "chemical/x-pdb",
    "protein": "chemical/x-pdb",
    "pocket": "chemical/x-pdb",
}

MAX_SUBMISSION_PAYLOAD_BYTES = 65_536

SUBMISSION_TOP_KEYS = frozenset(
    {
        "schema_version",
        "submission_id",
        "environment",
        "round_id",
        "blind_manifest_sha256",
        "kit_sha256",
        "items",
    }
)
SUBMISSION_ITEM_KEYS = frozenset({"item_id", "clustered", "unclustered"})
CLUSTER_DECISION_KEYS = frozenset({"selection_kind", "cluster_id"})
EXACT_DECISION_KEYS = frozenset({"selection_kind", "choice_id"})

README_TEMPLATE = """# Foldarium Weekly Selector Kit

This archive is a blind, round-bound benchmark kit. It contains normalized
targets, candidate pose/protein/pocket assets, JSON schemas, and a standalone
Python client. It intentionally excludes answers, private pipeline identifiers,
and raw coordinate arrays outside the bundled structure files.

## Contents

- `manifest.json` — canonical kit descriptor bound to this round
- `schemas/submission.schema.json` — submission contract
- `schemas/model-response.schema.json` — canonical model response contract
- `prompts/profile.json` — versioned prompt profile and canonical digest
- `prompts/system.txt` and `prompts/item-template.txt` — exact prompt bytes
- `client/foldarium_selector_client.py` — stdlib-only helper
- `items/<item_id>/target.json` — normalized `foldarium.prediction/v1` target
- `items/<item_id>/choices/<choice_id>/{pose,protein,pocket}.pdb` — blind assets

## Submitting

Build one complete JSON object with top-level keys `schema_version`,
`submission_id`, `environment`, `round_id`, `blind_manifest_sha256`,
`kit_sha256`, and `items`. Each item has independent `clustered` and
`unclustered` tagged decisions. Use `{"selection_kind":"none"}` to abstain in a
mode; nullable or omitted shorthand is invalid. Method and display identity
belong to token issuance, not the submission JSON. POST through the Foldarium
selector API using your issued bearer token.

See `schemas/submission.schema.json` for the exact field set. Unknown keys are
rejected.
"""

CLIENT_TEMPLATE = '''#!/usr/bin/env python3
"""Standalone stdlib client for Foldarium weekly selector kits."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KIT_SCHEMA = "foldarium.weekly-selector-kit/v2"
SUBMISSION_SCHEMA = "foldarium.selector-submission/v2"
ENVIRONMENTS = ("production", "preview", "development")
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
MAX_SUBMISSION_PAYLOAD_BYTES = 65536
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FORBIDDEN_KEYS = frozenset({
    "accepted_correct", "answer", "answer_metadata", "artifact_sha256", "correct",
    "coordinates", "crystal", "private_index", "reference", "reference_uri",
    "reveal_manifest", "rmsd", "run_id", "sample_id", "score",
})


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strict_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{label} contains unknown keys: {sorted(unknown)}")


def _reject_forbidden(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        leaked = _FORBIDDEN_KEYS.intersection(value)
        if leaked:
            raise ValueError(f"{path} contains forbidden keys: {sorted(leaked)}")
        for key, child in value.items():
            _reject_forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden(child, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")


def _kit_sha256(manifest_body: dict[str, Any]) -> str:
    payload = {key: value for key, value in manifest_body.items() if key != "kit_sha256"}
    return _sha256(canonical_json(payload).encode("utf-8"))


def load_manifest_from_zip(kit_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(kit_path) as archive:
        return json.loads(archive.read("manifest.json"))


def verify_kit(kit_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(kit_path) as archive:
        names = archive.namelist()
        if sorted(names) != names:
            raise ValueError("kit ZIP paths must be sorted")
        for name in names:
            if archive.getinfo(name).date_time != ZIP_EPOCH:
                raise ValueError(f"kit ZIP entry {name} has non-canonical timestamp")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("schema_version") != KIT_SCHEMA:
            raise ValueError("unsupported kit schema_version")
        if manifest.get("environment") not in ENVIRONMENTS:
            raise ValueError("manifest.environment is invalid")
        blind_digest = manifest.get("blind_manifest_sha256")
        if not isinstance(blind_digest, str) or not _HASH_RE.fullmatch(blind_digest):
            raise ValueError("manifest.blind_manifest_sha256 is invalid")
        _reject_forbidden(manifest, "manifest")
        expected_digest = manifest.get("kit_sha256")
        if not isinstance(expected_digest, str) or not _HASH_RE.fullmatch(expected_digest):
            raise ValueError("manifest.kit_sha256 is invalid")
        if _kit_sha256(manifest) != expected_digest:
            raise ValueError("manifest.kit_sha256 is inconsistent with manifest content")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("manifest.files must be a non-empty list")
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError("manifest.files entries must be objects")
            path = entry.get("path")
            digest = entry.get("sha256")
            if not isinstance(path, str) or not path:
                raise ValueError("manifest.files entry missing path")
            if not isinstance(digest, str) or not _HASH_RE.fullmatch(digest):
                raise ValueError(f"manifest.files[{path}] has invalid sha256")
            if path not in names:
                raise ValueError(f"manifest.files references missing ZIP entry: {path}")
            content = archive.read(path)
            if _sha256(content) != digest:
                raise ValueError(f"ZIP entry hash mismatch for {path}")
        _kit_indexes(manifest)
        return manifest


def _kit_indexes(kit: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    indexes: dict[str, dict[str, set[str]]] = {}
    for item in kit["items"]:
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
            raise ValueError("manifest item_id is invalid")
        if item_id in indexes:
            raise ValueError(f"duplicate manifest item_id: {item_id}")
        choices = item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError(f"manifest item {item_id} has no choices")
        choice_ids: set[str] = set()
        cluster_ids: set[str] = set()
        for choice in choices:
            choice_id = choice.get("choice_id") if isinstance(choice, dict) else None
            cluster_id = choice.get("cluster_id") if isinstance(choice, dict) else None
            if not isinstance(choice_id, str) or not _ID_RE.fullmatch(choice_id):
                raise ValueError(f"manifest choice_id is invalid for item {item_id}")
            if choice_id in choice_ids:
                raise ValueError(f"duplicate manifest choice_id in item {item_id}: {choice_id}")
            if not isinstance(cluster_id, str) or not _ID_RE.fullmatch(cluster_id):
                raise ValueError(f"manifest cluster_id is invalid for item {item_id}")
            choice_ids.add(choice_id)
            cluster_ids.add(cluster_id)
        indexes[item_id] = {
            "choice_ids": choice_ids,
            "cluster_ids": cluster_ids,
        }
    return indexes


def build_submission_template(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "item_id": item["item_id"],
            "clustered": {"selection_kind": "none"},
            "unclustered": {"selection_kind": "none"},
        }
        for item in sorted(manifest["items"], key=lambda row: row["item_id"])
    ]


def validate_submission(raw: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    _strict_keys(raw, {
        "schema_version", "submission_id", "environment", "round_id",
        "blind_manifest_sha256", "kit_sha256", "items",
    }, "submission")
    _reject_forbidden(raw)
    if raw.get("schema_version") != SUBMISSION_SCHEMA:
        raise ValueError(f"submission.schema_version must be {SUBMISSION_SCHEMA}")
    submission_id = str(uuid.UUID(str(raw.get("submission_id"))))
    if raw.get("submission_id") != submission_id:
        raise ValueError("submission.submission_id must be a canonical lowercase UUID")
    environment = raw.get("environment")
    if environment not in ENVIRONMENTS:
        raise ValueError("submission.environment is invalid")
    if environment != manifest.get("environment"):
        raise ValueError("submission.environment does not match kit.environment")
    round_id = raw.get("round_id")
    if not isinstance(round_id, str) or not round_id.strip():
        raise ValueError("submission.round_id must be a non-empty string")
    if round_id != manifest.get("round_id"):
        raise ValueError("submission.round_id does not match kit.round_id")
    blind_manifest_sha256 = raw.get("blind_manifest_sha256")
    if (
        not isinstance(blind_manifest_sha256, str)
        or not _HASH_RE.fullmatch(blind_manifest_sha256)
    ):
        raise ValueError(
            "submission.blind_manifest_sha256 must be a lowercase SHA-256 digest"
        )
    if blind_manifest_sha256 != manifest.get("blind_manifest_sha256"):
        raise ValueError(
            "submission.blind_manifest_sha256 does not match kit.blind_manifest_sha256"
        )
    kit_sha256 = raw.get("kit_sha256")
    if not isinstance(kit_sha256, str) or not _HASH_RE.fullmatch(kit_sha256):
        raise ValueError("submission.kit_sha256 must be a lowercase SHA-256 digest")
    if kit_sha256 != manifest.get("kit_sha256"):
        raise ValueError("submission.kit_sha256 does not match kit.kit_sha256")
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("submission.items must be a non-empty list")
    indexes = _kit_indexes(manifest)
    expected_items = set(indexes)
    seen_items: set[str] = set()
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items_raw):
        if not isinstance(item, dict):
            raise ValueError(f"submission.items[{index}] must be an object")
        _strict_keys(item, {"item_id", "clustered", "unclustered"}, f"submission.items[{index}]")
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
            raise ValueError(f"submission.items[{index}].item_id is invalid")
        if item_id not in expected_items:
            raise ValueError(f"submission references unknown item_id: {item_id}")
        if item_id in seen_items:
            raise ValueError(f"duplicate submission item_id: {item_id}")
        seen_items.add(item_id)
        if "clustered" not in item or "unclustered" not in item:
            raise ValueError("each submission item must include clustered and unclustered")
        clustered = item["clustered"]
        if not isinstance(clustered, dict):
            raise ValueError(f"submission.items[{index}].clustered must be an object")
        if clustered.get("selection_kind") == "none":
            _strict_keys(clustered, {"selection_kind"}, f"submission.items[{index}].clustered")
            normalized_clustered = {"selection_kind": "none"}
        elif clustered.get("selection_kind") == "cluster":
            _strict_keys(
                clustered, {"selection_kind", "cluster_id"},
                f"submission.items[{index}].clustered",
            )
            cluster_id = clustered.get("cluster_id")
            if not isinstance(cluster_id, str) or not _ID_RE.fullmatch(cluster_id):
                raise ValueError(f"submission.items[{index}].clustered.cluster_id is invalid")
            if cluster_id not in indexes[item_id]["cluster_ids"]:
                raise ValueError(f"cluster_id is not valid for item {item_id}")
            normalized_clustered = {"selection_kind": "cluster", "cluster_id": cluster_id}
        else:
            raise ValueError(
                f"submission.items[{index}].clustered.selection_kind must be cluster or none"
            )
        unclustered = item["unclustered"]
        if not isinstance(unclustered, dict):
            raise ValueError(f"submission.items[{index}].unclustered must be an object")
        if unclustered.get("selection_kind") == "none":
            _strict_keys(
                unclustered, {"selection_kind"},
                f"submission.items[{index}].unclustered",
            )
            normalized_unclustered = {"selection_kind": "none"}
        elif unclustered.get("selection_kind") == "exact":
            _strict_keys(
                unclustered, {"selection_kind", "choice_id"},
                f"submission.items[{index}].unclustered",
            )
            choice_id = unclustered.get("choice_id")
            if not isinstance(choice_id, str) or not _ID_RE.fullmatch(choice_id):
                raise ValueError(f"submission.items[{index}].unclustered.choice_id is invalid")
            if choice_id not in indexes[item_id]["choice_ids"]:
                raise ValueError(f"choice_id is not valid for item {item_id}")
            normalized_unclustered = {"selection_kind": "exact", "choice_id": choice_id}
        else:
            raise ValueError(
                f"submission.items[{index}].unclustered.selection_kind must be exact or none"
            )
        normalized_items.append({
            "item_id": item_id,
            "clustered": normalized_clustered,
            "unclustered": normalized_unclustered,
        })
    if seen_items != expected_items:
        missing = sorted(expected_items - seen_items)
        raise ValueError(f"submission must include exactly one decision for every round item; missing {missing}")
    submitted_order = [item["item_id"] for item in normalized_items]
    if submitted_order != sorted(submitted_order):
        raise ValueError("submission payload is not in canonical item order")
    normalized = {
        "schema_version": SUBMISSION_SCHEMA,
        "submission_id": submission_id.lower(),
        "environment": environment,
        "round_id": round_id,
        "blind_manifest_sha256": blind_manifest_sha256,
        "kit_sha256": kit_sha256,
        "items": sorted(normalized_items, key=lambda row: row["item_id"]),
    }
    if len(canonical_json(normalized).encode("utf-8")) > MAX_SUBMISSION_PAYLOAD_BYTES:
        raise ValueError(f"submission payload exceeds {MAX_SUBMISSION_PAYLOAD_BYTES} bytes")
    return normalized


def build_submission(
    manifest: dict[str, Any],
    *,
    submission_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return validate_submission({
        "schema_version": SUBMISSION_SCHEMA,
        "submission_id": submission_id,
        "environment": manifest["environment"],
        "round_id": manifest["round_id"],
        "blind_manifest_sha256": manifest["blind_manifest_sha256"],
        "kit_sha256": manifest["kit_sha256"],
        "items": sorted(
            items,
            key=lambda row: row.get("item_id", "") if isinstance(row, dict) else "",
        ),
    }, manifest)


def digest_submission(submission: dict[str, Any]) -> str:
    return _sha256(canonical_json(submission).encode("utf-8"))


def submit_submission(api_url: str, bearer_token: str, submission: dict[str, Any]) -> dict[str, Any]:
    body = canonical_json(submission).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"submit failed ({error.code}): {detail}") from error
    except URLError as error:
        raise ValueError(f"submit failed: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify", help="verify kit ZIP hashes and manifest binding")
    verify_parser.add_argument("kit", type=Path)

    template_parser = subparsers.add_parser("template", help="print a complete submission template")
    template_parser.add_argument("kit", type=Path)

    validate_parser = subparsers.add_parser("validate", help="validate a completed submission JSON")
    validate_parser.add_argument("kit", type=Path)
    validate_parser.add_argument("submission", type=Path)

    submit_parser = subparsers.add_parser("submit", help="validate and POST a submission")
    submit_parser.add_argument("kit", type=Path)
    submit_parser.add_argument("submission", type=Path)
    submit_parser.add_argument("--api-url", required=True)
    submit_parser.add_argument("--bearer-token", required=True)

    build_parser = subparsers.add_parser("build", help="build a submission from decisions JSON")
    build_parser.add_argument("kit", type=Path)
    build_parser.add_argument("--submission-id", default=str(uuid.uuid4()))
    build_parser.add_argument("--items-json", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            manifest = verify_kit(args.kit)
            print(canonical_json({
                "ok": True,
                "environment": manifest["environment"],
                "round_id": manifest["round_id"],
                "blind_manifest_sha256": manifest["blind_manifest_sha256"],
                "kit_sha256": manifest["kit_sha256"],
                "item_count": len(manifest["items"]),
            }))
            return 0
        manifest = verify_kit(args.kit)
        if args.command == "template":
            print(canonical_json(build_submission_template(manifest)))
            return 0
        if args.command == "validate":
            submission = json.loads(args.submission.read_text(encoding="utf-8"))
            print(canonical_json(validate_submission(submission, manifest)))
            return 0
        if args.command == "build":
            items = json.loads(args.items_json.read_text(encoding="utf-8"))
            submission = build_submission(
                manifest,
                submission_id=args.submission_id,
                items=items,
            )
            print(canonical_json(submission))
            return 0
        if args.command == "submit":
            submission = json.loads(args.submission.read_text(encoding="utf-8"))
            normalized = validate_submission(submission, manifest)
            receipt = submit_submission(args.api_url, args.bearer_token, normalized)
            print(canonical_json(receipt))
            return 0
    except (ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
'''


class WeeklySelectorError(ValueError):
    """Raised when a selector kit or submission violates the public contract."""


def canonical_json(value: Any) -> str:
    """Return canonical finite JSON for selector identities and artifacts."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise WeeklySelectorError(f"value is not finite canonical JSON: {error}") from error


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WeeklySelectorError(f"{field} must be an object")
    return deepcopy(dict(value))


def _nonempty(value: Any, field: str, *, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeeklySelectorError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise WeeklySelectorError(f"{field} exceeds {maximum} characters")
    return result


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise WeeklySelectorError(f"{field} must be a safe identifier")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise WeeklySelectorError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _environment(value: Any, field: str) -> str:
    if value not in SELECTOR_ENVIRONMENTS:
        raise WeeklySelectorError(
            f"{field} must be production, preview, or development"
        )
    return value


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise WeeklySelectorError(f"{field} must be a UUID") from error


def _strict_keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = set(value) - set(allowed)
    if unknown:
        raise WeeklySelectorError(f"{label} contains unknown keys: {sorted(unknown)}")


def _looks_like_coordinate_array(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value):
        return len(value) in (3, 4)
    if all(isinstance(item, list) for item in value):
        if not value:
            return False
        return all(
            isinstance(coord, (int, float)) and not isinstance(coord, bool)
            for row in value
            for coord in row
        )
    return False


def assert_no_forbidden_content(value: Any, *, path: str = "value") -> None:
    """Recursively reject reveal/private keys, coordinates, and non-finite numbers."""

    if isinstance(value, Mapping):
        leaked = FORBIDDEN_KEYS.intersection(value)
        if leaked:
            raise WeeklySelectorError(f"{path} contains forbidden keys: {sorted(leaked)}")
        for key, child in value.items():
            if key in FORBIDDEN_COORDINATE_KEYS:
                raise WeeklySelectorError(f"{path}.{key} is forbidden")
            assert_no_forbidden_content(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        if _looks_like_coordinate_array(value):
            raise WeeklySelectorError(f"{path} contains a forbidden coordinate array")
        for index, child in enumerate(value):
            assert_no_forbidden_content(child, path=f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise WeeklySelectorError(f"{path} contains a non-finite number")


def build_submission_schema() -> dict[str, Any]:
    none_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["selection_kind"],
        "properties": {"selection_kind": {"const": "none"}},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Foldarium selector submission",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "submission_id",
            "environment",
            "round_id",
            "blind_manifest_sha256",
            "kit_sha256",
            "items",
        ],
        "properties": {
            "schema_version": {"const": SUBMISSION_SCHEMA_VERSION},
            "submission_id": {"type": "string", "format": "uuid"},
            "environment": {"enum": list(SELECTOR_ENVIRONMENTS)},
            "round_id": {"type": "string", "minLength": 1},
            "blind_manifest_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
            "kit_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_id", "clustered", "unclustered"],
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                        },
                        "clustered": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["selection_kind", "cluster_id"],
                                    "properties": {
                                        "selection_kind": {"const": "cluster"},
                                        "cluster_id": {
                                            "type": "string",
                                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                                        },
                                    },
                                },
                                deepcopy(none_decision),
                            ]
                        },
                        "unclustered": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["selection_kind", "choice_id"],
                                    "properties": {
                                        "selection_kind": {"const": "exact"},
                                        "choice_id": {
                                            "type": "string",
                                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                                        },
                                    },
                                },
                                deepcopy(none_decision),
                            ]
                        },
                    },
                },
            },
        },
    }


def validate_blind_manifest(raw: Any, *, round_id: str) -> dict[str, Any]:
    blind = _object(raw, "blind_manifest")
    if blind.get("schema_version") != QUIZ_SCHEMA_VERSION:
        raise WeeklySelectorError("blind_manifest.schema_version must be 1")
    manifest_round_id = _nonempty(blind.get("round_id"), "blind_manifest.round_id")
    if manifest_round_id != round_id:
        raise WeeklySelectorError("blind_manifest.round_id does not match round_id")
    items_raw = blind.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise WeeklySelectorError("blind_manifest.items must be a non-empty list")

    normalized_items: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for index, raw_item in enumerate(items_raw):
        item = _object(raw_item, f"blind_manifest.items[{index}]")
        item_id = _identifier(item.get("id"), f"blind_manifest.items[{index}].id")
        if item_id in seen_items:
            raise WeeklySelectorError(f"duplicate blind item id: {item_id}")
        seen_items.add(item_id)
        choices_raw = item.get("choices")
        if not isinstance(choices_raw, list) or not choices_raw:
            raise WeeklySelectorError(f"blind_manifest.items[{index}].choices must be non-empty")

        normalized_choices: list[dict[str, Any]] = []
        seen_choices: set[str] = set()
        for choice_index, raw_choice in enumerate(choices_raw):
            choice = _object(raw_choice, f"blind_manifest.items[{index}].choices[{choice_index}]")
            choice_id = _identifier(
                choice.get("id"), f"blind_manifest.items[{index}].choices[{choice_index}].id"
            )
            if choice_id in seen_choices:
                raise WeeklySelectorError(f"duplicate blind choice id in {item_id}: {choice_id}")
            seen_choices.add(choice_id)
            cluster_id = _identifier(
                choice.get("cluster_id"),
                f"blind_manifest.items[{index}].choices[{choice_index}].cluster_id",
            )
            if not isinstance(choice.get("is_rep"), bool):
                raise WeeklySelectorError(
                    f"blind_manifest.items[{index}].choices[{choice_index}].is_rep must be boolean"
                )
            for uri_key in ("pose_uri", "protein_uri", "pocket_uri"):
                _nonempty(
                    choice.get(uri_key),
                    f"blind_manifest.items[{index}].choices[{choice_index}].{uri_key}",
                    maximum=4096,
                )
            normalized_choices.append(
                {
                    "choice_id": choice_id,
                    "cluster_id": cluster_id,
                    "is_rep": choice["is_rep"],
                    "pose_uri": choice["pose_uri"].strip(),
                    "protein_uri": choice["protein_uri"].strip(),
                    "pocket_uri": choice["pocket_uri"].strip(),
                }
            )
        normalized_items.append({"item_id": item_id, "choices": normalized_choices})

    normalized_items.sort(key=lambda item: item["item_id"])
    manifest = {
        "schema_version": QUIZ_SCHEMA_VERSION,
        "round_id": manifest_round_id,
        "items": normalized_items,
        "blind_manifest_sha256": manifest_sha256(blind),
    }
    assert_no_forbidden_content(manifest, path="blind_manifest")
    return manifest


def _asset_path(item_id: str, choice_id: str, kind: str) -> str:
    extension = ASSET_EXTENSIONS[kind]
    return f"items/{item_id}/choices/{choice_id}/{kind}.{extension}"


def _target_path(item_id: str) -> str:
    return f"items/{item_id}/target.json"


def _kit_sha256(manifest_body: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest_body.items() if key != "kit_sha256"}
    return _sha256(canonical_json(payload).encode("utf-8"))


def _build_manifest_body(
    *,
    environment: str,
    round_id: str,
    blind_manifest_sha256: str,
    normalized_targets: dict[str, dict[str, Any]],
    blind_items: list[dict[str, Any]],
    asset_descriptors: dict[str, dict[str, Any]],
    prompt_profile: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    kit_items: list[dict[str, Any]] = []
    for blind_item in blind_items:
        item_id = blind_item["item_id"]
        choices: list[dict[str, Any]] = []
        for choice in blind_item["choices"]:
            choice_id = choice["choice_id"]
            assets = {
                kind: asset_descriptors[_asset_path(item_id, choice_id, kind)]
                for kind in ASSET_KINDS
            }
            choices.append(
                {
                    "choice_id": choice_id,
                    "cluster_id": choice["cluster_id"],
                    "is_rep": choice["is_rep"],
                    "descriptors": {
                        "pose_uri": choice["pose_uri"],
                        "protein_uri": choice["protein_uri"],
                        "pocket_uri": choice["pocket_uri"],
                    },
                    "assets": assets,
                }
            )
        choices.sort(key=lambda row: row["choice_id"])
        kit_items.append(
            {
                "item_id": item_id,
                "target": normalized_targets[item_id],
                "choices": choices,
            }
        )
    kit_items.sort(key=lambda item: item["item_id"])
    return {
        "schema_version": KIT_SCHEMA_VERSION,
        "environment": environment,
        "round_id": round_id,
        "blind_manifest_sha256": blind_manifest_sha256,
        "prompt_profile": prompt_profile,
        "policies": {
            "target_schema_version": SCHEMA_VERSION,
            "decision_modes": ["clustered", "unclustered"],
            "abstention": 'Use {"selection_kind":"none"} independently per mode.',
        },
        "items": kit_items,
        "files": files,
    }


def _deterministic_zip(entries: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(entries):
            info = zipfile.ZipInfo(path)
            info.date_time = ZIP_EPOCH
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ZIP_FILE_MODE
            archive.writestr(info, entries[path])
    return buffer.getvalue()


def build_selector_kit(
    *,
    round_id: str,
    environment: str = "production",
    blind_manifest: Mapping[str, Any],
    targets_by_item_id: Mapping[str, Mapping[str, Any]],
    assets_by_choice: Mapping[tuple[str, str], Mapping[str, bytes]],
    policies: Mapping[str, Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Build a deterministic selector kit ZIP and public descriptor."""

    round_id = _nonempty(round_id, "round_id")
    environment = _environment(environment, "environment")
    validated_blind = validate_blind_manifest(blind_manifest, round_id=round_id)
    assert_no_forbidden_content(blind_manifest, path="blind_manifest")

    if not targets_by_item_id:
        raise WeeklySelectorError("targets_by_item_id must be non-empty")

    normalized_targets: dict[str, dict[str, Any]] = {}
    for item_id, raw_target in targets_by_item_id.items():
        item_id = _identifier(item_id, "targets_by_item_id key")
        try:
            normalized_targets[item_id] = validate_target(raw_target)
        except ContractError as error:
            raise WeeklySelectorError(f"target for {item_id} is invalid: {error}") from error
        assert_no_forbidden_content(normalized_targets[item_id], path=f"targets[{item_id}]")

    blind_item_ids = {item["item_id"] for item in validated_blind["items"]}
    if set(normalized_targets) != blind_item_ids:
        missing = sorted(blind_item_ids - set(normalized_targets))
        extra = sorted(set(normalized_targets) - blind_item_ids)
        details: list[str] = []
        if missing:
            details.append(f"missing targets for {missing}")
        if extra:
            details.append(f"unexpected targets for {extra}")
        raise WeeklySelectorError("; ".join(details))

    entries: dict[str, bytes] = {}
    asset_descriptors: dict[str, dict[str, Any]] = {}
    for blind_item in validated_blind["items"]:
        item_id = blind_item["item_id"]
        target_bytes = (canonical_json(normalized_targets[item_id]) + "\n").encode("utf-8")
        entries[_target_path(item_id)] = target_bytes
        for choice in blind_item["choices"]:
            choice_id = choice["choice_id"]
            provided = assets_by_choice.get((item_id, choice_id))
            if not isinstance(provided, Mapping):
                raise WeeklySelectorError(
                    f"missing assets for blind choice {item_id}/{choice_id}"
                )
            for kind in ASSET_KINDS:
                content = provided.get(kind)
                if not isinstance(content, (bytes, bytearray)) or not content:
                    raise WeeklySelectorError(
                        f"missing {kind} bytes for blind choice {item_id}/{choice_id}"
                    )
                path = _asset_path(item_id, choice_id, kind)
                entries[path] = bytes(content)
                asset_descriptors[path] = {
                    "path": path,
                    "sha256": _sha256(entries[path]),
                    "size_bytes": len(entries[path]),
                    "media_type": ASSET_MEDIA_TYPES[kind],
                }

    schema_bytes = (canonical_json(build_submission_schema()) + "\n").encode("utf-8")
    model_response_schema_bytes = (
        canonical_json(SELECTOR_MODEL_RESPONSE_SCHEMA) + "\n"
    ).encode("utf-8")
    prompt_profile = selector_prompt_profile()
    prompt_profile_bytes = (canonical_json(prompt_profile) + "\n").encode("utf-8")
    system_prompt_bytes = SELECTOR_SYSTEM_PROMPT.encode("utf-8")
    item_prompt_template_bytes = SELECTOR_ITEM_PROMPT_TEMPLATE.encode("utf-8")
    client_bytes = CLIENT_TEMPLATE.encode("utf-8")
    readme_bytes = README_TEMPLATE.encode("utf-8")
    entries["README.md"] = readme_bytes
    entries["client/foldarium_selector_client.py"] = client_bytes
    entries["prompts/item-template.txt"] = item_prompt_template_bytes
    entries["prompts/profile.json"] = prompt_profile_bytes
    entries["prompts/system.txt"] = system_prompt_bytes
    entries["schemas/model-response.schema.json"] = model_response_schema_bytes
    entries["schemas/submission.schema.json"] = schema_bytes

    static_files = [
        ("README.md", readme_bytes),
        ("client/foldarium_selector_client.py", client_bytes),
        ("prompts/item-template.txt", item_prompt_template_bytes),
        ("prompts/profile.json", prompt_profile_bytes),
        ("prompts/system.txt", system_prompt_bytes),
        ("schemas/model-response.schema.json", model_response_schema_bytes),
        ("schemas/submission.schema.json", schema_bytes),
    ]
    for item_id in sorted(normalized_targets):
        static_files.append((_target_path(item_id), entries[_target_path(item_id)]))
    for blind_item in validated_blind["items"]:
        item_id = blind_item["item_id"]
        for choice in blind_item["choices"]:
            choice_id = choice["choice_id"]
            for kind in ASSET_KINDS:
                path = _asset_path(item_id, choice_id, kind)
                static_files.append((path, entries[path]))

    files_manifest = [
        {
            "path": path,
            "sha256": _sha256(content),
            "size_bytes": len(content),
        }
        for path, content in sorted(static_files, key=lambda row: row[0])
    ]

    manifest_body = _build_manifest_body(
        environment=environment,
        round_id=round_id,
        blind_manifest_sha256=validated_blind["blind_manifest_sha256"],
        normalized_targets=normalized_targets,
        blind_items=validated_blind["items"],
        asset_descriptors=asset_descriptors,
        prompt_profile=prompt_profile,
        files=files_manifest,
    )
    if policies:
        manifest_body["policies"] = {
            **manifest_body["policies"],
            **deepcopy(dict(policies)),
        }
    assert_no_forbidden_content(manifest_body, path="manifest")

    manifest_body["kit_sha256"] = _kit_sha256(manifest_body)
    manifest_bytes = (canonical_json(manifest_body) + "\n").encode("utf-8")
    entries["manifest.json"] = manifest_bytes
    zip_bytes = _deterministic_zip(entries)

    descriptor = {
        "schema_version": KIT_SCHEMA_VERSION,
        "environment": environment,
        "round_id": round_id,
        "kit_sha256": manifest_body["kit_sha256"],
        "blind_manifest_sha256": validated_blind["blind_manifest_sha256"],
        "size_bytes": len(zip_bytes),
        "item_count": len(validated_blind["items"]),
        "choice_count": sum(len(item["choices"]) for item in validated_blind["items"]),
    }
    assert_no_forbidden_content(descriptor, path="descriptor")
    return zip_bytes, descriptor


def verify_selector_kit_zip(
    zip_bytes: bytes,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify ZIP entry hashes and deterministic manifest binding."""

    if not isinstance(zip_bytes, (bytes, bytearray)) or not zip_bytes:
        raise WeeklySelectorError("kit ZIP must be non-empty bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            if sorted(names) != names:
                raise WeeklySelectorError("kit ZIP paths must be sorted")
            for path in names:
                info = archive.getinfo(path)
                if info.date_time != ZIP_EPOCH:
                    raise WeeklySelectorError(f"kit ZIP entry {path} has non-canonical timestamp")
            if "manifest.json" not in names:
                raise WeeklySelectorError("kit ZIP is missing manifest.json")
            loaded_manifest = json.loads(archive.read("manifest.json"))
            validated = validate_selector_kit_manifest(
                manifest if manifest is not None else loaded_manifest
            )
            files_raw = validated.get("files")
            if not isinstance(files_raw, list) or not files_raw:
                raise WeeklySelectorError("manifest.files must be a non-empty list")
            for entry in files_raw:
                if not isinstance(entry, Mapping):
                    raise WeeklySelectorError("manifest.files entries must be objects")
                path = entry.get("path")
                digest = entry.get("sha256")
                if not isinstance(path, str) or not path:
                    raise WeeklySelectorError("manifest.files entry missing path")
                digest = _hash(digest, f"manifest.files[{path}].sha256")
                if path not in names:
                    raise WeeklySelectorError(
                        f"manifest.files references missing ZIP entry: {path}"
                    )
                content = archive.read(path)
                if _sha256(content) != digest:
                    raise WeeklySelectorError(f"ZIP entry hash mismatch for {path}")
    except zipfile.BadZipFile as error:
        raise WeeklySelectorError(f"kit ZIP is corrupt: {error}") from error
    except json.JSONDecodeError as error:
        raise WeeklySelectorError(f"manifest.json is invalid JSON: {error}") from error
    return validated


def parse_selector_kit(zip_bytes: bytes, *, verify_hashes: bool = False) -> dict[str, Any]:
    """Load and validate a selector kit manifest from ZIP bytes."""

    if verify_hashes:
        return verify_selector_kit_zip(zip_bytes)
    if not isinstance(zip_bytes, (bytes, bytearray)) or not zip_bytes:
        raise WeeklySelectorError("kit ZIP must be non-empty bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            names = archive.namelist()
            if sorted(names) != names:
                raise WeeklySelectorError("kit ZIP paths must be sorted")
            if "manifest.json" not in names:
                raise WeeklySelectorError("kit ZIP is missing manifest.json")
            manifest = json.loads(archive.read("manifest.json"))
            for path in names:
                info = archive.getinfo(path)
                if info.date_time != ZIP_EPOCH:
                    raise WeeklySelectorError(f"kit ZIP entry {path} has non-canonical timestamp")
    except zipfile.BadZipFile as error:
        raise WeeklySelectorError(f"kit ZIP is corrupt: {error}") from error
    except json.JSONDecodeError as error:
        raise WeeklySelectorError(f"manifest.json is invalid JSON: {error}") from error

    return validate_selector_kit_manifest(manifest)


def validate_selector_kit_manifest(
    raw: Any,
    *,
    expected_kit_sha256: str | None = None,
) -> dict[str, Any]:
    manifest = _object(raw, "manifest")
    _strict_keys(
        manifest,
        {
            "schema_version",
            "environment",
            "round_id",
            "kit_sha256",
            "blind_manifest_sha256",
            "prompt_profile",
            "policies",
            "items",
            "files",
        },
        "manifest",
    )
    if manifest.get("schema_version") != KIT_SCHEMA_VERSION:
        raise WeeklySelectorError(f"manifest.schema_version must be {KIT_SCHEMA_VERSION}")
    environment = _environment(manifest.get("environment"), "manifest.environment")
    round_id = _nonempty(manifest.get("round_id"), "manifest.round_id")
    kit_sha256 = _hash(manifest.get("kit_sha256"), "manifest.kit_sha256")
    if expected_kit_sha256 is not None and kit_sha256 != expected_kit_sha256:
        raise WeeklySelectorError("manifest.kit_sha256 does not match expected digest")
    blind_manifest_sha256 = _hash(
        manifest.get("blind_manifest_sha256"), "manifest.blind_manifest_sha256"
    )
    prompt_profile = _object(
        manifest.get("prompt_profile"), "manifest.prompt_profile"
    )
    if prompt_profile != selector_prompt_profile():
        raise WeeklySelectorError(
            "manifest.prompt_profile is not the canonical registered profile"
        )

    items_raw = manifest.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise WeeklySelectorError("manifest.items must be a non-empty list")
    normalized_items: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for index, raw_item in enumerate(items_raw):
        item = _object(raw_item, f"manifest.items[{index}]")
        _strict_keys(item, {"item_id", "target", "choices"}, f"manifest.items[{index}]")
        item_id = _identifier(item.get("item_id"), f"manifest.items[{index}].item_id")
        if item_id in seen_items:
            raise WeeklySelectorError(f"duplicate manifest item id: {item_id}")
        seen_items.add(item_id)
        try:
            normalized_target = validate_target(item.get("target"))
        except ContractError as error:
            raise WeeklySelectorError(
                f"manifest.items[{index}].target is invalid: {error}"
            ) from error
        choices_raw = item.get("choices")
        if not isinstance(choices_raw, list) or not choices_raw:
            raise WeeklySelectorError(f"manifest.items[{index}].choices must be non-empty")
        normalized_choices: list[dict[str, Any]] = []
        seen_choices: set[str] = set()
        for choice_index, raw_choice in enumerate(choices_raw):
            choice = _object(raw_choice, f"manifest.items[{index}].choices[{choice_index}]")
            _strict_keys(
                choice,
                {"choice_id", "cluster_id", "is_rep", "descriptors", "assets"},
                f"manifest.items[{index}].choices[{choice_index}]",
            )
            choice_id = _identifier(
                choice.get("choice_id"),
                f"manifest.items[{index}].choices[{choice_index}].choice_id",
            )
            if choice_id in seen_choices:
                raise WeeklySelectorError(f"duplicate manifest choice id in {item_id}: {choice_id}")
            seen_choices.add(choice_id)
            cluster_id = _identifier(
                choice.get("cluster_id"),
                f"manifest.items[{index}].choices[{choice_index}].cluster_id",
            )
            if not isinstance(choice.get("is_rep"), bool):
                raise WeeklySelectorError(
                    f"manifest.items[{index}].choices[{choice_index}].is_rep must be boolean"
                )
            descriptors = _object(
                choice.get("descriptors"),
                f"manifest.items[{index}].choices[{choice_index}].descriptors",
            )
            _strict_keys(
                descriptors,
                {"pose_uri", "protein_uri", "pocket_uri"},
                f"manifest.items[{index}].choices[{choice_index}].descriptors",
            )
            for uri_key in ("pose_uri", "protein_uri", "pocket_uri"):
                _nonempty(
                    descriptors.get(uri_key),
                    f"manifest.items[{index}].choices[{choice_index}].descriptors.{uri_key}",
                    maximum=4096,
                )
            assets = _object(
                choice.get("assets"),
                f"manifest.items[{index}].choices[{choice_index}].assets",
            )
            _strict_keys(
                assets,
                set(ASSET_KINDS),
                f"manifest.items[{index}].choices[{choice_index}].assets",
            )
            normalized_assets: dict[str, Any] = {}
            for kind in ASSET_KINDS:
                asset = _object(
                    assets.get(kind),
                    f"manifest.items[{index}].choices[{choice_index}].assets.{kind}",
                )
                _strict_keys(asset, {"path", "sha256", "size_bytes", "media_type"}, f"assets.{kind}")
                normalized_assets[kind] = {
                    "path": _nonempty(asset.get("path"), f"assets.{kind}.path", maximum=4096),
                    "sha256": _hash(asset.get("sha256"), f"assets.{kind}.sha256"),
                    "size_bytes": asset.get("size_bytes"),
                    "media_type": _nonempty(asset.get("media_type"), f"assets.{kind}.media_type"),
                }
                if (
                    not isinstance(normalized_assets[kind]["size_bytes"], int)
                    or normalized_assets[kind]["size_bytes"] < 1
                ):
                    raise WeeklySelectorError(f"assets.{kind}.size_bytes must be a positive integer")
            normalized_choices.append(
                {
                    "choice_id": choice_id,
                    "cluster_id": cluster_id,
                    "is_rep": choice["is_rep"],
                    "descriptors": {
                        "pose_uri": descriptors["pose_uri"].strip(),
                        "protein_uri": descriptors["protein_uri"].strip(),
                        "pocket_uri": descriptors["pocket_uri"].strip(),
                    },
                    "assets": normalized_assets,
                }
            )
        normalized_choices.sort(key=lambda row: row["choice_id"])
        normalized_items.append(
            {
                "item_id": item_id,
                "target": normalized_target,
                "choices": normalized_choices,
            }
        )
    normalized_items.sort(key=lambda item: item["item_id"])

    files_raw = manifest.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise WeeklySelectorError("manifest.files must be a non-empty list")

    normalized = {
        "schema_version": KIT_SCHEMA_VERSION,
        "environment": environment,
        "round_id": round_id,
        "kit_sha256": kit_sha256,
        "blind_manifest_sha256": blind_manifest_sha256,
        "prompt_profile": prompt_profile,
        "policies": _object(manifest.get("policies"), "manifest.policies"),
        "items": normalized_items,
        "files": deepcopy(files_raw),
    }
    if _kit_sha256(normalized) != kit_sha256:
        raise WeeklySelectorError("manifest.kit_sha256 is inconsistent with manifest content")
    assert_no_forbidden_content(normalized, path="manifest")
    return normalized


def _kit_indexes(kit: Mapping[str, Any]) -> dict[str, dict[str, set[str]]]:
    indexes: dict[str, dict[str, set[str]]] = {}
    for item in kit["items"]:
        indexes[item["item_id"]] = {
            "choice_ids": {choice["choice_id"] for choice in item["choices"]},
            "cluster_ids": {choice["cluster_id"] for choice in item["choices"]},
        }
    return indexes


def build_submission_template(kit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return a complete abstention template for every kit item."""

    return [
        {
            "item_id": item["item_id"],
            "clustered": {"selection_kind": "none"},
            "unclustered": {"selection_kind": "none"},
        }
        for item in sorted(kit["items"], key=lambda row: row["item_id"])
    ]


def build_selector_submission(
    kit: Mapping[str, Any],
    *,
    submission_id: str,
    items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build and validate a canonical selector submission."""

    return validate_selector_submission(
        {
            "schema_version": SUBMISSION_SCHEMA_VERSION,
            "submission_id": submission_id,
            "environment": kit["environment"],
            "round_id": kit["round_id"],
            "blind_manifest_sha256": kit["blind_manifest_sha256"],
            "kit_sha256": kit["kit_sha256"],
            "items": sorted(
                items,
                key=lambda row: (
                    row.get("item_id", "") if isinstance(row, Mapping) else ""
                ),
            ),
        },
        kit,
    )


def digest_selector_submission(submission: Mapping[str, Any]) -> str:
    """Return the SHA-256 digest of a validated submission payload."""

    return _sha256(canonical_json(dict(submission)).encode("utf-8"))


def submit_selector_submission(
    api_url: str,
    bearer_token: str,
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """POST a validated submission to the selector API using stdlib only."""

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    body = canonical_json(dict(submission)).encode("utf-8")
    request = Request(
        api_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise WeeklySelectorError(f"submit failed ({error.code}): {detail}") from error
    except URLError as error:
        raise WeeklySelectorError(f"submit failed: {error}") from error


def validate_selector_submission(
    raw: Mapping[str, Any],
    kit: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a complete selector submission against a kit manifest."""

    submission = _object(raw, "submission")
    assert_no_forbidden_content(submission, path="submission")
    _strict_keys(submission, SUBMISSION_TOP_KEYS, "submission")
    if submission.get("schema_version") != SUBMISSION_SCHEMA_VERSION:
        raise WeeklySelectorError(
            f"submission.schema_version must be {SUBMISSION_SCHEMA_VERSION}"
        )
    submission_id = _uuid(submission.get("submission_id"), "submission.submission_id")
    if submission.get("submission_id") != submission_id:
        raise WeeklySelectorError(
            "submission.submission_id must be a canonical lowercase UUID"
        )
    environment = _environment(
        submission.get("environment"), "submission.environment"
    )
    if environment != kit.get("environment"):
        raise WeeklySelectorError(
            "submission.environment does not match kit.environment"
        )
    round_id = _nonempty(submission.get("round_id"), "submission.round_id")
    if round_id != kit.get("round_id"):
        raise WeeklySelectorError("submission.round_id does not match kit.round_id")
    blind_manifest_sha256 = _hash(
        submission.get("blind_manifest_sha256"),
        "submission.blind_manifest_sha256",
    )
    if blind_manifest_sha256 != kit.get("blind_manifest_sha256"):
        raise WeeklySelectorError(
            "submission.blind_manifest_sha256 does not match kit.blind_manifest_sha256"
        )
    kit_sha256 = _hash(submission.get("kit_sha256"), "submission.kit_sha256")
    if kit_sha256 != kit.get("kit_sha256"):
        raise WeeklySelectorError("submission.kit_sha256 does not match kit.kit_sha256")

    items_raw = submission.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise WeeklySelectorError("submission.items must be a non-empty list")

    indexes = _kit_indexes(kit)
    expected_items = set(indexes)
    seen_items: set[str] = set()
    normalized_items: list[dict[str, Any]] = []

    for index, raw_item in enumerate(items_raw):
        item = _object(raw_item, f"submission.items[{index}]")
        _strict_keys(item, SUBMISSION_ITEM_KEYS, f"submission.items[{index}]")
        item_id = _identifier(item.get("item_id"), f"submission.items[{index}].item_id")
        if item_id not in expected_items:
            raise WeeklySelectorError(f"submission references unknown item_id: {item_id}")
        if item_id in seen_items:
            raise WeeklySelectorError(f"duplicate submission item_id: {item_id}")
        seen_items.add(item_id)

        if "clustered" not in item:
            raise WeeklySelectorError(
                f"submission.items[{index}] must include clustered"
            )
        if "unclustered" not in item:
            raise WeeklySelectorError(
                f"submission.items[{index}] must include unclustered"
            )

        clustered = _object(
            item.get("clustered"), f"submission.items[{index}].clustered"
        )
        clustered_kind = clustered.get("selection_kind")
        if clustered_kind == "none":
            _strict_keys(
                clustered,
                {"selection_kind"},
                f"submission.items[{index}].clustered",
            )
            normalized_clustered = {"selection_kind": "none"}
        elif clustered_kind == "cluster":
            _strict_keys(
                clustered,
                CLUSTER_DECISION_KEYS,
                f"submission.items[{index}].clustered",
            )
            if "cluster_id" not in clustered:
                raise WeeklySelectorError(
                    f"submission.items[{index}].clustered must include cluster_id"
                )
            cluster_id = _identifier(
                clustered.get("cluster_id"),
                f"submission.items[{index}].clustered.cluster_id",
            )
            if cluster_id not in indexes[item_id]["cluster_ids"]:
                raise WeeklySelectorError(f"cluster_id is not valid for item {item_id}")
            normalized_clustered = {
                "selection_kind": "cluster",
                "cluster_id": cluster_id,
            }
        else:
            raise WeeklySelectorError(
                f"submission.items[{index}].clustered.selection_kind "
                "must be cluster or none"
            )

        unclustered = _object(
            item.get("unclustered"), f"submission.items[{index}].unclustered"
        )
        unclustered_kind = unclustered.get("selection_kind")
        if unclustered_kind == "none":
            _strict_keys(
                unclustered,
                {"selection_kind"},
                f"submission.items[{index}].unclustered",
            )
            normalized_unclustered = {"selection_kind": "none"}
        elif unclustered_kind == "exact":
            _strict_keys(
                unclustered,
                EXACT_DECISION_KEYS,
                f"submission.items[{index}].unclustered",
            )
            if "choice_id" not in unclustered:
                raise WeeklySelectorError(
                    f"submission.items[{index}].unclustered must include choice_id"
                )
            choice_id = _identifier(
                unclustered.get("choice_id"),
                f"submission.items[{index}].unclustered.choice_id",
            )
            if choice_id not in indexes[item_id]["choice_ids"]:
                raise WeeklySelectorError(f"choice_id is not valid for item {item_id}")
            normalized_unclustered = {
                "selection_kind": "exact",
                "choice_id": choice_id,
            }
        else:
            raise WeeklySelectorError(
                f"submission.items[{index}].unclustered.selection_kind "
                "must be exact or none"
            )

        normalized_items.append(
            {
                "item_id": item_id,
                "clustered": normalized_clustered,
                "unclustered": normalized_unclustered,
            }
        )

    if seen_items != expected_items:
        missing = sorted(expected_items - seen_items)
        raise WeeklySelectorError(
            f"submission must include exactly one decision for every round item; missing {missing}"
        )
    submitted_order = [item["item_id"] for item in normalized_items]
    if submitted_order != sorted(submitted_order):
        raise WeeklySelectorError(
            "submission payload is not in canonical item order"
        )

    normalized = {
        "schema_version": SUBMISSION_SCHEMA_VERSION,
        "submission_id": submission_id.lower(),
        "environment": environment,
        "round_id": round_id,
        "blind_manifest_sha256": blind_manifest_sha256,
        "kit_sha256": kit_sha256,
        "items": sorted(normalized_items, key=lambda row: row["item_id"]),
    }
    payload_bytes = len(canonical_json(normalized).encode("utf-8"))
    if payload_bytes > MAX_SUBMISSION_PAYLOAD_BYTES:
        raise WeeklySelectorError(
            f"submission payload exceeds {MAX_SUBMISSION_PAYLOAD_BYTES} bytes"
        )
    assert_no_forbidden_content(normalized, path="submission")
    return normalized


__all__ = [
    "CLIENT_TEMPLATE",
    "FORBIDDEN_KEYS",
    "KIT_SCHEMA_VERSION",
    "MAX_SUBMISSION_PAYLOAD_BYTES",
    "SELECTOR_ENVIRONMENTS",
    "SUBMISSION_SCHEMA_VERSION",
    "WeeklySelectorError",
    "assert_no_forbidden_content",
    "build_selector_kit",
    "build_selector_submission",
    "build_submission_schema",
    "build_submission_template",
    "canonical_json",
    "digest_selector_submission",
    "parse_selector_kit",
    "submit_selector_submission",
    "validate_blind_manifest",
    "validate_selector_kit_manifest",
    "validate_selector_submission",
    "verify_selector_kit_zip",
]
