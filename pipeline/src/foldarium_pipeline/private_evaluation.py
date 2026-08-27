"""Private, exact-round materialization of released-coordinate quiz scores.

This module is intentionally narrower than Wednesday reveal. It supports the
explicitly allow-listed pre-close catch-up path and an idempotent post-close path
for every production round. Both invoke the same scientific evaluator, write a
deterministic artifact to a verified private bucket, and optionally catalog its
integrity descriptor. It has no publisher callback and no operation that
updates ``weekly_quiz_rounds``.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .contracts import canonical_json, stable_id
from .evaluation import evaluate_ligand_pose
from .quiz import manifest_sha256
from .weekly_quiz import (
    _selected_ligand,
)
from .selection import HEAVY_ATOM_MINIMUM, SELECTION_POLICY_VERSION, ligand_rejection_reason
from .wednesday_reveal import (
    ACCEPTANCE_POLICY_VERSION,
    CORRECT_RMSD_ANGSTROM,
    REVEAL_POLICY_VERSION,
    CoordinateResolver,
    PoseEvaluator,
    fetch_rcsb_released_reference,
    rcsb_reference_url,
    run_private_preclose_evaluation,
)

PRIVATE_EVALUATION_FORMAT_VERSION = "foldarium.weekly-private-evaluation/v5"
PRIVATE_EVALUATION_MEDIA_TYPE = "application/json"
PRODUCTION_BETA_CATCHUP_ROUND_ID = "weekly-2026-08-08-beta-v5-global-tm-29"
ALLOWED_PRECLOSE_EVALUATION_ROUND_IDS = frozenset(
    {PRODUCTION_BETA_CATCHUP_ROUND_ID}
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PrivateEvaluationError(RuntimeError):
    """Raised when a private materialization cannot prove its safety bindings."""


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PrivateEvaluationError(f"{field} must be a lowercase SHA-256")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PrivateEvaluationError(f"{field} must be non-empty text")
    return value


def _private_index_descriptor(round_record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = round_record.get("metadata")
    descriptor = metadata.get("private_index") if isinstance(metadata, Mapping) else None
    if not isinstance(descriptor, Mapping):
        raise PrivateEvaluationError("round metadata has no private-index descriptor")
    result = deepcopy(dict(descriptor))
    digest = _digest(result.get("sha256"), "private-index sha256")
    object_uri = _text(result.get("object_uri"), "private-index object_uri")
    parsed = urlsplit(object_uri)
    if (
        parsed.scheme != "supabase"
        or not parsed.netloc
        or parsed.path != f"/sha256/{digest[:2]}/{digest}"
        or parsed.query
        or parsed.fragment
    ):
        raise PrivateEvaluationError(
            "private-index object_uri is not bound to its content digest"
        )
    if result.get("media_type") != PRIVATE_EVALUATION_MEDIA_TYPE:
        raise PrivateEvaluationError("private index must be application/json")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PrivateEvaluationError(f"{field} must be a positive integer")
    return value


def _content_addressed_supabase_uri(bucket: str, digest: str) -> str:
    bucket = _text(bucket, "storage bucket")
    digest = _digest(digest, "content digest")
    return f"supabase://{bucket}/sha256/{digest[:2]}/{digest}"


def _manifest_canonical_digest(canonical_json_text: str) -> str:
    return hashlib.sha256(canonical_json_text.encode("utf-8")).hexdigest()


def _artifact_manifest_canonical_json(
    raw_canonical: Any,
    parsed: Any,
    field: str,
) -> str:
    if not isinstance(raw_canonical, str) or not raw_canonical:
        raise PrivateEvaluationError(f"artifact {field} canonical JSON is missing")
    if not isinstance(parsed, Mapping):
        raise PrivateEvaluationError(f"artifact {field} object is missing")
    if canonical_json(parsed) != raw_canonical:
        raise PrivateEvaluationError(f"artifact {field} canonical JSON is inconsistent")
    return raw_canonical


def _artifact_private_index_descriptor(private_index: Any) -> dict[str, Any]:
    if not isinstance(private_index, Mapping):
        raise PrivateEvaluationError("artifact round has no private-index descriptor")
    result = deepcopy(dict(private_index))
    digest = _digest(result.get("sha256"), "artifact private-index sha256")
    object_uri = _text(result.get("object_uri"), "artifact private-index object_uri")
    parsed = urlsplit(object_uri)
    if (
        parsed.scheme != "supabase"
        or not parsed.netloc
        or parsed.path != f"/sha256/{digest[:2]}/{digest}"
        or parsed.query
        or parsed.fragment
    ):
        raise PrivateEvaluationError(
            "artifact private-index object_uri is not bound to its content digest"
        )
    return result


def _artifact_reference_rows(raw_rows: Any) -> list[dict[str, str]]:
    if not isinstance(raw_rows, list) or not raw_rows:
        raise PrivateEvaluationError("artifact has no released-reference provenance")
    rows: list[dict[str, str]] = []
    seen_items: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise PrivateEvaluationError("artifact released-reference provenance is invalid")
        item_id = _text(raw.get("item_id"), "reference item_id")
        target_id = _text(raw.get("target_id"), "reference target_id")
        source_uri = _text(raw.get("source_uri"), "reference source_uri")
        if source_uri != rcsb_reference_url(target_id):
            raise PrivateEvaluationError(
                f"reference {target_id} is not the canonical released RCSB coordinate"
            )
        if item_id in seen_items:
            raise PrivateEvaluationError("artifact released-reference item IDs are duplicated")
        seen_items.add(item_id)
        rows.append(
            {
                "item_id": item_id,
                "target_id": target_id.upper(),
                "source_uri": source_uri,
                "sha256": _digest(raw.get("sha256"), "reference sha256"),
            }
        )
    rows.sort(key=lambda row: row["item_id"])
    return rows


def _manifest_identities(manifest: Mapping[str, Any], label: str) -> dict[str, set[str]]:
    raw_items = manifest.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise PrivateEvaluationError(f"{label} item identities are invalid")
    identities: dict[str, set[str]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise PrivateEvaluationError(f"{label} item identity is invalid")
        item_id = _text(raw_item.get("id"), f"{label} item id")
        if item_id in identities:
            raise PrivateEvaluationError(f"{label} item IDs are duplicated")
        choices = raw_item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PrivateEvaluationError(f"{label} choice identities are invalid")
        choice_ids: set[str] = set()
        for raw_choice in choices:
            if not isinstance(raw_choice, Mapping):
                raise PrivateEvaluationError(f"{label} choice identity is invalid")
            choice_id = _text(raw_choice.get("id"), f"{label} choice id")
            if choice_id in choice_ids:
                raise PrivateEvaluationError(f"{label} choice IDs are duplicated")
            choice_ids.add(choice_id)
        identities[item_id] = choice_ids
    return identities


def _require_matching_manifest_identities(
    blind: Mapping[str, Any],
    reveal: Mapping[str, Any],
    *,
    label: str,
) -> None:
    blind_identity = _manifest_identities(blind, f"{label} blind manifest")
    reveal_identity = _manifest_identities(reveal, f"{label} reveal manifest")
    if blind_identity != reveal_identity:
        raise PrivateEvaluationError("blind and reveal item identities differ")


def _artifact_prediction_bindings(
    reveal: Mapping[str, Any], references_by_item: Mapping[str, Mapping[str, str]]
) -> list[dict[str, str]]:
    raw_items = reveal.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise PrivateEvaluationError("artifact reveal manifest has no items")
    prediction_bindings: list[dict[str, str]] = []
    seen_items: set[str] = set()
    seen_choices: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise PrivateEvaluationError("artifact reveal item is invalid")
        item_id = _text(raw_item.get("id"), "reveal item id")
        if item_id in seen_items or item_id not in references_by_item:
            raise PrivateEvaluationError("artifact item/reference identities are inconsistent")
        seen_items.add(item_id)
        choices = raw_item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PrivateEvaluationError("artifact reveal item has no choices")
        reference = references_by_item[item_id]
        for raw_choice in choices:
            if not isinstance(raw_choice, Mapping):
                raise PrivateEvaluationError("artifact reveal choice is invalid")
            choice_id = _text(raw_choice.get("id"), "reveal choice id")
            if choice_id in seen_choices:
                raise PrivateEvaluationError("artifact reveal choice IDs are duplicated")
            seen_choices.add(choice_id)
            if (
                raw_choice.get("reference_uri") != reference["source_uri"]
                or raw_choice.get("reference_sha256") != reference["sha256"]
            ):
                raise PrivateEvaluationError(
                    "artifact reveal choice is not bound to its released reference"
                )
            prediction_bindings.append(
                {
                    "item_id": item_id,
                    "choice_id": choice_id,
                    "run_id": _text(raw_choice.get("run_id"), "choice run_id"),
                    "sample_id": _text(raw_choice.get("sample_id"), "choice sample_id"),
                    "prediction_sha256": _digest(
                        raw_choice.get("prediction_sha256"), "prediction sha256"
                    ),
                }
            )
    if seen_items != set(references_by_item):
        raise PrivateEvaluationError(
            "artifact released references do not exactly match reveal items"
        )
    prediction_bindings.sort(key=lambda row: (row["item_id"], row["choice_id"]))
    return prediction_bindings


def _answer_overlay_rows(
    raw_rows: Any,
    reveal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise PrivateEvaluationError("answer overlays are missing")
    reveal_items = reveal.get("items")
    if not isinstance(reveal_items, list) or not reveal_items:
        raise PrivateEvaluationError("answer overlays require reveal items")
    expected: dict[str, dict[str, Mapping[str, Any]]] = {}
    for item in reveal_items:
        if not isinstance(item, Mapping):
            raise PrivateEvaluationError("answer overlay reveal item is invalid")
        item_id = _text(item.get("id"), "answer overlay reveal item_id")
        choices = item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PrivateEvaluationError("answer overlay reveal choices are invalid")
        by_id: dict[str, Mapping[str, Any]] = {}
        for choice in choices:
            if (
                not isinstance(choice, Mapping)
                or isinstance(choice.get("rmsd"), bool)
                or not isinstance(choice.get("rmsd"), (int, float))
                or not isinstance(choice.get("correct"), bool)
            ):
                raise PrivateEvaluationError("answer overlay reveal choice is invalid")
            choice_id = _text(choice.get("id"), "answer overlay reveal choice_id")
            if choice_id in by_id:
                raise PrivateEvaluationError("answer overlay reveal choices are duplicated")
            by_id[choice_id] = choice
        expected[item_id] = by_id

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise PrivateEvaluationError("answer overlay row is invalid")
        item_id = _text(raw.get("item_id"), "answer overlay item_id")
        if item_id in seen or item_id not in expected:
            raise PrivateEvaluationError("answer overlay item identities are inconsistent")
        seen.add(item_id)
        expected_choices = expected[item_id]
        best_choice = min(
            expected_choices.values(),
            key=lambda choice: (float(choice["rmsd"]), str(choice["id"])),
        )
        crystal_source_id = _text(
            raw.get("crystal_source_id"), "answer overlay crystal_source_id"
        )
        crystal_source_rmsd = raw.get("crystal_source_rmsd")
        if (
            crystal_source_id != best_choice.get("id")
            or isinstance(crystal_source_rmsd, bool)
            or not isinstance(crystal_source_rmsd, (int, float))
            or float(crystal_source_rmsd) != float(best_choice.get("rmsd"))
        ):
            raise PrivateEvaluationError("answer overlay crystal binding is inconsistent")
        crystal_pdb = _text(
            raw.get("crystal_ligand_pdb"), "answer overlay crystal ligand PDB"
        )
        if (
            len(crystal_pdb.encode("utf-8")) > 1_000_000
            or not crystal_pdb.endswith("\nEND\n")
        ):
            raise PrivateEvaluationError("answer overlay crystal ligand PDB is invalid")
        crystal_sha256 = _digest(
            raw.get("crystal_ligand_sha256"),
            "answer overlay crystal ligand sha256",
        )
        if hashlib.sha256(crystal_pdb.encode("utf-8")).hexdigest() != crystal_sha256:
            raise PrivateEvaluationError(
                "answer overlay crystal ligand digest is inconsistent"
            )
        raw_poses = raw.get("poses")
        if not isinstance(raw_poses, list):
            raise PrivateEvaluationError("answer overlay poses are missing")
        poses: list[dict[str, Any]] = []
        seen_poses: set[str] = set()
        for raw_pose in raw_poses:
            if not isinstance(raw_pose, Mapping):
                raise PrivateEvaluationError("answer overlay pose is invalid")
            choice_id = _text(raw_pose.get("id"), "answer overlay pose id")
            expected_choice = expected_choices.get(choice_id)
            if expected_choice is None or choice_id in seen_poses:
                raise PrivateEvaluationError("answer overlay pose identities are inconsistent")
            seen_poses.add(choice_id)
            rmsd = raw_pose.get("rmsd")
            if (
                isinstance(rmsd, bool)
                or not isinstance(rmsd, (int, float))
                or float(rmsd) != float(expected_choice.get("rmsd"))
                or raw_pose.get("correct") is not expected_choice.get("correct")
            ):
                raise PrivateEvaluationError("answer overlay pose score is inconsistent")
            pose_pdb = _text(
                raw_pose.get("predicted_pose_pdb"), "answer overlay pose PDB"
            )
            if (
                len(pose_pdb.encode("utf-8")) > 1_000_000
                or not pose_pdb.endswith("\nEND\n")
            ):
                raise PrivateEvaluationError("answer overlay pose PDB is invalid")
            pose_sha256 = _digest(
                raw_pose.get("predicted_pose_sha256"), "answer overlay pose sha256"
            )
            if hashlib.sha256(pose_pdb.encode("utf-8")).hexdigest() != pose_sha256:
                raise PrivateEvaluationError("answer overlay pose digest is inconsistent")
            pose_crystal_pdb = _text(
                raw_pose.get("crystal_ligand_pdb"),
                "answer overlay pose crystal ligand PDB",
            )
            if (
                len(pose_crystal_pdb.encode("utf-8")) > 1_000_000
                or not pose_crystal_pdb.endswith("\nEND\n")
            ):
                raise PrivateEvaluationError(
                    "answer overlay pose crystal ligand PDB is invalid"
                )
            pose_crystal_sha256 = _digest(
                raw_pose.get("crystal_ligand_sha256"),
                "answer overlay pose crystal ligand sha256",
            )
            if (
                hashlib.sha256(pose_crystal_pdb.encode("utf-8")).hexdigest()
                != pose_crystal_sha256
            ):
                raise PrivateEvaluationError(
                    "answer overlay pose crystal ligand digest is inconsistent"
                )
            pose_pocket_pdb = _text(
                raw_pose.get("crystal_pocket_pdb"),
                "answer overlay pose crystal pocket PDB",
            )
            if (
                len(pose_pocket_pdb.encode("utf-8")) > 1_000_000
                or not pose_pocket_pdb.endswith("\nEND\n")
            ):
                raise PrivateEvaluationError(
                    "answer overlay pose crystal pocket PDB is invalid"
                )
            pose_pocket_sha256 = _digest(
                raw_pose.get("crystal_pocket_sha256"),
                "answer overlay pose crystal pocket sha256",
            )
            if (
                hashlib.sha256(pose_pocket_pdb.encode("utf-8")).hexdigest()
                != pose_pocket_sha256
            ):
                raise PrivateEvaluationError(
                    "answer overlay pose crystal pocket digest is inconsistent"
                )
            poses.append(
                {
                    "id": choice_id,
                    "rmsd": float(rmsd),
                    "correct": bool(raw_pose["correct"]),
                    "predicted_pose_pdb": pose_pdb,
                    "predicted_pose_sha256": pose_sha256,
                    "crystal_ligand_pdb": pose_crystal_pdb,
                    "crystal_ligand_sha256": pose_crystal_sha256,
                    "crystal_pocket_pdb": pose_pocket_pdb,
                    "crystal_pocket_sha256": pose_pocket_sha256,
                }
            )
        if seen_poses != set(expected_choices):
            raise PrivateEvaluationError(
                "answer overlay poses do not exactly cover reveal choices"
            )
        poses.sort(key=lambda row: row["id"])
        source_pose = next(row for row in poses if row["id"] == crystal_source_id)
        if (
            source_pose["crystal_ligand_pdb"] != crystal_pdb
            or source_pose["crystal_ligand_sha256"] != crystal_sha256
        ):
            raise PrivateEvaluationError(
                "answer overlay crystal source ligand is inconsistent"
            )
        rows.append(
            {
                "item_id": item_id,
                "crystal_source_id": crystal_source_id,
                "crystal_source_rmsd": float(crystal_source_rmsd),
                "crystal_ligand_pdb": crystal_pdb,
                "crystal_ligand_sha256": crystal_sha256,
                "poses": poses,
            }
        )
    if seen != set(expected):
        raise PrivateEvaluationError("answer overlays do not exactly cover reveal items")
    rows.sort(key=lambda row: row["item_id"])
    return rows


def describe_private_evaluation_artifact(
    content: bytes,
    *,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Parse stored artifact bytes and recompute the Preview integrity descriptor."""

    if not isinstance(content, bytes) or not content:
        raise PrivateEvaluationError("artifact content is empty")
    artifact_sha256 = hashlib.sha256(content).hexdigest()
    if expected_artifact_sha256 is not None:
        expected_artifact_sha256 = _digest(
            expected_artifact_sha256, "expected artifact sha256"
        )
        if artifact_sha256 != expected_artifact_sha256:
            raise PrivateEvaluationError("artifact content does not match expected digest")
    try:
        decoded = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateEvaluationError("artifact is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise PrivateEvaluationError("artifact must be an object")
    try:
        json.dumps(decoded, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PrivateEvaluationError("artifact is not finite JSON") from exc
    canonical_bytes = canonical_json(decoded).encode("utf-8")
    if canonical_bytes != content:
        raise PrivateEvaluationError("artifact bytes are not canonical JSON")

    format_version = decoded.get("format_version")
    if format_version != PRIVATE_EVALUATION_FORMAT_VERSION:
        raise PrivateEvaluationError("artifact format_version is invalid")

    round_block = decoded.get("round")
    if not isinstance(round_block, Mapping):
        raise PrivateEvaluationError("artifact round block is missing")
    round_id = _text(round_block.get("round_id"), "round_id")
    campaign_id = _text(round_block.get("campaign_id"), "campaign_id")
    environment = round_block.get("environment")
    if environment != "production":
        raise PrivateEvaluationError("artifact round environment is invalid")
    opens_at = _text(round_block.get("opens_at"), "opens_at")
    closes_at = _text(round_block.get("closes_at"), "closes_at")
    blind_sha256 = _digest(
        round_block.get("blind_manifest_sha256"), "blind manifest sha256"
    )
    private_index = _artifact_private_index_descriptor(round_block.get("private_index"))
    storage_bucket = urlsplit(private_index["object_uri"]).netloc

    policy = decoded.get("policy")
    if not isinstance(policy, Mapping):
        raise PrivateEvaluationError("artifact policy block is missing")
    if policy.get("reveal_policy_version") != REVEAL_POLICY_VERSION:
        raise PrivateEvaluationError("artifact reveal policy version is invalid")
    if policy.get("acceptance_policy_version") != ACCEPTANCE_POLICY_VERSION:
        raise PrivateEvaluationError("artifact acceptance policy version is invalid")
    if policy.get("correct_rmsd_threshold_angstrom") != CORRECT_RMSD_ANGSTROM:
        raise PrivateEvaluationError("artifact RMSD threshold is invalid")
    raw_evaluator_versions = policy.get("evaluator_versions")
    if not isinstance(raw_evaluator_versions, list) or not raw_evaluator_versions:
        raise PrivateEvaluationError("artifact evaluator_versions is invalid")
    evaluator_versions = sorted(
        _text(version, "evaluator version") for version in raw_evaluator_versions
    )
    if list(raw_evaluator_versions) != evaluator_versions:
        raise PrivateEvaluationError("artifact evaluator_versions must be sorted")

    reveal = decoded.get("reveal_manifest")
    if not isinstance(reveal, Mapping):
        raise PrivateEvaluationError("artifact reveal manifest is missing")
    reveal = deepcopy(dict(reveal))
    blind_manifest = decoded.get("blind_manifest")
    blind_canonical = _artifact_manifest_canonical_json(
        decoded.get("blind_manifest_canonical_json"),
        blind_manifest,
        "blind_manifest",
    )
    if _manifest_canonical_digest(blind_canonical) != blind_sha256:
        raise PrivateEvaluationError("artifact blind manifest digest is inconsistent")
    reveal_canonical = _artifact_manifest_canonical_json(
        decoded.get("reveal_manifest_canonical_json"),
        reveal,
        "reveal_manifest",
    )
    reveal_sha256 = _manifest_canonical_digest(reveal_canonical)
    if reveal.get("round_id") != round_id:
        raise PrivateEvaluationError("artifact reveal manifest round_id is invalid")
    if reveal.get("blind_manifest_sha256") != blind_sha256:
        raise PrivateEvaluationError("artifact reveal manifest blind digest is invalid")

    if not isinstance(blind_manifest, Mapping):
        raise PrivateEvaluationError("artifact blind manifest is missing")
    blind_manifest = deepcopy(dict(blind_manifest))
    _require_matching_manifest_identities(
        blind_manifest,
        reveal,
        label="artifact",
    )

    references = _artifact_reference_rows(decoded.get("references"))
    references_by_item = {row["item_id"]: row for row in references}
    prediction_bindings = _artifact_prediction_bindings(reveal, references_by_item)
    reference_set_sha256 = hashlib.sha256(
        canonical_json(references).encode("utf-8")
    ).hexdigest()
    prediction_set_sha256 = hashlib.sha256(
        canonical_json(prediction_bindings).encode("utf-8")
    ).hexdigest()
    answer_overlays = _answer_overlay_rows(decoded.get("answer_overlays"), reveal)
    answer_overlay_set_sha256 = hashlib.sha256(
        canonical_json(answer_overlays).encode("utf-8")
    ).hexdigest()

    integrity = decoded.get("integrity")
    if not isinstance(integrity, Mapping):
        raise PrivateEvaluationError("artifact integrity block is missing")
    if integrity.get("reveal_manifest_sha256") != reveal_sha256:
        raise PrivateEvaluationError("artifact reveal-manifest digest is inconsistent")
    if integrity.get("reference_set_sha256") != reference_set_sha256:
        raise PrivateEvaluationError("artifact reference-set digest is inconsistent")
    if integrity.get("prediction_set_sha256") != prediction_set_sha256:
        raise PrivateEvaluationError("artifact prediction-set digest is inconsistent")
    if integrity.get("answer_overlay_set_sha256") != answer_overlay_set_sha256:
        raise PrivateEvaluationError("artifact answer-overlay digest is inconsistent")

    counts = decoded.get("counts")
    if not isinstance(counts, Mapping):
        raise PrivateEvaluationError("artifact counts block is missing")
    item_count = _positive_int(counts.get("item_count"), "item_count")
    choice_count = _positive_int(counts.get("choice_count"), "choice_count")
    if item_count != len(reveal.get("items", [])):
        raise PrivateEvaluationError("artifact item_count is inconsistent")
    if choice_count != len(prediction_bindings):
        raise PrivateEvaluationError("artifact choice_count is inconsistent")

    descriptor = {
        "evaluation_id": stable_id(
            "weekly_eval",
            {
                "format_version": PRIVATE_EVALUATION_FORMAT_VERSION,
                "round_id": round_id,
                "blind_manifest_sha256": blind_sha256,
                "private_index_sha256": private_index["sha256"],
                "artifact_sha256": artifact_sha256,
            },
            length=32,
        ),
        "round_id": round_id,
        "campaign_id": campaign_id,
        "environment": "production",
        "round_opens_at": opens_at,
        "round_closes_at": closes_at,
        "blind_manifest_sha256": blind_sha256,
        "private_index_sha256": private_index["sha256"],
        "reveal_manifest_sha256": reveal_sha256,
        "reference_set_sha256": reference_set_sha256,
        "prediction_set_sha256": prediction_set_sha256,
        "format_version": PRIVATE_EVALUATION_FORMAT_VERSION,
        "evaluator_versions": evaluator_versions,
        "reveal_policy_version": REVEAL_POLICY_VERSION,
        "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
        "correct_rmsd_threshold_angstrom": CORRECT_RMSD_ANGSTROM,
        "item_count": item_count,
        "choice_count": choice_count,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": len(content),
        "artifact_media_type": PRIVATE_EVALUATION_MEDIA_TYPE,
        "artifact_object_uri": _content_addressed_supabase_uri(
            storage_bucket, artifact_sha256
        ),
    }
    return descriptor


def _reference_rows(result: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_rows = result.get("references")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise PrivateEvaluationError("evaluation has no released-reference provenance")
    rows: list[dict[str, str]] = []
    seen_items: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise PrivateEvaluationError("released-reference provenance is invalid")
        item_id = _text(raw.get("item_id"), "reference item_id")
        target_id = _text(raw.get("target_id"), "reference target_id")
        source_uri = _text(raw.get("source_uri"), "reference source_uri")
        if source_uri != rcsb_reference_url(target_id):
            raise PrivateEvaluationError(
                f"reference {target_id} is not the canonical released RCSB coordinate"
            )
        if item_id in seen_items:
            raise PrivateEvaluationError("released-reference item IDs are duplicated")
        seen_items.add(item_id)
        rows.append(
            {
                "item_id": item_id,
                "target_id": target_id.upper(),
                "source_uri": source_uri,
                "sha256": _digest(raw.get("sha256"), "reference sha256"),
            }
        )
    rows.sort(key=lambda row: row["item_id"])
    return rows


def build_private_evaluation_artifact(
    round_record: Mapping[str, Any], result: Mapping[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    """Build deterministic private bytes and their catalog-ready integrity fields."""

    round_id = _text(round_record.get("round_id"), "round_id")
    if result.get("round_id") != round_id:
        raise PrivateEvaluationError("evaluation result belongs to another round")
    if round_record.get("environment") != "production":
        raise PrivateEvaluationError("private evaluation artifact requires production")
    if round_record.get("status") not in {"open", "revealed"}:
        raise PrivateEvaluationError("private evaluation artifact requires an eligible round")
    if result.get("status") not in {
        "evaluated-private-preclose",
        "evaluated-private-postclose",
    }:
        raise PrivateEvaluationError("result is not a private weekly evaluation")

    blind_sha256 = _digest(
        round_record.get("blind_manifest_sha256"), "blind manifest sha256"
    )
    blind_manifest = round_record.get("blind_manifest")
    if not isinstance(blind_manifest, Mapping):
        raise PrivateEvaluationError("round blind manifest is missing")
    blind_manifest = deepcopy(dict(blind_manifest))
    if manifest_sha256(blind_manifest) != blind_sha256:
        raise PrivateEvaluationError("blind manifest does not match the round digest")
    blind_manifest_canonical_json = canonical_json(blind_manifest)
    private_index = _private_index_descriptor(round_record)
    reveal = result.get("reveal_manifest")
    if not isinstance(reveal, Mapping):
        raise PrivateEvaluationError("evaluation has no reveal manifest")
    reveal = deepcopy(dict(reveal))
    reveal_manifest_canonical_json = canonical_json(reveal)
    reveal_sha256 = _manifest_canonical_digest(reveal_manifest_canonical_json)
    if result.get("reveal_manifest_sha256") != reveal_sha256:
        raise PrivateEvaluationError("evaluation reveal-manifest digest is inconsistent")
    if (
        reveal.get("round_id") != round_id
        or reveal.get("blind_manifest_sha256") != blind_sha256
    ):
        raise PrivateEvaluationError("evaluation reveal manifest is not bound to the round")
    _require_matching_manifest_identities(blind_manifest, reveal, label="evaluation")

    references = _reference_rows(result)
    references_by_item = {row["item_id"]: row for row in references}
    raw_items = reveal.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise PrivateEvaluationError("evaluation reveal manifest has no items")
    prediction_bindings: list[dict[str, str]] = []
    evaluator_versions: set[str] = set()
    seen_items: set[str] = set()
    seen_choices: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise PrivateEvaluationError("evaluation reveal item is invalid")
        item_id = _text(raw_item.get("id"), "reveal item id")
        if item_id in seen_items or item_id not in references_by_item:
            raise PrivateEvaluationError("evaluation item/reference identities are inconsistent")
        seen_items.add(item_id)
        choices = raw_item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise PrivateEvaluationError("evaluation reveal item has no choices")
        reference = references_by_item[item_id]
        for raw_choice in choices:
            if not isinstance(raw_choice, Mapping):
                raise PrivateEvaluationError("evaluation reveal choice is invalid")
            choice_id = _text(raw_choice.get("id"), "reveal choice id")
            if choice_id in seen_choices:
                raise PrivateEvaluationError("evaluation reveal choice IDs are duplicated")
            seen_choices.add(choice_id)
            if (
                raw_choice.get("reference_uri") != reference["source_uri"]
                or raw_choice.get("reference_sha256") != reference["sha256"]
            ):
                raise PrivateEvaluationError(
                    "evaluation choice is not bound to its released reference"
                )
            evaluator_versions.add(
                _text(raw_choice.get("evaluator_version"), "evaluator version")
            )
            prediction_bindings.append(
                {
                    "item_id": item_id,
                    "choice_id": choice_id,
                    "run_id": _text(raw_choice.get("run_id"), "choice run_id"),
                    "sample_id": _text(
                        raw_choice.get("sample_id"), "choice sample_id"
                    ),
                    "prediction_sha256": _digest(
                        raw_choice.get("prediction_sha256"), "prediction sha256"
                    ),
                }
            )
    if seen_items != set(references_by_item):
        raise PrivateEvaluationError("released references do not exactly match reveal items")

    item_count = len(raw_items)
    choice_count = len(prediction_bindings)
    if result.get("item_count") != item_count or result.get("choice_count") != choice_count:
        raise PrivateEvaluationError("evaluation counts are inconsistent")
    prediction_bindings.sort(key=lambda row: (row["item_id"], row["choice_id"]))
    reference_set_sha256 = hashlib.sha256(
        canonical_json(references).encode("utf-8")
    ).hexdigest()
    prediction_set_sha256 = hashlib.sha256(
        canonical_json(prediction_bindings).encode("utf-8")
    ).hexdigest()
    answer_overlays = _answer_overlay_rows(result.get("answer_overlays"), reveal)
    answer_overlay_set_sha256 = hashlib.sha256(
        canonical_json(answer_overlays).encode("utf-8")
    ).hexdigest()

    artifact = {
        "format_version": PRIVATE_EVALUATION_FORMAT_VERSION,
        "round": {
            "round_id": round_id,
            "campaign_id": _text(round_record.get("campaign_id"), "campaign_id"),
            "environment": "production",
            "opens_at": _text(round_record.get("opens_at"), "opens_at"),
            "closes_at": _text(round_record.get("closes_at"), "closes_at"),
            "blind_manifest_sha256": blind_sha256,
            "private_index": {
                "object_uri": private_index["object_uri"],
                "sha256": private_index["sha256"],
            },
        },
        "policy": {
            "reveal_policy_version": REVEAL_POLICY_VERSION,
            "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
            "correct_rmsd_threshold_angstrom": CORRECT_RMSD_ANGSTROM,
            "evaluator_versions": sorted(evaluator_versions),
        },
        "integrity": {
            "reveal_manifest_sha256": reveal_sha256,
            "reference_set_sha256": reference_set_sha256,
            "prediction_set_sha256": prediction_set_sha256,
            "answer_overlay_set_sha256": answer_overlay_set_sha256,
        },
        "counts": {"item_count": item_count, "choice_count": choice_count},
        "references": references,
        "answer_overlays": answer_overlays,
        "blind_manifest": blind_manifest,
        "blind_manifest_canonical_json": blind_manifest_canonical_json,
        "reveal_manifest": reveal,
        "reveal_manifest_canonical_json": reveal_manifest_canonical_json,
    }
    # This is the exact finite canonical representation stored and hashed.  A
    # wall-clock timestamp is deliberately absent so identical scientific
    # inputs produce identical bytes on every retry.
    try:
        json.dumps(artifact, allow_nan=False)
        artifact_bytes = canonical_json(artifact).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PrivateEvaluationError("private evaluation artifact is not finite JSON") from exc
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    descriptor = {
        "evaluation_id": stable_id(
            "weekly_eval",
            {
                "format_version": PRIVATE_EVALUATION_FORMAT_VERSION,
                "round_id": round_id,
                "blind_manifest_sha256": blind_sha256,
                "private_index_sha256": private_index["sha256"],
                "artifact_sha256": artifact_sha256,
            },
            length=32,
        ),
        "round_id": round_id,
        "campaign_id": artifact["round"]["campaign_id"],
        "environment": "production",
        "round_opens_at": artifact["round"]["opens_at"],
        "round_closes_at": artifact["round"]["closes_at"],
        "blind_manifest_sha256": blind_sha256,
        "private_index_sha256": private_index["sha256"],
        "reveal_manifest_sha256": reveal_sha256,
        "reference_set_sha256": reference_set_sha256,
        "prediction_set_sha256": prediction_set_sha256,
        "format_version": PRIVATE_EVALUATION_FORMAT_VERSION,
        "evaluator_versions": sorted(evaluator_versions),
        "reveal_policy_version": REVEAL_POLICY_VERSION,
        "acceptance_policy_version": ACCEPTANCE_POLICY_VERSION,
        "correct_rmsd_threshold_angstrom": CORRECT_RMSD_ANGSTROM,
        "item_count": item_count,
        "choice_count": choice_count,
        "artifact_sha256": artifact_sha256,
        "artifact_size_bytes": len(artifact_bytes),
        "artifact_media_type": PRIVATE_EVALUATION_MEDIA_TYPE,
    }
    return artifact_bytes, descriptor


def _legacy_target_ids_missing_eligibility(
    round_record: Mapping[str, Any],
    private_index_content: bytes,
) -> list[str]:
    metadata = round_record.get("metadata")
    descriptor = metadata.get("private_index") if isinstance(metadata, Mapping) else None
    expected = descriptor.get("sha256") if isinstance(descriptor, Mapping) else None
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise PrivateEvaluationError("round metadata has no valid private-index digest")
    if not isinstance(private_index_content, bytes) or not private_index_content:
        raise PrivateEvaluationError("private-index download returned no bytes")
    if hashlib.sha256(private_index_content).hexdigest() != expected:
        raise PrivateEvaluationError("private-index content does not match its recorded digest")
    try:
        decoded = json.loads(private_index_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateEvaluationError("private index is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise PrivateEvaluationError("private index must be an object")
    items = decoded.get("items")
    if not isinstance(items, list):
        raise PrivateEvaluationError("private index has no items")
    missing: set[str] = set()
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        if isinstance(raw_item.get("ligand_eligibility"), Mapping):
            continue
        target_id = raw_item.get("target_id")
        if isinstance(target_id, str) and target_id.strip():
            missing.add(target_id.strip().upper())
    return sorted(missing)


def _private_index_object(private_index_content: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(private_index_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateEvaluationError("private index is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise PrivateEvaluationError("private index must be an object")
    return deepcopy(dict(decoded))


def _legacy_item_ligand_bindings(
    private_index: Mapping[str, Any],
    target_ids: list[str],
) -> dict[str, dict[str, Any]]:
    items = private_index.get("items")
    if not isinstance(items, list):
        raise PrivateEvaluationError("private index has no items")
    required = {target_id.strip().upper() for target_id in target_ids}
    bindings: dict[str, dict[str, Any]] = {}
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        target_id = raw_item.get("target_id")
        ligand = raw_item.get("ligand")
        if not isinstance(target_id, str) or not target_id.strip():
            continue
        normalized_target = target_id.strip().upper()
        if normalized_target not in required:
            continue
        if not isinstance(ligand, Mapping):
            raise PrivateEvaluationError(
                f"legacy recovery private item for {normalized_target} has no ligand"
            )
        component_id = ligand.get("component_id")
        heavy_atoms = ligand.get("heavy_atoms")
        if not isinstance(component_id, str) or not component_id.strip():
            raise PrivateEvaluationError(
                f"legacy recovery ligand component_id is invalid for {normalized_target}"
            )
        if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
            raise PrivateEvaluationError(
                f"legacy recovery ligand heavy_atoms is invalid for {normalized_target}"
            )
        normalized_component = component_id.strip().upper()
        binding = {
            "component_id": normalized_component,
            "heavy_atoms": heavy_atoms,
        }
        previous = bindings.get(normalized_target)
        if previous is not None and previous != binding:
            raise PrivateEvaluationError(
                f"legacy recovery private items disagree on ligand binding for {normalized_target}"
            )
        bindings[normalized_target] = binding
    missing = sorted(required.difference(bindings))
    if missing:
        raise PrivateEvaluationError(
            "legacy recovery private index is missing ligand bindings for "
            + ", ".join(missing)
        )
    return bindings


def _legacy_recovered_ligand_eligibility(
    component_id: str,
    heavy_atoms: int,
    smiles: str,
) -> dict[str, Any]:
    """Rebuild eligibility for legacy rounds using immutable item ligand binding."""

    if not isinstance(component_id, str) or not component_id.strip():
        raise PrivateEvaluationError("legacy recovery ligand component_id is invalid")
    if isinstance(heavy_atoms, bool) or not isinstance(heavy_atoms, int) or heavy_atoms < 1:
        raise PrivateEvaluationError("legacy recovery ligand heavy_atoms is invalid")
    if not isinstance(smiles, str) or not smiles.strip():
        raise PrivateEvaluationError("legacy recovery ligand SMILES is invalid")
    normalized_component = component_id.strip().upper()
    normalized_smiles = smiles.strip()
    rejection_reason = ligand_rejection_reason(
        {"component_id": normalized_component, "smiles": normalized_smiles},
        heavy_atom_minimum=HEAVY_ATOM_MINIMUM,
    )
    passed = rejection_reason is None and heavy_atoms >= HEAVY_ATOM_MINIMUM
    if passed is False and rejection_reason is None and heavy_atoms < HEAVY_ATOM_MINIMUM:
        rejection_reason = "below-heavy-atom-minimum"
    return {
        "policy": SELECTION_POLICY_VERSION,
        "passed": passed,
        "component_id": normalized_component,
        "heavy_atoms": heavy_atoms,
        "smiles": normalized_smiles,
        "smiles_sha256": hashlib.sha256(normalized_smiles.encode("utf-8")).hexdigest(),
        "reason": rejection_reason,
    }


def _recovered_ligand_eligibility_for_legacy_items(
    coordinator: Any,
    round_record: Mapping[str, Any],
    target_ids: list[str],
    *,
    private_index: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    campaign_id = round_record.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id:
        raise PrivateEvaluationError("round has no campaign_id for legacy recovery")
    bindings = _legacy_item_ligand_bindings(private_index, target_ids)
    packages = coordinator.fetch_campaign_target_packages(campaign_id, target_ids)
    recovered: dict[str, dict[str, Any]] = {}
    for target_id in target_ids:
        normalized_target = target_id.strip().upper()
        binding = bindings[normalized_target]
        package_row = packages.get(normalized_target)
        if not isinstance(package_row, Mapping):
            raise PrivateEvaluationError(
                f"legacy recovery did not return a package for {normalized_target}"
            )
        package = package_row.get("package")
        if not isinstance(package, Mapping):
            raise PrivateEvaluationError(
                f"legacy recovery package for {normalized_target} is not an object"
            )
        package_component, _package_heavy_atoms, _chain_ids, smiles = _selected_ligand(
            package
        )
        if package_component != binding["component_id"]:
            raise PrivateEvaluationError(
                "legacy recovery package component_id disagrees with item ligand"
            )
        recovered[normalized_target] = _legacy_recovered_ligand_eligibility(
            binding["component_id"],
            binding["heavy_atoms"],
            smiles,
        )
    return recovered


def recover_legacy_ligand_eligibility(
    coordinator: Any,
    round_record: Mapping[str, Any],
    private_index_content: bytes,
) -> dict[str, dict[str, Any]] | None:
    """Recover ligand provenance for rounds assembled before it was embedded."""

    legacy_target_ids = _legacy_target_ids_missing_eligibility(
        round_record, private_index_content
    )
    if not legacy_target_ids:
        return None
    private_index = _private_index_object(private_index_content)
    return _recovered_ligand_eligibility_for_legacy_items(
        coordinator,
        round_record,
        legacy_target_ids,
        private_index=private_index,
    )


def _materialization_report(
    round_id: str,
    descriptor: Mapping[str, Any],
    object_uri: str,
    *,
    register_catalog: bool,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    integrity_descriptor = deepcopy(dict(descriptor))
    return {
        "status": "materialized-private-preclose",
        "round_id": round_id,
        "register_catalog": register_catalog,
        "catalog_registered": register_catalog and catalog is not None,
        "evaluation_id": descriptor["evaluation_id"],
        "item_count": descriptor["item_count"],
        "choice_count": descriptor["choice_count"],
        "blind_manifest_sha256": descriptor["blind_manifest_sha256"],
        "private_index_sha256": descriptor["private_index_sha256"],
        "reveal_manifest_sha256": descriptor["reveal_manifest_sha256"],
        "artifact": {
            "object_uri": object_uri,
            "sha256": descriptor["artifact_sha256"],
            "size_bytes": descriptor["artifact_size_bytes"],
            "media_type": descriptor["artifact_media_type"],
        },
        "integrity_descriptor": integrity_descriptor,
        "catalog_created_at": catalog.get("created_at") if catalog else None,
    }


def materialize_private_evaluation_result(
    round_record: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    coordinator: Any,
    register_catalog: bool,
    bucket_verified: bool = False,
) -> dict[str, Any]:
    """Store and optionally catalog one already-computed private evaluation."""

    if not bucket_verified:
        coordinator.require_private_bucket()
    round_id = _text(round_record.get("round_id"), "round_id")
    artifact_content, descriptor = build_private_evaluation_artifact(
        round_record, result
    )
    stored = coordinator.store_bytes(
        artifact_content, PRIVATE_EVALUATION_MEDIA_TYPE
    )
    for field in ("sha256", "size_bytes", "media_type"):
        expected = descriptor[f"artifact_{field}"]
        if stored.get(field) != expected:
            raise PrivateEvaluationError(f"stored private artifact {field} is inconsistent")
    object_uri = _text(stored.get("object_uri"), "stored artifact object_uri")
    parsed_object = urlsplit(object_uri)
    if (
        parsed_object.scheme != "supabase"
        or parsed_object.netloc != coordinator.storage_bucket
        or parsed_object.path
        != (
            f"/sha256/{descriptor['artifact_sha256'][:2]}/"
            f"{descriptor['artifact_sha256']}"
        )
        or parsed_object.query
        or parsed_object.fragment
    ):
        raise PrivateEvaluationError("stored artifact URI is not content-addressed")
    descriptor["artifact_object_uri"] = object_uri
    catalog: Mapping[str, Any] | None = None
    if register_catalog:
        catalog = coordinator.register_private_weekly_evaluation(descriptor)
        if not isinstance(catalog, Mapping):
            raise PrivateEvaluationError(
                "private evaluation catalog returned no descriptor"
            )
        for field, expected in descriptor.items():
            if catalog.get(field) != expected:
                raise PrivateEvaluationError(
                    f"private evaluation catalog changed descriptor field {field}"
                )
    return _materialization_report(
        round_id,
        descriptor,
        object_uri,
        register_catalog=register_catalog,
        catalog=catalog,
    )


def _aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PrivateEvaluationError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateEvaluationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise PrivateEvaluationError(f"{field} must include a timezone")
    return parsed


def _materialize_private_weekly_evaluation(
    round_id: str,
    destination: str | Path,
    *,
    coordinator: Any,
    prediction_resolver: CoordinateResolver | None = None,
    reference_resolver: CoordinateResolver = fetch_rcsb_released_reference,
    evaluator: PoseEvaluator = evaluate_ligand_pose,
    now: datetime | None = None,
    register_catalog: bool = False,
    allow_after_close: bool = False,
    allow_revealed: bool = False,
    require_allowlist: bool = True,
    require_closed: bool = False,
) -> dict[str, Any]:
    if require_allowlist and round_id not in ALLOWED_PRECLOSE_EVALUATION_ROUND_IDS:
        raise PrivateEvaluationError(
            "round_id is not explicitly allow-listed for pre-close evaluation"
        )
    coordinator.require_private_bucket()
    round_record, private_index_content = coordinator.weekly_quiz_reveal_inputs(round_id)
    if round_record.get("round_id") != round_id:
        raise PrivateEvaluationError("coordinator returned a different weekly round")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise PrivateEvaluationError("now must be a timezone-aware datetime")
    if require_closed and current < _aware_datetime(
        round_record.get("closes_at"), "round closes_at"
    ):
        raise PrivateEvaluationError("weekly voting has not closed yet")

    recovered_ligand_eligibility = recover_legacy_ligand_eligibility(
        coordinator,
        round_record,
        private_index_content,
    )

    resolver = prediction_resolver
    if resolver is None:
        resolver = lambda choice: coordinator.download_predicted_complex(
            choice.get("run_id"), choice.get("sample_id")
        )
    result = run_private_preclose_evaluation(
        round_record,
        private_index_content,
        destination,
        prediction_resolver=resolver,
        reference_resolver=reference_resolver,
        evaluator=evaluator,
        now=now,
        recovered_ligand_eligibility=recovered_ligand_eligibility,
        allow_after_close=allow_after_close,
        allow_revealed=allow_revealed,
    )
    return materialize_private_evaluation_result(
        round_record,
        result,
        coordinator=coordinator,
        register_catalog=register_catalog,
        bucket_verified=True,
    )


def materialize_private_preclose_evaluation(
    round_id: str,
    destination: str | Path,
    *,
    coordinator: Any,
    prediction_resolver: CoordinateResolver | None = None,
    reference_resolver: CoordinateResolver = fetch_rcsb_released_reference,
    evaluator: PoseEvaluator = evaluate_ligand_pose,
    now: datetime | None = None,
    register_catalog: bool = False,
    allow_after_close: bool = False,
) -> dict[str, Any]:
    """Evaluate and privately store one explicitly allow-listed catch-up round."""

    report = _materialize_private_weekly_evaluation(
        round_id,
        destination,
        coordinator=coordinator,
        prediction_resolver=prediction_resolver,
        reference_resolver=reference_resolver,
        evaluator=evaluator,
        now=now,
        register_catalog=register_catalog,
        allow_after_close=allow_after_close,
    )
    return report


def materialize_postclose_weekly_evaluation(
    round_id: str,
    destination: str | Path,
    *,
    coordinator: Any,
    prediction_resolver: CoordinateResolver | None = None,
    reference_resolver: CoordinateResolver = fetch_rcsb_released_reference,
    evaluator: PoseEvaluator = evaluate_ligand_pose,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Idempotently materialize one closed production round without publishing it."""

    existing = coordinator.private_weekly_evaluation(round_id)
    if existing is not None:
        if not isinstance(existing, Mapping):
            raise PrivateEvaluationError("private evaluation catalog lookup is invalid")
        report = _materialization_report(
            round_id,
            existing,
            _text(existing.get("artifact_object_uri"), "catalog artifact object_uri"),
            register_catalog=True,
            catalog=existing,
        )
        return {**report, "status": "already-materialized-private-postclose"}
    report = _materialize_private_weekly_evaluation(
        round_id,
        destination,
        coordinator=coordinator,
        prediction_resolver=prediction_resolver,
        reference_resolver=reference_resolver,
        evaluator=evaluator,
        now=now,
        register_catalog=True,
        allow_after_close=True,
        allow_revealed=True,
        require_allowlist=False,
        require_closed=True,
    )
    return {**report, "status": "materialized-private-postclose"}


__all__ = [
    "ALLOWED_PRECLOSE_EVALUATION_ROUND_IDS",
    "PRIVATE_EVALUATION_FORMAT_VERSION",
    "PRIVATE_EVALUATION_MEDIA_TYPE",
    "PRODUCTION_BETA_CATCHUP_ROUND_ID",
    "PrivateEvaluationError",
    "build_private_evaluation_artifact",
    "describe_private_evaluation_artifact",
    "materialize_postclose_weekly_evaluation",
    "materialize_private_preclose_evaluation",
    "materialize_private_evaluation_result",
    "recover_legacy_ligand_eligibility",
]
