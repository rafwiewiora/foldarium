"""Blind/reveal manifest contracts for the Saturday-to-Wednesday quiz."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Iterable, Mapping

from .contracts import canonical_json, stable_id

QUIZ_SCHEMA_VERSION = 1
REVEAL_ONLY_FIELDS = frozenset(
    {
        "correct",
        "rmsd",
        "answer",
        "answer_metadata",
        "score",
        "reference",
        "reference_uri",
        "method",
        "method_version",
        "run_id",
    }
)
BLIND_CHOICE_FIELDS = frozenset(
    {"id", "pose_uri", "protein_uri", "pocket_uri", "display_label", "media_type"}
)


class QuizManifestError(ValueError):
    """Raised when quiz publication would leak answers or break identity."""


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QuizManifestError(f"{field} must be an object")
    return deepcopy(dict(value))


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuizManifestError(f"{field} must be a non-empty string")
    return value.strip()


def _assert_no_reveal_fields(value: Any, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        leaked = REVEAL_ONLY_FIELDS.intersection(value)
        if leaked:
            raise QuizManifestError(f"{path} contains reveal-only fields: {sorted(leaked)}")
        for key, child in value.items():
            _assert_no_reveal_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_reveal_fields(child, f"{path}[{index}]")


def build_blind_manifest(
    round_id: str,
    items: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(blind, private_index)`` from completed but unscored predictions.

    Choice IDs are content-derived and method-neutral.  The private index retains
    method/run/sample identities for Wednesday evaluation; it must be stored in a
    private content-addressed object, never in the public manifest.
    """

    round_id = _nonempty(round_id, "round_id")
    blind_items: list[dict[str, Any]] = []
    private_items: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for item_index, raw_item in enumerate(items):
        item = _object(raw_item, f"items[{item_index}]")
        item_id = _nonempty(item.get("id"), f"items[{item_index}].id")
        if item_id in seen_items:
            raise QuizManifestError(f"duplicate item id: {item_id}")
        seen_items.add(item_id)
        raw_choices = item.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices:
            raise QuizManifestError(f"items[{item_index}].choices must be non-empty")

        public_choices: list[dict[str, Any]] = []
        private_choices: list[dict[str, Any]] = []
        for choice_index, raw_choice in enumerate(raw_choices):
            choice = _object(raw_choice, f"items[{item_index}].choices[{choice_index}]")
            identity = {
                "round_id": round_id,
                "item_id": item_id,
                "run_id": _nonempty(choice.get("run_id"), "choice.run_id"),
                "sample_id": _nonempty(choice.get("sample_id"), "choice.sample_id"),
            }
            choice_id = stable_id("choice", identity, length=16)
            public_choice = {"id": choice_id}
            for key in BLIND_CHOICE_FIELDS - {"id"}:
                if key in choice:
                    public_choice[key] = deepcopy(choice[key])
            if "pose_uri" not in public_choice:
                raise QuizManifestError("every blind choice requires pose_uri")
            private_choice = deepcopy(choice)
            private_choice["id"] = choice_id
            public_choices.append(public_choice)
            private_choices.append(private_choice)

        # Deterministic pseudorandom order prevents method grouping without
        # making a replay produce different vote identifiers.
        order = sorted(
            range(len(public_choices)),
            key=lambda index: hashlib.sha256(
                f"{round_id}:{item_id}:{public_choices[index]['id']}".encode("utf-8")
            ).hexdigest(),
        )
        public_choices = [public_choices[index] for index in order]
        blind_item = {
            "id": item_id,
            "ligand": item.get("ligand"),
            "week": item.get("week"),
            "choices": public_choices,
        }
        for key in ("protein_uri", "pocket_uri", "metadata"):
            if key in item:
                blind_item[key] = deepcopy(item[key])
        blind_items.append(blind_item)
        private_items.append(
            {
                "id": item_id,
                "target_id": item.get("target_id", item_id),
                "ligand": item.get("ligand"),
                "choices": private_choices,
            }
        )

    blind_items.sort(key=lambda item: item["id"])
    private_items.sort(key=lambda item: item["id"])
    if not blind_items:
        raise QuizManifestError("a blind manifest must contain at least one item")
    blind = {"schema_version": QUIZ_SCHEMA_VERSION, "round_id": round_id, "items": blind_items}
    _assert_no_reveal_fields(blind)
    private = {
        "schema_version": QUIZ_SCHEMA_VERSION,
        "round_id": round_id,
        "items": private_items,
        "blind_manifest_sha256": manifest_sha256(blind),
    }
    return blind, private


def build_reveal_manifest(
    blind_manifest: Mapping[str, Any],
    scored_items: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate scored choices against the blind IDs and build Wednesday reveal."""

    blind = _object(blind_manifest, "blind_manifest")
    if blind.get("schema_version") != QUIZ_SCHEMA_VERSION or not isinstance(
        blind.get("items"), list
    ):
        raise QuizManifestError("invalid blind manifest")
    expected = {
        str(item.get("id")): {str(choice.get("id")) for choice in item.get("choices", [])}
        for item in blind["items"]
        if isinstance(item, Mapping)
    }
    reveal_items: list[dict[str, Any]] = []
    for raw in scored_items:
        item = _object(raw, "scored item")
        item_id = _nonempty(item.get("id"), "scored item.id")
        choices = item.get("choices")
        if item_id not in expected or not isinstance(choices, list):
            raise QuizManifestError(f"unexpected scored item: {item_id}")
        seen: set[str] = set()
        normalized_choices: list[dict[str, Any]] = []
        for raw_choice in choices:
            choice = _object(raw_choice, "scored choice")
            choice_id = _nonempty(choice.get("id"), "scored choice.id")
            if choice_id not in expected[item_id] or choice_id in seen:
                raise QuizManifestError(f"unexpected or duplicate scored choice: {choice_id}")
            rmsd = choice.get("rmsd")
            correct = choice.get("correct")
            if isinstance(rmsd, bool) or not isinstance(rmsd, (int, float)) or rmsd < 0:
                raise QuizManifestError("scored choice.rmsd must be a non-negative number")
            if not isinstance(correct, bool):
                raise QuizManifestError("scored choice.correct must be boolean")
            normalized = deepcopy(choice)
            normalized["id"] = choice_id
            normalized["rmsd"] = float(rmsd)
            seen.add(choice_id)
            normalized_choices.append(normalized)
        if seen != expected[item_id]:
            raise QuizManifestError(f"scored choices are incomplete for {item_id}")
        reveal_items.append({"id": item_id, "choices": normalized_choices})
    if {item["id"] for item in reveal_items} != set(expected):
        raise QuizManifestError("scored item IDs do not match the blind manifest")
    reveal_items.sort(key=lambda item: item["id"])
    return {
        "schema_version": QUIZ_SCHEMA_VERSION,
        "round_id": blind.get("round_id"),
        "blind_manifest_sha256": manifest_sha256(blind),
        "items": reveal_items,
    }


__all__ = [
    "BLIND_CHOICE_FIELDS",
    "QUIZ_SCHEMA_VERSION",
    "QuizManifestError",
    "REVEAL_ONLY_FIELDS",
    "build_blind_manifest",
    "build_reveal_manifest",
    "manifest_sha256",
]
