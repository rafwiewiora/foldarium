"""Deterministic post-reveal retrospective archive publication.

The private evaluation artifact remains the scientific source of truth.  This
module adds a second, post-reveal boundary: it snapshots final ballots, current
human pseudonyms, and the reviewed automated-identity registry, then emits a
sanitized aggregate artifact and a private pseudonymous artifact.
Neither artifact contains database identities, hashes, traces, comments,
application state, credentials, or object URIs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .contracts import canonical_json, stable_id
from .private_evaluation import (
    PRIVATE_EVALUATION_FORMAT_VERSION,
    describe_private_evaluation_artifact,
)
from .quiz import manifest_sha256
from .supabase import PRIVATE_WEEKLY_EVALUATION_FIELDS

RETROSPECTIVE_PUBLICATION_FORMAT_VERSION = (
    "foldarium.weekly-retrospective-publication/v1"
)
RETROSPECTIVE_SOURCE_FORMAT_VERSION = "foldarium.weekly-retrospective-source/v1"
RETROSPECTIVE_PUBLIC_FORMAT_VERSION = "foldarium.weekly-retrospective-public/v1"
RETROSPECTIVE_ADMIN_FORMAT_VERSION = "foldarium.weekly-retrospective-admin/v1"
RETROSPECTIVE_MEDIA_TYPE = "application/json"
LEGACY_ANONYMOUS_ROUND_ID = "weekly-2026-08-08-beta-v5-global-tm-29"
LEGACY_ANONYMOUS_DISPLAY_NAME = "Anonymous"
LEGACY_EXACT_SCOPE_ROUND_ID = LEGACY_ANONYMOUS_ROUND_ID

APPROVED_AUTOMATED_IDENTITIES = frozenset(
    {
        "Claude Opus",
        "Codex GPT-5.6",
        "GPT-5.6 Sol",
    }
)
SMINA_IDENTITY = "Smina"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_FORBIDDEN_ARTIFACT_KEYS = {
    "user_id",
    "session_id",
    "participant_hash",
    "display_name_hash",
    "participant_link",
    "vote_id",
    "vote_attempt_id",
    "viewer_trace",
    "trace",
    "comment",
    "comments",
    "suggestion_text",
    "app_state",
    "initial_app_state",
    "auth",
    "object_uri",
    "execution_id",
    "execution_sha256",
    "payload",
    "payload_digest",
    "runtime_sha256",
    "config_sha256",
    "tools_sha256",
    "input_manifest_sha256",
    "prompt_sha256",
    "blindness_attestation",
    "blindness_attestation_sha256",
    "output_sha256",
    "provenance",
    "engine",
    "usage",
}


class RetrospectiveArchiveError(RuntimeError):
    """Raised when publication inputs cannot be proven safe and immutable."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RetrospectiveArchiveError(f"{field} must be non-empty text")
    return value


def _display_name(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
        raise RetrospectiveArchiveError(f"{field} is invalid")
    normalized = " ".join(value.strip().split())
    if (
        not normalized
        or len(normalized) > 80
        or len(normalized.encode("utf-8")) > 320
    ):
        raise RetrospectiveArchiveError(f"{field} is invalid")
    return normalized


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RetrospectiveArchiveError(f"{field} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RetrospectiveArchiveError(f"{field} must be a positive integer")
    return value


def _participant_link(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise RetrospectiveArchiveError(f"{field} must be a UUID")
    return value.lower()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrospectiveArchiveError(f"{field} must be an object")
    result = deepcopy(dict(value))
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RetrospectiveArchiveError(f"{field} must contain finite JSON") from exc
    return result


def _rows(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise RetrospectiveArchiveError(f"{field} must be an array of objects")
    return [deepcopy(dict(row)) for row in value]


def _timestamp_sort_key(value: Any, field: str) -> tuple[datetime, str]:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrospectiveArchiveError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise RetrospectiveArchiveError(f"{field} must include a timezone")
    return parsed, text


def _benchmark_unclustered_vote(
    raw_item: Mapping[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    item_id = _text(raw_item.get("item_id"), f"benchmark item_id[{index}]")
    unclustered = raw_item.get("unclustered")
    if not isinstance(unclustered, Mapping):
        raise RetrospectiveArchiveError("benchmark item unclustered is invalid")
    selection_kind = unclustered.get("selection_kind")
    if selection_kind == "none":
        if set(unclustered) != {"selection_kind"}:
            raise RetrospectiveArchiveError("benchmark none decision is malformed")
        return {
            "item_id": item_id,
            "choice_id": None,
            "picked_none": True,
            "selection_kind": "none",
        }
    if selection_kind == "exact":
        if set(unclustered) != {"selection_kind", "choice_id"}:
            raise RetrospectiveArchiveError("benchmark exact decision is malformed")
        choice_id = _text(unclustered.get("choice_id"), f"benchmark choice_id[{index}]")
        return {
            "item_id": item_id,
            "choice_id": choice_id,
            "picked_none": False,
            "selection_kind": "exact",
        }
    raise RetrospectiveArchiveError("benchmark unclustered decision is invalid")


def _normalize_post_close_benchmark_rows(
    round_id: str,
    benchmark_rows: list[Mapping[str, Any]],
    *,
    item_count: int | None,
    ballot_participant_links: set[str],
    active_automated_identities: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not benchmark_rows:
        return [], []
    if item_count is None or item_count < 1:
        raise RetrospectiveArchiveError(
            "post-close benchmarks require a positive item_count"
        )

    normalized_rows = _rows(benchmark_rows, "post_close_benchmarks")
    participants: list[dict[str, Any]] = []
    votes: list[dict[str, Any]] = []
    seen_automated_names: set[str] = set()

    for row_index, row in enumerate(
        sorted(
            normalized_rows,
            key=lambda candidate: (
                str(candidate.get("display_name", "")).casefold(),
                str(
                    (
                        candidate.get("payload", {})
                        if isinstance(candidate.get("payload"), Mapping)
                        else {}
                    ).get("submission_id", "")
                ).casefold(),
            ),
        )
    ):
        if row.get("run_class") != "post_close_benchmark":
            raise RetrospectiveArchiveError("post-close benchmark run_class is invalid")
        display_name = _text(row.get("display_name"), "benchmark display_name")
        if display_name not in APPROVED_AUTOMATED_IDENTITIES:
            raise RetrospectiveArchiveError(
                "post-close benchmark identity is not code-approved"
            )
        if display_name in active_automated_identities:
            raise RetrospectiveArchiveError(
                "post-close benchmark duplicates a ballot automated identity"
            )
        if display_name in seen_automated_names:
            raise RetrospectiveArchiveError(
                "post-close benchmarks contain a duplicate automated identity"
            )
        seen_automated_names.add(display_name)

        payload = _object(row.get("payload"), f"post_close_benchmarks[{row_index}].payload")
        if payload.get("round_id") != round_id:
            raise RetrospectiveArchiveError("benchmark payload round_id mismatch")
        participant_link = _participant_link(
            payload.get("submission_id"), "benchmark submission_id"
        )
        if participant_link in ballot_participant_links:
            raise RetrospectiveArchiveError(
                "post-close benchmark participant_link collides with a ballot"
            )
        ballot_participant_links.add(participant_link)

        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != item_count:
            raise RetrospectiveArchiveError(
                "post-close benchmark payload items are incomplete"
            )

        seen_items: set[str] = set()
        participant_votes: list[dict[str, Any]] = []
        for index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                raise RetrospectiveArchiveError("benchmark payload item is invalid")
            vote = _benchmark_unclustered_vote(raw_item, index=index)
            item_id = vote["item_id"]
            if item_id in seen_items:
                raise RetrospectiveArchiveError("benchmark payload item_id is duplicated")
            seen_items.add(item_id)
            participant_votes.append(
                {
                    "participant_link": participant_link,
                    **vote,
                }
            )
        if len(seen_items) != item_count:
            raise RetrospectiveArchiveError(
                "post-close benchmark payload items are incomplete"
            )

        participant_votes.sort(
            key=lambda vote: (
                vote["item_id"],
                vote["picked_none"],
                vote["choice_id"] or "",
            )
        )
        votes.extend(participant_votes)
        participants.append(
            {
                "participant_link": participant_link,
                "participant_kind": "automated",
                "automated_identity": display_name,
                "display_name": None,
                "current_session_count": 0,
            }
        )
        seen_automated_names.add(display_name)

    participants.sort(key=lambda row: row["participant_link"])
    votes.sort(
        key=lambda row: (
            row["participant_link"],
            row["item_id"],
            row["picked_none"],
            row["choice_id"] or "",
        )
    )
    return participants, votes


def build_retrospective_source_snapshot(
    round_id: str,
    *,
    votes: list[Mapping[str, Any]],
    vote_attempts: list[Mapping[str, Any]],
    current_sessions: list[Mapping[str, Any]],
    automated_identities: list[Mapping[str, Any]],
    post_close_benchmarks: list[Mapping[str, Any]] | None = None,
    item_count: int | None = None,
) -> dict[str, Any]:
    """Normalize final ballots and minimal session lineage into stable input.

    ``participant_link`` is deliberately retained only in this private source
    object.  Publication artifacts replace it with per-publication pseudonyms.
    """

    round_id = _text(round_id, "round_id")
    vote_rows = _rows(votes, "votes")
    attempt_rows = _rows(vote_attempts, "vote_attempts")
    session_rows = _rows(current_sessions, "current_sessions")
    identity_rows = _rows(automated_identities, "automated_identities")

    current_session_counts: dict[str, int] = {}
    current_names: dict[str, set[str]] = {}
    for row in session_rows:
        if row.get("round_id") != round_id:
            raise RetrospectiveArchiveError("current session round_id mismatch")
        participant = _participant_link(row.get("user_id"), "session user_id")
        display_name = _display_name(row.get("display_name"), "session display_name")
        current_session_counts[participant] = current_session_counts.get(participant, 0) + 1
        current_names.setdefault(participant, set()).add(display_name)

    automation_by_participant: dict[str, str] = {}
    for row in identity_rows:
        participant = _participant_link(
            row.get("user_id"), "automated identity user_id"
        )
        display_name = _text(
            row.get("display_name"), "automated identity display_name"
        )
        if display_name not in APPROVED_AUTOMATED_IDENTITIES:
            raise RetrospectiveArchiveError(
                "automated identity is not code-approved"
            )
        if row.get("participant_kind") != "llm":
            raise RetrospectiveArchiveError(
                "automated identity participant_kind is invalid"
            )
        previous = automation_by_participant.get(participant)
        if previous is not None:
            raise RetrospectiveArchiveError(
                "automated identity registry contains a duplicate participant"
            )
        automation_by_participant[participant] = display_name

    attempts_by_vote: dict[tuple[str, str, str | None, bool], tuple[Any, str]] = {}
    for row in attempt_rows:
        if row.get("round_id") != round_id:
            raise RetrospectiveArchiveError("vote attempt round_id mismatch")
        participant = _participant_link(row.get("user_id"), "vote attempt user_id")
        item_id = _text(row.get("item_id"), "vote attempt item_id")
        picked_none = row.get("picked_none")
        if not isinstance(picked_none, bool):
            raise RetrospectiveArchiveError("vote attempt picked_none is invalid")
        choice_id = row.get("choice_id")
        if picked_none:
            if choice_id not in (None, ""):
                raise RetrospectiveArchiveError(
                    "picked-none vote attempt contains choice_id"
                )
            choice_id = None
        else:
            choice_id = _text(choice_id, "vote attempt choice_id")
        app_state = row.get("app_state")
        selection_kind = (
            app_state.get("selection_kind")
            if isinstance(app_state, Mapping)
            else None
        )
        if selection_kind not in {"exact", "cluster"}:
            continue
        submitted = _timestamp_sort_key(
            row.get("submitted_at"), "vote attempt submitted_at"
        )
        attempt_id = _text(
            row.get("vote_attempt_id", ""), "vote attempt identity"
        )
        key = (participant, item_id, choice_id, picked_none)
        ordering = (submitted, attempt_id)
        previous = attempts_by_vote.get(key)
        if previous is None or ordering > previous[0]:
            attempts_by_vote[key] = (ordering, selection_kind)

    normalized_votes: list[dict[str, Any]] = []
    participant_links: set[str] = set()
    seen_votes: set[tuple[str, str]] = set()
    for row in vote_rows:
        if row.get("round_id") != round_id:
            raise RetrospectiveArchiveError("vote round_id mismatch")
        participant = _participant_link(row.get("user_id"), "vote user_id")
        item_id = _text(row.get("item_id"), "vote item_id")
        identity = (participant, item_id)
        if identity in seen_votes:
            raise RetrospectiveArchiveError("final votes contain a duplicate item")
        seen_votes.add(identity)
        picked_none = row.get("picked_none")
        if not isinstance(picked_none, bool):
            raise RetrospectiveArchiveError("vote picked_none is invalid")
        choice_id = row.get("choice_id")
        if picked_none:
            if choice_id not in (None, ""):
                raise RetrospectiveArchiveError("picked-none vote contains choice_id")
            choice_id = None
            selection_kind = "none"
        else:
            choice_id = _text(choice_id, "vote choice_id")
            attempt = attempts_by_vote.get(
                (participant, item_id, choice_id, picked_none)
            )
            if attempt is not None:
                selection_kind = attempt[1]
            elif round_id == LEGACY_EXACT_SCOPE_ROUND_ID:
                # This beta round presented unclustered poses to every participant.
                selection_kind = "exact"
            else:
                raise RetrospectiveArchiveError(
                    "non-empty vote is missing exact-or-cluster scope"
                )
        participant_links.add(participant)
        normalized_votes.append(
            {
                "participant_link": participant,
                "item_id": item_id,
                "choice_id": choice_id,
                "picked_none": picked_none,
                "selection_kind": selection_kind,
            }
        )

    normalized_votes.sort(
        key=lambda row: (
            row["participant_link"],
            row["item_id"],
            row["picked_none"],
            row["choice_id"] or "",
        )
    )
    ballot_participant_links = set(participant_links)
    participants = []
    active_automated_identities: set[str] = set()
    for participant in sorted(ballot_participant_links):
        automated_identity = automation_by_participant.get(participant)
        names = current_names.get(participant, set())
        human_display_name = None
        if automated_identity is None:
            if len(names) == 1:
                human_display_name = next(iter(names))
            elif not names and round_id == LEGACY_ANONYMOUS_ROUND_ID:
                human_display_name = LEGACY_ANONYMOUS_DISPLAY_NAME
            else:
                raise RetrospectiveArchiveError(
                    "human participant must have one unambiguous pseudonym"
                )
        if automated_identity is not None:
            if automated_identity in active_automated_identities:
                raise RetrospectiveArchiveError(
                    "one automated identity used multiple credentials in this round"
                )
            active_automated_identities.add(automated_identity)
        participants.append(
            {
                "participant_link": participant,
                "participant_kind": (
                    "automated" if automated_identity is not None else "human"
                ),
                "automated_identity": automated_identity,
                "display_name": human_display_name,
                "current_session_count": current_session_counts.get(participant, 0),
            }
        )

    benchmark_participants, benchmark_votes = _normalize_post_close_benchmark_rows(
        round_id,
        list(post_close_benchmarks or []),
        item_count=item_count,
        ballot_participant_links=ballot_participant_links,
        active_automated_identities=active_automated_identities,
    )
    participants.extend(benchmark_participants)
    participants.sort(key=lambda row: row["participant_link"])
    normalized_votes.extend(benchmark_votes)
    normalized_votes.sort(
        key=lambda row: (
            row["participant_link"],
            row["item_id"],
            row["picked_none"],
            row["choice_id"] or "",
        )
    )
    return {
        "format_version": RETROSPECTIVE_SOURCE_FORMAT_VERSION,
        "round_id": round_id,
        "participants": participants,
        "votes": normalized_votes,
    }


def encode_retrospective_source_snapshot(snapshot: Mapping[str, Any]) -> bytes:
    normalized = _object(snapshot, "source snapshot")
    if normalized.get("format_version") != RETROSPECTIVE_SOURCE_FORMAT_VERSION:
        raise RetrospectiveArchiveError("source snapshot format_version is invalid")
    _text(normalized.get("round_id"), "source snapshot round_id")
    _rows(normalized.get("participants"), "source snapshot participants")
    _rows(normalized.get("votes"), "source snapshot votes")
    return canonical_json(normalized).encode("utf-8")


def _revealed_round(round_record: Mapping[str, Any]) -> dict[str, Any]:
    row = _object(round_record, "weekly round")
    if row.get("environment") != "production" or row.get("status") != "revealed":
        raise RetrospectiveArchiveError(
            "retrospective publication requires a revealed production round"
        )
    reveal = row.get("reveal_manifest")
    if not isinstance(reveal, Mapping):
        raise RetrospectiveArchiveError("revealed round has no reveal manifest")
    reveal_digest = _digest(
        row.get("reveal_manifest_sha256"), "round reveal manifest sha256"
    )
    if manifest_sha256(reveal) != reveal_digest:
        raise RetrospectiveArchiveError(
            "round reveal manifest does not match its recorded digest"
        )
    if reveal.get("round_id") != row.get("round_id"):
        raise RetrospectiveArchiveError("round reveal manifest identity is inconsistent")
    _text(row.get("revealed_at"), "round revealed_at")
    return row


def _verify_evaluation(
    round_record: Mapping[str, Any],
    descriptor: Mapping[str, Any],
    content: bytes,
) -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = _object(descriptor, "private evaluation descriptor")
    if set(catalog).difference(set(PRIVATE_WEEKLY_EVALUATION_FIELDS) | {"created_at"}):
        raise RetrospectiveArchiveError("private evaluation descriptor has unknown fields")
    described = describe_private_evaluation_artifact(
        content,
        expected_artifact_sha256=_digest(
            catalog.get("artifact_sha256"), "evaluation artifact sha256"
        ),
    )
    for field in PRIVATE_WEEKLY_EVALUATION_FIELDS:
        expected = catalog.get(field)
        actual = described.get(field)
        if field in {"round_opens_at", "round_closes_at"}:
            if _timestamp_sort_key(expected, field)[0] != _timestamp_sort_key(
                actual, field
            )[0]:
                raise RetrospectiveArchiveError(
                    f"private evaluation catalog differs from artifact at {field}"
                )
        elif actual != expected:
            raise RetrospectiveArchiveError(
                f"private evaluation catalog differs from artifact at {field}"
            )
    if catalog.get("format_version") != PRIVATE_EVALUATION_FORMAT_VERSION:
        raise RetrospectiveArchiveError("retrospective requires a v5 private evaluation")
    for field in (
        "round_id",
        "campaign_id",
        "environment",
        "blind_manifest_sha256",
        "reveal_manifest_sha256",
    ):
        round_field = {
            "round_id": "round_id",
            "campaign_id": "campaign_id",
            "environment": "environment",
            "blind_manifest_sha256": "blind_manifest_sha256",
            "reveal_manifest_sha256": "reveal_manifest_sha256",
        }[field]
        if catalog.get(field) != round_record.get(round_field):
            raise RetrospectiveArchiveError(
                f"private evaluation is not bound to round field {field}"
            )
    try:
        artifact = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # already checked above
        raise RetrospectiveArchiveError("private evaluation artifact is invalid") from exc
    return catalog, artifact


def _answer_key(reveal: Mapping[str, Any], item_count: int) -> dict[str, Any]:
    raw_items = reveal.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != item_count:
        raise RetrospectiveArchiveError("reveal item_count is inconsistent")
    key: dict[str, Any] = {}
    for raw_item in raw_items:
        item = _object(raw_item, "reveal item")
        item_id = _text(item.get("id"), "reveal item id")
        if item_id in key:
            raise RetrospectiveArchiveError("reveal item IDs are duplicated")
        raw_choices = item.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices:
            raise RetrospectiveArchiveError("reveal item has no choices")
        choices: dict[str, dict[str, bool]] = {}
        for raw_choice in raw_choices:
            choice = _object(raw_choice, "reveal choice")
            choice_id = _text(choice.get("id"), "reveal choice id")
            if choice_id in choices:
                raise RetrospectiveArchiveError("reveal choice IDs are duplicated")
            choices[choice_id] = {
                "accepted_correct": choice.get("accepted_correct") is True,
                "raw_correct": choice.get("correct") is True,
            }
        key[item_id] = {
            "choices": choices,
            "choice_order": {choice_id: index for index, choice_id in enumerate(choices)},
            "has_accepted_correct": any(
                choice["accepted_correct"] for choice in choices.values()
            ),
        }
    return key


def _smina_picks(
    blind: Mapping[str, Any],
    answer_key: Mapping[str, Any],
) -> dict[str, str]:
    raw_items = blind.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != len(answer_key):
        raise RetrospectiveArchiveError("blind item_count is inconsistent")
    picks: dict[str, str] = {}
    for raw_item in raw_items:
        item = _object(raw_item, "blind item")
        item_id = _text(item.get("id"), "blind item id")
        if item_id in picks or item_id not in answer_key:
            raise RetrospectiveArchiveError("blind/reveal item identities differ")
        choices = item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RetrospectiveArchiveError("blind item has no choices")
        scored: list[tuple[float, str]] = []
        seen: set[str] = set()
        for raw_choice in choices:
            choice = _object(raw_choice, "blind choice")
            choice_id = _text(choice.get("id"), "blind choice id")
            if choice_id in seen or choice_id not in answer_key[item_id]["choices"]:
                raise RetrospectiveArchiveError("blind/reveal choice identities differ")
            seen.add(choice_id)
            score = choice.get("smina_score")
            expected = {
                "metric": "smina_affinity",
                "protocol": "score_only",
                "scoring_function": "vina",
                "units": "kcal/mol",
            }
            if not isinstance(score, Mapping) or set(score) != set(expected) | {"value"}:
                raise RetrospectiveArchiveError("blind choice smina score schema is invalid")
            if any(score.get(field) != value for field, value in expected.items()):
                raise RetrospectiveArchiveError("blind choice smina score schema is invalid")
            value = score.get("value")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise RetrospectiveArchiveError("blind choice smina score is invalid")
            scored.append((float(value), choice_id))
        if seen != set(answer_key[item_id]["choices"]):
            raise RetrospectiveArchiveError("blind/reveal choice identities differ")
        picks[item_id] = min(scored)[1]
    if set(picks) != set(answer_key):
        raise RetrospectiveArchiveError("blind/reveal item identities differ")
    return picks


def _percent(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return math.floor((numerator / denominator) * 1000.0 + 0.5) / 10.0


def _result_row(identity: str, kind: str, correct: int, answered: int, total: int) -> dict:
    return {
        "participant": identity,
        "participant_kind": kind,
        "correct": correct,
        "answered": answered,
        "total": total,
        "accuracy": _percent(correct, answered),
        "coverage": _percent(answered, total),
        "complete": answered == total,
    }


def _assert_sanitized_artifact(value: Any) -> None:
    def inspect(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                normalized = str(key).lower()
                if normalized in _FORBIDDEN_ARTIFACT_KEYS:
                    raise RetrospectiveArchiveError(
                        f"archive artifact contains forbidden field {key}"
                    )
                inspect(child)
        elif isinstance(node, list):
            for child in node:
                inspect(child)
        elif isinstance(node, str):
            parsed = urlsplit(node)
            if parsed.scheme or "://" in node:
                raise RetrospectiveArchiveError("archive artifact contains a URI")

    inspect(value)


def build_retrospective_artifacts(
    round_record: Mapping[str, Any],
    evaluation_descriptor: Mapping[str, Any],
    evaluation_content: bytes,
    source_snapshot: Mapping[str, Any],
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Build sanitized public and private-admin archive bytes."""

    round_row = _revealed_round(round_record)
    evaluation, evaluation_artifact = _verify_evaluation(
        round_row, evaluation_descriptor, evaluation_content
    )
    round_id = _text(round_row.get("round_id"), "round_id")
    if source_snapshot.get("round_id") != round_id:
        raise RetrospectiveArchiveError("source snapshot belongs to another round")
    source_bytes = encode_retrospective_source_snapshot(source_snapshot)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    item_count = _positive_int(evaluation.get("item_count"), "item_count")
    choice_count = _positive_int(evaluation.get("choice_count"), "choice_count")
    blind = _object(evaluation_artifact.get("blind_manifest"), "blind manifest")
    reveal = _object(evaluation_artifact.get("reveal_manifest"), "reveal manifest")
    answer_key = _answer_key(reveal, item_count)
    if sum(len(item["choices"]) for item in answer_key.values()) != choice_count:
        raise RetrospectiveArchiveError("reveal choice_count is inconsistent")
    smina_picks = _smina_picks(blind, answer_key)

    raw_participants = _rows(source_snapshot.get("participants"), "participants")
    participant_by_link: dict[str, dict[str, Any]] = {}
    human_links: list[str] = []
    automated_links: list[str] = []
    for participant in raw_participants:
        link = _participant_link(
            participant.get("participant_link"), "participant link"
        )
        if link in participant_by_link:
            raise RetrospectiveArchiveError("source participants are duplicated")
        kind = participant.get("participant_kind")
        automated_identity = participant.get("automated_identity")
        if kind == "human":
            if automated_identity is not None:
                raise RetrospectiveArchiveError("human participant has automation identity")
            participant["display_name"] = _display_name(
                participant.get("display_name"), "human participant display_name"
            )
            human_links.append(link)
        elif kind == "automated":
            if automated_identity not in APPROVED_AUTOMATED_IDENTITIES:
                raise RetrospectiveArchiveError(
                    "source contains an unapproved automated identity"
                )
            automated_links.append(link)
        else:
            raise RetrospectiveArchiveError("participant kind is invalid")
        participant_by_link[link] = participant

    labels = {
        link: participant_by_link[link]["display_name"] for link in human_links
    }
    for link in automated_links:
        labels[link] = participant_by_link[link]["automated_identity"]

    votes_by_participant: dict[str, list[dict[str, Any]]] = {
        link: [] for link in participant_by_link
    }
    for vote in _rows(source_snapshot.get("votes"), "source votes"):
        link = _participant_link(vote.get("participant_link"), "vote participant link")
        if link not in participant_by_link:
            raise RetrospectiveArchiveError("vote references an unknown participant")
        votes_by_participant[link].append(vote)

    participant_rows: list[dict[str, Any]] = []
    response_rows: dict[str, list[dict[str, Any]]] = {
        item_id: [] for item_id in answer_key
    }
    for link in sorted(participant_by_link):
        votes = votes_by_participant[link]
        participant_kind = (
            "human"
            if participant_by_link[link]["participant_kind"] == "human"
            else "llm"
        )
        seen_items: set[str] = set()
        correct_count = 0
        for vote in votes:
            item_id = _text(vote.get("item_id"), "vote item_id")
            if item_id in seen_items or item_id not in answer_key:
                raise RetrospectiveArchiveError(
                    "participant votes contain duplicate or unknown items"
                )
            seen_items.add(item_id)
            picked_none = vote.get("picked_none")
            if not isinstance(picked_none, bool):
                raise RetrospectiveArchiveError("vote picked_none is invalid")
            selection_kind = vote.get("selection_kind")
            if selection_kind not in {"none", "exact", "cluster"}:
                raise RetrospectiveArchiveError("vote selection_kind is invalid")
            if picked_none:
                if vote.get("choice_id") is not None or selection_kind != "none":
                    raise RetrospectiveArchiveError("picked-none vote is inconsistent")
                choice_id = None
                correct = not answer_key[item_id]["has_accepted_correct"]
            else:
                choice_id = _text(vote.get("choice_id"), "vote choice_id")
                choice = answer_key[item_id]["choices"].get(choice_id)
                if choice is None:
                    raise RetrospectiveArchiveError("vote references an unknown choice")
                correct = choice["accepted_correct"]
            if correct:
                correct_count += 1
            response_rows[item_id].append(
                {
                    "participant": labels[link],
                    "participant_kind": participant_kind,
                    "choice_id": choice_id,
                    "picked_none": picked_none,
                    "selection_kind": selection_kind,
                    "correct": correct,
                }
            )
        participant_rows.append(
            _result_row(
                labels[link],
                participant_kind,
                correct_count,
                len(seen_items),
                item_count,
            )
        )

    smina_correct = 0
    for item_id, choice_id in smina_picks.items():
        correct = answer_key[item_id]["choices"][choice_id]["raw_correct"]
        if correct:
            smina_correct += 1
        response_rows[item_id].append(
            {
                "participant": SMINA_IDENTITY,
                "participant_kind": "baseline",
                "choice_id": choice_id,
                "picked_none": False,
                "selection_kind": "exact",
                "correct": correct,
            }
        )
    participant_rows.append(
        _result_row(
            SMINA_IDENTITY,
            "baseline",
            smina_correct,
            item_count,
            item_count,
        )
    )

    participant_rows.sort(
        key=lambda row: (
            {"llm": 0, "baseline": 1, "human": 2}[row["participant_kind"]],
            row["participant"],
        )
    )
    automated_results = [
        deepcopy(row)
        for row in participant_rows
        if row["participant_kind"] != "human"
    ]
    human_results = [
        row for row in participant_rows if row["participant_kind"] == "human"
    ]
    distribution_counts: dict[tuple[int, int], int] = {}
    for row in human_results:
        key = (row["correct"], row["answered"])
        distribution_counts[key] = distribution_counts.get(key, 0) + 1

    public_questions: list[dict[str, Any]] = []
    admin_questions: list[dict[str, Any]] = []
    for item_id in answer_key:
        responses = response_rows[item_id]
        human_responses = [
            row for row in responses if row["participant_kind"] == "human"
        ]
        automated_responses = [
            deepcopy(row)
            for row in responses
            if row["participant_kind"] != "human"
        ]
        aggregate: dict[tuple[str | None, bool, str, bool], int] = {}
        for response in human_responses:
            key = (
                response["choice_id"],
                response["picked_none"],
                response["selection_kind"],
                response["correct"],
            )
            aggregate[key] = aggregate.get(key, 0) + 1
        human_answers = [
            {
                "choice_id": key[0],
                "picked_none": key[1],
                "selection_kind": key[2],
                "correct": key[3],
                "vote_count": count,
            }
            for key, count in aggregate.items()
        ]
        human_answers.sort(
            key=lambda row: (
                -row["vote_count"],
                item_count + 1
                if row["picked_none"]
                else answer_key[item_id]["choice_order"][row["choice_id"]],
                row["selection_kind"],
            )
        )
        automated_responses.sort(key=lambda row: row["participant"])
        responses.sort(key=lambda row: (row["participant_kind"], row["participant"]))
        public_questions.append(
            {
                "item_id": item_id,
                "human_aggregate": {
                    "answered_count": len(human_responses),
                    "suppressed": False,
                    "correct_count": sum(
                        1 for row in human_responses if row["correct"]
                    ),
                    "answers": human_answers,
                },
                "automated_entries": automated_responses,
            }
        )
        admin_questions.append({"item_id": item_id, "responses": responses})

    round_block = {
        "round_id": round_id,
        "campaign_id": _text(round_row.get("campaign_id"), "campaign_id"),
        "opens_at": _text(round_row.get("opens_at"), "opens_at"),
        "closes_at": _text(round_row.get("closes_at"), "closes_at"),
        "revealed_at": _text(round_row.get("revealed_at"), "revealed_at"),
        "item_count": item_count,
        "choice_count": choice_count,
    }
    public_artifact = {
        "format_version": RETROSPECTIVE_PUBLIC_FORMAT_VERSION,
        "round": round_block,
        "human_aggregate": {
            "participant_count": len(human_results),
            "suppressed": False,
            "complete_count": sum(1 for row in human_results if row["complete"]),
            "partial_count": sum(
                1
                for row in human_results
                if 0 < row["answered"] < item_count
            ),
            "score_distribution": [
                {
                    "correct": correct,
                    "answered": answered,
                    "participant_count": count,
                }
                for (correct, answered), count in sorted(
                    distribution_counts.items(),
                    key=lambda item: (-item[0][0], -item[0][1]),
                )
            ],
        },
        "automated_entries": automated_results,
        "questions": public_questions,
    }
    admin_artifact = {
        "format_version": RETROSPECTIVE_ADMIN_FORMAT_VERSION,
        "round": round_block,
        "participants": participant_rows,
        "questions": admin_questions,
    }
    _assert_sanitized_artifact(public_artifact)
    _assert_sanitized_artifact(admin_artifact)
    public_bytes = canonical_json(public_artifact).encode("utf-8")
    admin_bytes = canonical_json(admin_artifact).encode("utf-8")
    return public_bytes, admin_bytes, {
        "source_snapshot_sha256": source_sha256,
        "source_snapshot_size_bytes": len(source_bytes),
        "item_count": item_count,
        "choice_count": choice_count,
    }


def _stored_descriptor(
    stored: Mapping[str, Any],
    content: bytes,
    *,
    label: str,
    storage_bucket: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    expected = {
        "sha256": digest,
        "size_bytes": len(content),
        "media_type": RETROSPECTIVE_MEDIA_TYPE,
    }
    for field, value in expected.items():
        if stored.get(field) != value:
            raise RetrospectiveArchiveError(f"stored {label} {field} is inconsistent")
    object_uri = _text(stored.get("object_uri"), f"stored {label} object_uri")
    parsed = urlsplit(object_uri)
    if (
        parsed.scheme != "supabase"
        or parsed.netloc != storage_bucket
        or parsed.path != f"/sha256/{digest[:2]}/{digest}"
        or parsed.query
        or parsed.fragment
    ):
        raise RetrospectiveArchiveError(
            f"stored {label} URI is not content-addressed"
        )
    return {"object_uri": object_uri, **expected}


def _publication_descriptor(
    round_record: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    source: Mapping[str, Any],
    public: Mapping[str, Any],
    admin: Mapping[str, Any],
) -> dict[str, Any]:
    identity = {
        "format_version": RETROSPECTIVE_PUBLICATION_FORMAT_VERSION,
        "round_id": round_record["round_id"],
        "evaluation_id": evaluation["evaluation_id"],
        "evaluation_artifact_sha256": evaluation["artifact_sha256"],
        "source_snapshot_sha256": source["sha256"],
        "public_artifact_sha256": public["sha256"],
        "admin_artifact_sha256": admin["sha256"],
    }
    return {
        "publication_id": stable_id("weekly_archive", identity, length=32),
        "round_id": round_record["round_id"],
        "campaign_id": round_record["campaign_id"],
        "environment": "production",
        "format_version": RETROSPECTIVE_PUBLICATION_FORMAT_VERSION,
        "evaluation_id": evaluation["evaluation_id"],
        "evaluation_format_version": evaluation["format_version"],
        "round_opens_at": round_record["opens_at"],
        "round_closes_at": round_record["closes_at"],
        "round_revealed_at": round_record["revealed_at"],
        "blind_manifest_sha256": evaluation["blind_manifest_sha256"],
        "private_index_sha256": evaluation["private_index_sha256"],
        "reveal_manifest_sha256": evaluation["reveal_manifest_sha256"],
        "reference_set_sha256": evaluation["reference_set_sha256"],
        "prediction_set_sha256": evaluation["prediction_set_sha256"],
        "evaluation_artifact_sha256": evaluation["artifact_sha256"],
        "item_count": evaluation["item_count"],
        "choice_count": evaluation["choice_count"],
        "source_snapshot_object_uri": source["object_uri"],
        "source_snapshot_sha256": source["sha256"],
        "source_snapshot_size_bytes": source["size_bytes"],
        "source_snapshot_media_type": source["media_type"],
        "public_artifact_object_uri": public["object_uri"],
        "public_artifact_sha256": public["sha256"],
        "public_artifact_size_bytes": public["size_bytes"],
        "public_artifact_media_type": public["media_type"],
        "admin_artifact_object_uri": admin["object_uri"],
        "admin_artifact_sha256": admin["sha256"],
        "admin_artifact_size_bytes": admin["size_bytes"],
        "admin_artifact_media_type": admin["media_type"],
    }


def _verify_catalog_row(
    row: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    result = _object(row, "retrospective publication catalog row")
    for field, value in expected.items():
        observed = result.get(field)
        if field in {
            "round_opens_at",
            "round_closes_at",
            "round_revealed_at",
        }:
            if _timestamp_sort_key(observed, field)[0] != _timestamp_sort_key(
                value, field
            )[0]:
                raise RetrospectiveArchiveError(
                    f"retrospective publication source drift at {field}"
                )
        elif observed != value:
            raise RetrospectiveArchiveError(
                f"retrospective publication source drift at {field}"
            )
    return result


def materialize_retrospective_publication(
    round_id: str,
    *,
    coordinator: Any,
) -> dict[str, Any]:
    """Idempotently publish one exact revealed production round."""

    round_id = _text(round_id, "round_id")
    coordinator.require_private_bucket()
    round_record = _revealed_round(coordinator.weekly_quiz_round(round_id))
    if round_record.get("round_id") != round_id:
        raise RetrospectiveArchiveError("coordinator returned a different round")
    evaluation = coordinator.private_weekly_evaluation(round_id)
    if not isinstance(evaluation, Mapping):
        raise RetrospectiveArchiveError(
            "revealed round has no immutable v5 private evaluation"
        )
    evaluation_content = coordinator.download_content_object(
        evaluation.get("artifact_object_uri"),
        expected_sha256=evaluation.get("artifact_sha256"),
    )
    source_rows = coordinator.weekly_retrospective_source_rows(
        round_id,
        environment=_text(round_record.get("environment"), "round environment"),
    )
    source_snapshot = build_retrospective_source_snapshot(
        round_id,
        votes=source_rows["votes"],
        vote_attempts=source_rows["vote_attempts"],
        current_sessions=source_rows["current_sessions"],
        automated_identities=source_rows["automated_identities"],
        post_close_benchmarks=source_rows.get("post_close_benchmarks"),
        item_count=_positive_int(evaluation.get("item_count"), "item_count"),
    )
    source_content = encode_retrospective_source_snapshot(source_snapshot)
    public_content, admin_content, _summary = build_retrospective_artifacts(
        round_record,
        evaluation,
        evaluation_content,
        source_snapshot,
    )

    calculated = {
        "source": {
            "sha256": hashlib.sha256(source_content).hexdigest(),
            "size_bytes": len(source_content),
            "media_type": RETROSPECTIVE_MEDIA_TYPE,
        },
        "public": {
            "sha256": hashlib.sha256(public_content).hexdigest(),
            "size_bytes": len(public_content),
            "media_type": RETROSPECTIVE_MEDIA_TYPE,
        },
        "admin": {
            "sha256": hashlib.sha256(admin_content).hexdigest(),
            "size_bytes": len(admin_content),
            "media_type": RETROSPECTIVE_MEDIA_TYPE,
        },
    }
    existing = coordinator.weekly_retrospective_publication(round_id)
    if existing is not None:
        expected = _publication_descriptor(
            round_record,
            evaluation,
            {
                **calculated["source"],
                "object_uri": existing.get("source_snapshot_object_uri"),
            },
            {
                **calculated["public"],
                "object_uri": existing.get("public_artifact_object_uri"),
            },
            {
                **calculated["admin"],
                "object_uri": existing.get("admin_artifact_object_uri"),
            },
        )
        row = _verify_catalog_row(existing, expected)
        # Re-enter the validating RPC even for an existing identity. Its source
        # table locks close the read/register race and turn this into a
        # transactionally verified no-op rather than a stale catalog shortcut.
        registered = coordinator.register_weekly_retrospective_publication(
            expected,
            source_snapshot_canonical=source_content.decode("utf-8"),
        )
        row = _verify_catalog_row(registered, expected)
        for label, content, uri_field, digest_field in (
            (
                "source snapshot",
                source_content,
                "source_snapshot_object_uri",
                "source_snapshot_sha256",
            ),
            (
                "public artifact",
                public_content,
                "public_artifact_object_uri",
                "public_artifact_sha256",
            ),
            (
                "admin artifact",
                admin_content,
                "admin_artifact_object_uri",
                "admin_artifact_sha256",
            ),
        ):
            stored_content = coordinator.download_content_object(
                row[uri_field],
                expected_sha256=row[digest_field],
            )
            if stored_content != content:
                raise RetrospectiveArchiveError(
                    f"stored retrospective {label} differs from deterministic bytes"
                )
        return {
            "status": "already-published",
            "round_id": round_id,
            "publication_id": row["publication_id"],
            "item_count": row["item_count"],
            "choice_count": row["choice_count"],
            "source_snapshot_sha256": row["source_snapshot_sha256"],
            "public_artifact": {
                "object_uri": row["public_artifact_object_uri"],
                "sha256": row["public_artifact_sha256"],
                "size_bytes": row["public_artifact_size_bytes"],
                "media_type": row["public_artifact_media_type"],
            },
            "admin_artifact": {
                "object_uri": row["admin_artifact_object_uri"],
                "sha256": row["admin_artifact_sha256"],
                "size_bytes": row["admin_artifact_size_bytes"],
                "media_type": row["admin_artifact_media_type"],
                "private": True,
            },
        }

    source_stored = _stored_descriptor(
        coordinator.store_bytes(source_content, RETROSPECTIVE_MEDIA_TYPE),
        source_content,
        label="source snapshot",
        storage_bucket=coordinator.storage_bucket,
    )
    public_stored = _stored_descriptor(
        coordinator.store_bytes(public_content, RETROSPECTIVE_MEDIA_TYPE),
        public_content,
        label="public artifact",
        storage_bucket=coordinator.storage_bucket,
    )
    admin_stored = _stored_descriptor(
        coordinator.store_bytes(admin_content, RETROSPECTIVE_MEDIA_TYPE),
        admin_content,
        label="admin artifact",
        storage_bucket=coordinator.storage_bucket,
    )
    descriptor = _publication_descriptor(
        round_record,
        evaluation,
        source_stored,
        public_stored,
        admin_stored,
    )
    registered = coordinator.register_weekly_retrospective_publication(
        descriptor,
        source_snapshot_canonical=source_content.decode("utf-8"),
    )
    row = _verify_catalog_row(registered, descriptor)
    return {
        "status": "published",
        "round_id": round_id,
        "publication_id": row["publication_id"],
        "item_count": row["item_count"],
        "choice_count": row["choice_count"],
        "source_snapshot_sha256": row["source_snapshot_sha256"],
        "public_artifact": {**public_stored},
        "admin_artifact": {**admin_stored, "private": True},
    }


def publish_missing_retrospectives(
    *,
    coordinator: Any,
    round_id: str | None = None,
) -> dict[str, Any]:
    """Publish an exact round or every revealed production round still missing."""

    round_ids = (
        [_text(round_id, "round_id")]
        if round_id is not None
        else coordinator.missing_weekly_retrospective_round_ids()
    )
    results = [
        materialize_retrospective_publication(value, coordinator=coordinator)
        for value in round_ids
    ]
    return {
        "status": "no-work" if not results else "complete",
        "requested_round_id": round_id,
        "round_count": len(results),
        "round_ids": round_ids,
        "results": results,
    }


__all__ = [
    "APPROVED_AUTOMATED_IDENTITIES",
    "RETROSPECTIVE_ADMIN_FORMAT_VERSION",
    "RETROSPECTIVE_MEDIA_TYPE",
    "RETROSPECTIVE_PUBLICATION_FORMAT_VERSION",
    "RETROSPECTIVE_PUBLIC_FORMAT_VERSION",
    "RETROSPECTIVE_SOURCE_FORMAT_VERSION",
    "RetrospectiveArchiveError",
    "build_retrospective_artifacts",
    "build_retrospective_source_snapshot",
    "encode_retrospective_source_snapshot",
    "materialize_retrospective_publication",
    "publish_missing_retrospectives",
]
