#!/usr/bin/env python3
"""Plan or apply the reviewed historical Weekly LLM selection-scope resolution.

Dry-run is the default.  Applying requires ``--apply`` and a service-role client;
each write calls the database's transactional
``resolve_weekly_quiz_vote_selection`` RPC.  This tool never updates attempts or
app_state and never accepts a caller-selected round or model allow-list.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import decimal
import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence


SOURCE_ROUND_ID = "weekly-2026-08-08-beta-v4"
TARGET_ROUND_ID = "weekly-2026-08-08-beta-v5-global-tm-29"
ALLOWED_MODEL_LABELS = ("Claude Opus", "Codex GPT-5.6")
PROCEDURAL_EVIDENCE_PATHS = (
    "local/build_llm_vote_packets.py",
    "local/validate_llm_ballots.py",
    "local/submit_llm_ballots.py",
)
EVIDENCE_SCHEMA_VERSION = "foldarium.weekly-llm-selection-evidence/v1"
AUDIT_SCHEMA_VERSION = "foldarium.weekly-llm-selection-backfill-audit/v1"
RESOLUTION_NAMESPACE = uuid.UUID("9f69a613-e994-4f67-a219-92658048d75f")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

ATTEMPT_FINGERPRINT_FIELDS = (
    "vote_attempt_id",
    "session_id",
    "round_id",
    "user_id",
    "item_id",
    "question_index",
    "choice_id",
    "picked_none",
    "selection_kind",
    "selection_id",
    "viewer_trace",
    "app_state",
    "active_pane_id",
    "vote_comment",
    "submitted_at",
    "created_at",
)


class BackfillError(RuntimeError):
    """A fail-closed validation error safe to show in an operator console."""


@dataclasses.dataclass(frozen=True)
class EvidenceBundle:
    manifest: dict[str, Any]
    sha256: str


@dataclasses.dataclass(frozen=True)
class ResolutionPlan:
    entries: tuple[dict[str, Any], ...]
    report: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise BackfillError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BackfillError(f"{label} is required")
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise BackfillError(f"{label} is invalid or too long")
    return normalized


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise BackfillError(f"{label} must be an array")
    return value


def _row_value(row: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in row:
        raise BackfillError(f"{label} is missing required field {key}")
    return row[key]


def _postgres_jsonb_number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise BackfillError("attempt fingerprint contains a non-finite number")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BackfillError("attempt fingerprint contains a non-finite number")
        return str(decimal.Decimal(str(value)))
    raise TypeError(f"not a JSON number: {type(value)!r}")


def postgres_jsonb_text(value: Any) -> str:
    """Serialize the JSON-compatible value as PostgreSQL ``jsonb::text``.

    PostgreSQL JSONB orders object keys by UTF-8 byte length and then byte value,
    and renders a space after commas and colons.  The migration fingerprints a
    ``jsonb_build_object(...)::text`` value, so matching that representation is
    part of the optimistic guard.
    """

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool):
        return _postgres_jsonb_number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(postgres_jsonb_text(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = list(value)
        if not all(isinstance(key, str) for key in keys):
            raise BackfillError("attempt fingerprint contains a non-string object key")
        keys.sort(key=lambda key: (len(key.encode("utf-8")), key.encode("utf-8")))
        return "{" + ", ".join(
            f"{json.dumps(key, ensure_ascii=False)}: {postgres_jsonb_text(value[key])}"
            for key in keys
        ) + "}"
    raise BackfillError(f"attempt fingerprint contains unsupported {type(value).__name__}")


def vote_attempt_fingerprint(attempt: Mapping[str, Any]) -> str:
    fingerprint_object: dict[str, Any] = {}
    for field in ATTEMPT_FINGERPRINT_FIELDS:
        fingerprint_object[field] = _row_value(attempt, field, "vote attempt")
    return _sha256_bytes(postgres_jsonb_text(fingerprint_object).encode("utf-8"))


def blind_manifest_sha256(blind_manifest: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(blind_manifest).encode("utf-8"))


def build_evidence_bundle(
    repo_root: pathlib.Path,
    *,
    source_manifest_sha256: str,
    target_manifest_sha256: str,
) -> EvidenceBundle:
    root = repo_root.resolve()
    files: list[dict[str, Any]] = []
    for relative in PROCEDURAL_EVIDENCE_PATHS:
        path = root / relative
        try:
            stat = path.lstat()
        except FileNotFoundError as error:
            raise BackfillError(f"required procedural evidence is missing: {relative}") from error
        if path.is_symlink() or not path.is_file():
            raise BackfillError(f"procedural evidence must be a regular non-symlink file: {relative}")
        content = path.read_bytes()
        if len(content) != stat.st_size:
            raise BackfillError(f"procedural evidence changed while hashing: {relative}")
        files.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(content),
                "size_bytes": len(content),
            }
        )
    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "source_round_id": SOURCE_ROUND_ID,
        "target_round_id": TARGET_ROUND_ID,
        "source_blind_manifest_sha256": _require_digest(
            source_manifest_sha256, "source manifest digest"
        ),
        "target_blind_manifest_sha256": _require_digest(
            target_manifest_sha256, "target manifest digest"
        ),
        "procedural_files": files,
    }
    return EvidenceBundle(
        manifest=manifest,
        sha256=_sha256_bytes(_canonical_json(manifest).encode("utf-8")),
    )


def _index_rounds(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _require_list(snapshot.get("rounds"), "snapshot.rounds")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("round_id"), str):
            raise BackfillError("snapshot contains an invalid round row")
        if raw["round_id"] in by_id:
            raise BackfillError("snapshot contains duplicate round rows")
        by_id[raw["round_id"]] = raw
    if set(by_id) != {SOURCE_ROUND_ID, TARGET_ROUND_ID}:
        raise BackfillError("snapshot must contain exactly the fixed source and target rounds")
    return by_id[SOURCE_ROUND_ID], by_id[TARGET_ROUND_ID]


def _validate_round(round_row: Mapping[str, Any], expected_id: str) -> tuple[dict[str, Any], str]:
    if round_row.get("round_id") != expected_id:
        raise BackfillError("round identity mismatch")
    manifest = round_row.get("blind_manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), list):
        raise BackfillError(f"{expected_id} has no usable immutable blind manifest")
    if manifest.get("round_id") not in (None, expected_id):
        raise BackfillError(f"{expected_id} blind manifest round binding changed")
    stored_digest = _require_digest(
        round_row.get("blind_manifest_sha256"), f"{expected_id} blind manifest digest"
    )
    computed_digest = blind_manifest_sha256(manifest)
    if not hmac.compare_digest(stored_digest, computed_digest):
        raise BackfillError(f"{expected_id} blind manifest digest mismatch")
    item_count = round_row.get("item_count")
    if item_count is not None and item_count != len(manifest["items"]):
        raise BackfillError(f"{expected_id} item count does not match its manifest")
    return manifest, stored_digest


def _manifest_indexes(
    source_manifest: Mapping[str, Any],
    target_manifest: Mapping[str, Any],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    source_items = source_manifest["items"]
    target_items = target_manifest["items"]
    source_ids = [item.get("id") for item in source_items if isinstance(item, dict)]
    target_ids = [item.get("id") for item in target_items if isinstance(item, dict)]
    if (
        len(source_ids) != len(source_items)
        or len(target_ids) != len(target_items)
        or any(
            not isinstance(item_id, str) or not ID_RE.fullmatch(item_id)
            for item_id in source_ids + target_ids
        )
        or len(set(source_ids)) != len(source_ids)
        or len(set(target_ids)) != len(target_ids)
    ):
        raise BackfillError("source or target manifest has invalid or duplicate item IDs")
    if not set(target_ids).issubset(source_ids):
        raise BackfillError("target manifest contains an item absent from the source round")

    source_by_id = {item["id"]: item for item in source_items}
    representative_by_item: dict[str, dict[str, str]] = {}
    for item in target_items:
        item_id = item["id"]
        round_representatives: list[dict[str, str]] = []
        for round_label, round_item in (
            ("source", source_by_id[item_id]),
            ("target", item),
        ):
            choices = round_item.get("choices")
            if not isinstance(choices, list) or not choices:
                raise BackfillError(f"{round_label} manifest item {item_id} has no choices")
            seen_choices: set[str] = set()
            members_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for choice in choices:
                if not isinstance(choice, dict):
                    raise BackfillError(
                        f"{round_label} manifest item {item_id} has an invalid choice"
                    )
                choice_id = choice.get("id")
                cluster_id = choice.get("cluster_id")
                if (
                    not isinstance(choice_id, str)
                    or not ID_RE.fullmatch(choice_id)
                    or choice_id in seen_choices
                    or not isinstance(cluster_id, str)
                    or not ID_RE.fullmatch(cluster_id)
                    or not isinstance(choice.get("is_rep"), bool)
                ):
                    raise BackfillError(
                        f"{round_label} manifest item {item_id} has invalid choice provenance"
                    )
                seen_choices.add(choice_id)
                members_by_cluster[cluster_id].append(choice)
            representatives: dict[str, str] = {}
            for cluster_id, members in members_by_cluster.items():
                reps = [member for member in members if member["is_rep"] is True]
                if len(reps) != 1:
                    raise BackfillError(
                        f"{round_label} manifest item {item_id} does not have one "
                        "immutable representative per cluster"
                    )
                representatives[reps[0]["id"]] = cluster_id
            round_representatives.append(representatives)
        if round_representatives[0] != round_representatives[1]:
            raise BackfillError(
                f"source/target representative mapping changed for item {item_id}"
            )
        representative_by_item[item_id] = round_representatives[1]
    return target_ids, representative_by_item


def _validate_identity_sessions(
    sessions: Sequence[Any],
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    valid_rows: list[dict[str, Any]] = []
    for row in sessions:
        if not isinstance(row, dict):
            raise BackfillError("snapshot contains an invalid session row")
        if row.get("round_id") in (SOURCE_ROUND_ID, TARGET_ROUND_ID):
            valid_rows.append(row)

    source_users: dict[str, str] = {}
    for label in ALLOWED_MODEL_LABELS:
        matches = [
            row
            for row in valid_rows
            if row.get("round_id") == SOURCE_ROUND_ID and row.get("display_name") == label
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("user_id"), str):
            raise BackfillError(f"source identity linkage for {label} is missing or ambiguous")
        state = matches[0].get("initial_app_state")
        if (
            not isinstance(state, dict)
            or state.get("participant_type") != "llm"
            or state.get("model_label") != label
            or state.get("round_id") != SOURCE_ROUND_ID
        ):
            raise BackfillError(f"source identity provenance for {label} is invalid")
        source_users[label] = matches[0]["user_id"]
    if len(set(source_users.values())) != len(ALLOWED_MODEL_LABELS):
        raise BackfillError("allow-listed model identities unexpectedly share a user")

    target_sessions: dict[tuple[str, str], str] = {}
    for label, user_id in source_users.items():
        matches = [
            row
            for row in valid_rows
            if row.get("round_id") == TARGET_ROUND_ID and row.get("user_id") == user_id
        ]
        if len(matches) != 1 or matches[0].get("display_name") != label:
            raise BackfillError(f"target identity linkage for {label} is missing or renamed")
        if not isinstance(matches[0].get("session_id"), str):
            raise BackfillError(f"target session identity for {label} is invalid")
        state = matches[0].get("initial_app_state")
        if (
            not isinstance(state, dict)
            or state.get("participant_type") != "llm"
            or state.get("model_label") != label
            or state.get("round_id") not in (SOURCE_ROUND_ID, TARGET_ROUND_ID)
        ):
            raise BackfillError(f"target identity provenance for {label} is invalid")
        target_sessions[(user_id, label)] = matches[0]["session_id"]

    llm_rows = [
        row
        for row in valid_rows
        if row.get("round_id") in (SOURCE_ROUND_ID, TARGET_ROUND_ID)
        and (
            (isinstance(row.get("initial_app_state"), dict)
             and row["initial_app_state"].get("participant_type") == "llm")
            or row.get("display_name") in ALLOWED_MODEL_LABELS
        )
    ]
    expected_pairs = {
        (SOURCE_ROUND_ID, user_id, label)
        for label, user_id in source_users.items()
    } | {
        (TARGET_ROUND_ID, user_id, label)
        for label, user_id in source_users.items()
    }
    observed_pairs = {
        (row.get("round_id"), row.get("user_id"), row.get("display_name"))
        for row in llm_rows
    }
    if observed_pairs != expected_pairs or len(llm_rows) != 4:
        raise BackfillError("unexpected LLM name, user, or duplicate session is present")
    return source_users, target_sessions


def _validate_attempt_state(
    attempt: Mapping[str, Any],
    *,
    label: str,
    item_id: str,
    choice_id: Any,
    picked_none: bool,
) -> None:
    state = attempt.get("app_state")
    if not isinstance(state, dict):
        raise BackfillError(f"latest attempt for {label}/{item_id} has no frozen app state")
    if (
        state.get("participant_type") != "llm"
        or state.get("model_label") != label
        or state.get("item_id") != item_id
        or state.get("choice_id") != choice_id
        or state.get("picked_none") is not picked_none
    ):
        raise BackfillError(f"latest attempt provenance changed for {label}/{item_id}")


def _prepare_votes(
    snapshot: Mapping[str, Any],
    *,
    item_ids: list[str],
    representative_by_item: Mapping[str, Mapping[str, str]],
    source_users: Mapping[str, str],
    target_sessions: Mapping[tuple[str, str], str],
    evidence: EvidenceBundle,
    actor: str,
    reviewer: str,
    reason: str,
    expected_vote_count: int | None,
) -> ResolutionPlan:
    user_to_label = {user_id: label for label, user_id in source_users.items()}
    expected_count = len(item_ids) * len(ALLOWED_MODEL_LABELS)
    if expected_vote_count is not None and expected_vote_count != expected_count:
        raise BackfillError(
            "operator expected-vote-count does not match manifest items times allow-listed models"
        )

    votes = _require_list(snapshot.get("votes"), "snapshot.votes")
    target_votes = [
        vote for vote in votes
        if isinstance(vote, dict) and vote.get("round_id") == TARGET_ROUND_ID
    ]
    if len(target_votes) != expected_count:
        raise BackfillError("target unresolved vote count is incomplete or contains unexpected rows")
    if any(vote.get("user_id") not in user_to_label for vote in target_votes):
        raise BackfillError("target votes include a user outside the reviewed allow-list")

    votes_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for vote in target_votes:
        user_id = vote.get("user_id")
        item_id = vote.get("item_id")
        label = user_to_label[user_id]
        if item_id not in representative_by_item:
            raise BackfillError(f"target vote for {label} references an unexpected item")
        key = (user_id, item_id)
        if key in votes_by_key:
            raise BackfillError(f"target vote for {label}/{item_id} is duplicated")
        if (
            vote.get("selection_kind") is not None
            or vote.get("selection_id") is not None
            or vote.get("selection_source_attempt_id") is not None
            or vote.get("selection_source") is not None
            or vote.get("selection_resolution_id") is not None
        ):
            raise BackfillError(f"target vote for {label}/{item_id} is not unresolved")
        revision = vote.get("selection_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise BackfillError(f"target vote for {label}/{item_id} has an invalid revision")
        votes_by_key[key] = vote

    expected_keys = {
        (user_id, item_id)
        for user_id in user_to_label
        for item_id in item_ids
    }
    if set(votes_by_key) != expected_keys:
        raise BackfillError("target vote matrix is not complete for both reviewed identities")

    attempts = _require_list(snapshot.get("attempts"), "snapshot.attempts")
    target_attempts: list[dict[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise BackfillError("snapshot contains an invalid attempt row")
        if attempt.get("round_id") == TARGET_ROUND_ID:
            target_attempts.append(attempt)
    if any(attempt.get("user_id") not in user_to_label for attempt in target_attempts):
        raise BackfillError("target attempts include a user outside the reviewed allow-list")

    attempts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for attempt in target_attempts:
        item_id = attempt.get("item_id")
        if item_id not in representative_by_item:
            raise BackfillError("target attempts include an unexpected item")
        attempts_by_key[(attempt.get("user_id"), item_id)].append(attempt)

    entries: list[dict[str, Any]] = []
    kind_counts: Counter[str] = Counter()
    model_counts: dict[str, Counter[str]] = {
        label: Counter() for label in ALLOWED_MODEL_LABELS
    }
    item_ordinal = {item_id: index for index, item_id in enumerate(item_ids)}

    for user_id, item_id in sorted(expected_keys, key=lambda key: (user_to_label[key[0]], key[1])):
        label = user_to_label[user_id]
        vote = votes_by_key[(user_id, item_id)]
        candidates = attempts_by_key.get((user_id, item_id), [])
        if not candidates:
            raise BackfillError(f"latest attempt for {label}/{item_id} is missing")
        if any(not isinstance(attempt.get("submitted_at"), str) for attempt in candidates):
            raise BackfillError(f"attempt timestamps for {label}/{item_id} are invalid")
        latest_timestamp = max(attempt["submitted_at"] for attempt in candidates)
        latest_attempts = [
            attempt for attempt in candidates
            if attempt.get("submitted_at") == latest_timestamp
        ]
        if len(latest_attempts) != 1:
            raise BackfillError(f"matching latest attempt for {label}/{item_id} is duplicated")
        if vote.get("submitted_at") != latest_timestamp:
            raise BackfillError(f"latest vote timestamp changed for {label}/{item_id}")
        attempt = latest_attempts[0]
        if (
            attempt.get("choice_id") != vote.get("choice_id")
            or attempt.get("picked_none") is not vote.get("picked_none")
        ):
            raise BackfillError(f"latest vote choice changed for {label}/{item_id}")
        if attempt.get("session_id") != target_sessions[(user_id, label)]:
            raise BackfillError(f"latest attempt for {label}/{item_id} has an unexpected session")
        if attempt.get("question_index") != item_ordinal[item_id]:
            raise BackfillError(f"latest attempt question index changed for {label}/{item_id}")
        if attempt.get("selection_kind") is not None or attempt.get("selection_id") is not None:
            raise BackfillError(f"latest attempt for {label}/{item_id} is not historical/unresolved")
        picked_none = vote.get("picked_none")
        if not isinstance(picked_none, bool):
            raise BackfillError(f"latest vote for {label}/{item_id} has invalid picked_none")
        choice_id = vote.get("choice_id")
        if picked_none:
            if choice_id is not None:
                raise BackfillError(f"none vote for {label}/{item_id} unexpectedly has a choice")
            selection_kind = "none"
            selection_id = None
        else:
            if not isinstance(choice_id, str):
                raise BackfillError(f"cluster vote for {label}/{item_id} has no choice")
            selection_id = representative_by_item[item_id].get(choice_id)
            if selection_id is None:
                raise BackfillError(f"cluster vote for {label}/{item_id} uses a nonrepresentative")
            selection_kind = "cluster"
        _validate_attempt_state(
            attempt,
            label=label,
            item_id=item_id,
            choice_id=choice_id,
            picked_none=picked_none,
        )

        fingerprint = vote_attempt_fingerprint(attempt)
        resolution_id = str(
            uuid.uuid5(
                RESOLUTION_NAMESPACE,
                ":".join(
                    (
                        evidence.sha256,
                        str(attempt["vote_attempt_id"]),
                        str(vote["selection_revision"]),
                        selection_kind,
                        selection_id or "none",
                    )
                ),
            )
        )
        metadata = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "source_round_id": SOURCE_ROUND_ID,
            "target_round_id": TARGET_ROUND_ID,
            "source_vote_procedure": "cluster-card-representative-or-none",
            "model_label": label,
            "question_index": item_ordinal[item_id],
            "procedural_files": copy.deepcopy(evidence.manifest["procedural_files"]),
        }
        payload = {
            "p_resolution_id": resolution_id,
            "p_source_vote_attempt_id": attempt["vote_attempt_id"],
            "p_selection_kind": selection_kind,
            "p_selection_id": selection_id,
            "p_evidence_sha256": evidence.sha256,
            "p_evidence_metadata": metadata,
            "p_actor": actor,
            "p_reviewer": reviewer,
            "p_reason": reason,
            "p_expected_selection_revision": vote["selection_revision"],
            "p_expected_vote_fingerprint_sha256": fingerprint,
            "p_supersedes_resolution_id": None,
        }
        entries.append(
            {
                "model_label": label,
                "item_id": item_id,
                "selection_kind": selection_kind,
                "payload": payload,
            }
        )
        kind_counts[selection_kind] += 1
        model_counts[label][selection_kind] += 1

    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "mode": "dry-run",
        "source_round_id": SOURCE_ROUND_ID,
        "target_round_id": TARGET_ROUND_ID,
        "item_count": len(item_ids),
        "allow_list": list(ALLOWED_MODEL_LABELS),
        "expected_vote_count": expected_count,
        "unresolved_input_count": len(entries),
        "planned_resolution_count": len(entries),
        "selection_counts": {
            "cluster": kind_counts["cluster"],
            "none": kind_counts["none"],
        },
        "models": [
            {
                "model_label": label,
                "votes": sum(model_counts[label].values()),
                "cluster": model_counts[label]["cluster"],
                "none": model_counts[label]["none"],
            }
            for label in ALLOWED_MODEL_LABELS
        ],
        "evidence": {
            "sha256": evidence.sha256,
            "procedural_files": copy.deepcopy(evidence.manifest["procedural_files"]),
            "source_blind_manifest_sha256": evidence.manifest[
                "source_blind_manifest_sha256"
            ],
            "target_blind_manifest_sha256": evidence.manifest[
                "target_blind_manifest_sha256"
            ],
        },
        "ready_to_apply": len(entries) == expected_count,
        "writes_performed": 0,
    }
    return ResolutionPlan(entries=tuple(entries), report=report)


def prepare_resolution_plan(
    snapshot: Mapping[str, Any],
    *,
    repo_root: pathlib.Path,
    expected_evidence_sha256: str,
    actor: str,
    reviewer: str,
    reason: str,
    expected_vote_count: int | None = None,
) -> ResolutionPlan:
    actor = _require_text(actor, "actor", 200)
    reviewer = _require_text(reviewer, "reviewer", 200)
    reason = _require_text(reason, "reason", 4000)
    if actor == reviewer:
        raise BackfillError("actor and independent reviewer must differ")
    expected_evidence_sha256 = _require_digest(
        expected_evidence_sha256, "reviewed evidence digest"
    )

    source_round, target_round = _index_rounds(snapshot)
    source_manifest, source_digest = _validate_round(source_round, SOURCE_ROUND_ID)
    target_manifest, target_digest = _validate_round(target_round, TARGET_ROUND_ID)
    item_ids, representatives = _manifest_indexes(source_manifest, target_manifest)
    evidence = build_evidence_bundle(
        repo_root,
        source_manifest_sha256=source_digest,
        target_manifest_sha256=target_digest,
    )
    if not hmac.compare_digest(evidence.sha256, expected_evidence_sha256):
        raise BackfillError("reviewed evidence digest does not match runtime evidence")
    sessions = _require_list(snapshot.get("sessions"), "snapshot.sessions")
    source_users, target_sessions = _validate_identity_sessions(sessions)
    return _prepare_votes(
        snapshot,
        item_ids=item_ids,
        representative_by_item=representatives,
        source_users=source_users,
        target_sessions=target_sessions,
        evidence=evidence,
        actor=actor,
        reviewer=reviewer,
        reason=reason,
        expected_vote_count=expected_vote_count,
    )


def _receipt_row(response: Any) -> Mapping[str, Any]:
    if isinstance(response, list) and len(response) == 1 and isinstance(response[0], dict):
        return response[0]
    if isinstance(response, dict):
        return response
    raise BackfillError("resolution RPC returned an unexpected receipt shape")


def execute_resolution_plan(
    plan: ResolutionPlan,
    *,
    client: Any,
    apply: bool = False,
) -> dict[str, Any]:
    report = copy.deepcopy(plan.report)
    if not apply:
        return report
    if len(plan.entries) != report["expected_vote_count"] or not report["ready_to_apply"]:
        raise BackfillError("incomplete resolution plan cannot be applied")

    applied = 0
    for entry in plan.entries:
        response = client.rpc(
            "resolve_weekly_quiz_vote_selection",
            copy.deepcopy(entry["payload"]),
        )
        receipt = _receipt_row(response)
        if receipt.get("resolution_id") != entry["payload"]["p_resolution_id"]:
            raise BackfillError("resolution RPC receipt identity mismatch")
        applied += 1

    check_response = client.rpc(
        "check_weekly_quiz_selection_provenance",
        {"p_round_id": TARGET_ROUND_ID},
    )
    check = _receipt_row(check_response)
    if (
        check.get("round_id") != TARGET_ROUND_ID
        or check.get("total_votes") != report["expected_vote_count"]
        or check.get("unresolved_votes") != 0
        or check.get("inconsistent_votes") != 0
        or check.get("ready") is not True
    ):
        raise BackfillError("post-apply provenance completeness check failed")

    report["mode"] = "apply"
    report["writes_performed"] = applied
    report["post_apply"] = {
        "total_votes": check["total_votes"],
        "unresolved_votes": check["unresolved_votes"],
        "inconsistent_votes": check["inconsistent_votes"],
        "ready": True,
    }
    return report


class SupabaseServiceClient:
    """Small service-role REST client; response bodies are never included in errors."""

    def __init__(self, base_url: str, service_role_key: str) -> None:
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            raise BackfillError("Supabase URL must be an https URL")
        if not isinstance(service_role_key, str) or len(service_role_key) < 20:
            raise BackfillError("service-role key is missing or invalid")
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }

    def _request(self, path: str, *, method: str, body: Any = None) -> Any:
        payload = None
        if body is not None:
            payload = _canonical_json(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/rest/v1/{path}",
            data=payload,
            method=method,
            headers=self.headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise BackfillError(f"Supabase request failed with HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise BackfillError("Supabase request failed") from error
        if not raw:
            return None
        try:
            return json.loads(raw, parse_float=decimal.Decimal)
        except json.JSONDecodeError as error:
            raise BackfillError("Supabase returned invalid JSON") from error

    def rows(self, table: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({**params, "limit": "10000"})
        response = self._request(f"{table}?{query}", method="GET")
        if not isinstance(response, list) or len(response) >= 10000:
            raise BackfillError(f"{table} query was invalid or truncated")
        return response

    def rpc(self, name: str, payload: Mapping[str, Any]) -> Any:
        return self._request(f"rpc/{name}", method="POST", body=payload)

    def fetch_snapshot(self) -> dict[str, Any]:
        round_filter = f"in.({SOURCE_ROUND_ID},{TARGET_ROUND_ID})"
        rounds = self.rows(
            "weekly_quiz_rounds",
            {
                "select": (
                    "round_id,status,item_count,blind_manifest,"
                    "blind_manifest_sha256,metadata"
                ),
                "round_id": round_filter,
                "order": "round_id.asc",
            },
        )
        sessions = self.rows(
            "weekly_quiz_sessions",
            {
                "select": (
                    "session_id,round_id,user_id,display_name,"
                    "initial_app_state,completed_at"
                ),
                "round_id": round_filter,
                "order": "round_id.asc,display_name.asc",
            },
        )
        votes = self.rows(
            "weekly_quiz_votes",
            {
                "select": (
                    "vote_id,round_id,user_id,item_id,choice_id,picked_none,"
                    "submitted_at,selection_kind,selection_id,"
                    "selection_source_attempt_id,selection_revision,"
                    "selection_source,selection_resolution_id"
                ),
                "round_id": f"eq.{TARGET_ROUND_ID}",
                "order": "user_id.asc,item_id.asc",
            },
        )
        attempts = self.rows(
            "weekly_quiz_vote_attempts",
            {
                "select": ",".join(ATTEMPT_FINGERPRINT_FIELDS),
                "round_id": f"eq.{TARGET_ROUND_ID}",
                "order": "user_id.asc,item_id.asc,submitted_at.asc",
            },
        )
        return {
            "rounds": rounds,
            "sessions": sessions,
            "votes": votes,
            "attempts": attempts,
        }


def _load_snapshot(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_float=decimal.Decimal)
    except (OSError, json.JSONDecodeError) as error:
        raise BackfillError("offline snapshot could not be read") from error
    if not isinstance(value, dict):
        raise BackfillError("offline snapshot root must be an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="call transactional resolution RPCs; omitted means read-only dry-run",
    )
    parser.add_argument("--actor", required=True, help="reviewed resolver identity")
    parser.add_argument("--reviewer", required=True, help="independent reviewer identity")
    parser.add_argument("--reason", required=True, help="reviewed resolution reason")
    parser.add_argument(
        "--evidence-sha256",
        required=True,
        help="reviewed digest of the runtime-built evidence manifest",
    )
    parser.add_argument(
        "--expected-vote-count",
        type=int,
        help="optional operator cross-check; derived count remains authoritative",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parents[1],
        help="repository root containing the three procedural evidence files",
    )
    parser.add_argument(
        "--snapshot",
        type=pathlib.Path,
        help="offline JSON snapshot for dry-run; --apply is deliberately rejected",
    )
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL"))
    parser.add_argument(
        "--service-role-key",
        default=os.environ.get("SUPABASE_SERVICE_ROLE_KEY"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.expected_vote_count is not None and args.expected_vote_count < 1:
            raise BackfillError("expected-vote-count must be positive")
        if args.snapshot is not None:
            if args.apply:
                raise BackfillError("--apply cannot use an offline snapshot")
            snapshot = _load_snapshot(args.snapshot)
            client = None
        else:
            client = SupabaseServiceClient(args.supabase_url, args.service_role_key)
            snapshot = client.fetch_snapshot()
        plan = prepare_resolution_plan(
            snapshot,
            repo_root=args.repo_root,
            expected_evidence_sha256=args.evidence_sha256,
            actor=args.actor,
            reviewer=args.reviewer,
            reason=args.reason,
            expected_vote_count=args.expected_vote_count,
        )
        report = execute_resolution_plan(plan, client=client, apply=args.apply)
        print(_canonical_json(report))
        return 0
    except BackfillError as error:
        print(
            _canonical_json(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "ok": False,
                    "error": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
