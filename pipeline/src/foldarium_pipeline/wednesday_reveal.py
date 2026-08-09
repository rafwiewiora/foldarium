"""Fail-closed Wednesday evaluation and weekly-quiz reveal orchestration.

Saturday's public assets intentionally hide released-coordinate answers and scores;
co-folding method and ligand confidence may be shown during voting.
Wednesday evaluation instead resolves the original predicted complex from each
private ``(run_id, sample_id)`` identity, then scores it against the released
PDB coordinates.  All network and database access is injected except for the
small allow-listed RCSB reference downloader, keeping this service usable from
Modal, another scheduler, and deterministic tests.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import canonical_json, stable_id
from .evaluation import EVALUATOR_VERSION, evaluate_ligand_pose
from .quiz import QUIZ_SCHEMA_VERSION, build_reveal_manifest, manifest_sha256

RCSB_DOWNLOAD_ORIGIN = "https://files.rcsb.org/download"
REVEAL_POLICY_VERSION = "foldarium-weekly-reveal/v1"
ACCEPTANCE_POLICY_VERSION = "foldarium-weekly-cluster-any-member/v1"
# This is the strict correctness boundary rendered by the current Foldarium
# viewer.  A value exactly at the boundary is not correct.
CORRECT_RMSD_ANGSTROM = 1.5
MAX_COMPRESSED_COORDINATE_BYTES = 64 * 1024 * 1024
MAX_COORDINATE_BYTES = 256 * 1024 * 1024
USER_AGENT = "Foldarium weekly reveal/0.1 (released PDB coordinate evaluation)"

CoordinateResolver = Callable[[Mapping[str, Any]], Mapping[str, Any]]
PoseEvaluator = Callable[..., Mapping[str, Any]]
RevealPublisher = Callable[..., Any]

_PDB_ID = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WednesdayRevealError(RuntimeError):
    """Raised when reveal inputs are unsafe or scientific scoring is incomplete."""


class WednesdayRevealNotReady(WednesdayRevealError):
    """Raised for a retryable pre-close or not-yet-released coordinate state."""


def rcsb_reference_url(pdb_id: str) -> str:
    """Return the canonical released-entry mmCIF URL for one classic PDB ID."""

    if not isinstance(pdb_id, str) or not _PDB_ID.fullmatch(pdb_id):
        raise WednesdayRevealError("reference target_id is not a classic four-character PDB ID")
    return f"{RCSB_DOWNLOAD_ORIGIN}/{pdb_id.upper()}.cif.gz"


def _read_bounded(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise WednesdayRevealNotReady("released reference is empty or exceeds the size limit")
    return data


def fetch_rcsb_released_reference(
    item: Mapping[str, Any],
    *,
    opener: Callable[..., Any] = urlopen,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Fetch one released PDB entry from an allow-listed RCSB download URL.

    A prerelease target that is delayed or withdrawn normally returns 404 on
    Wednesday.  That is reported as ``WednesdayRevealNotReady`` so the caller
    can retry without publishing a partial reveal.
    """

    if not isinstance(item, Mapping):
        raise WednesdayRevealError("private quiz item must be an object")
    target_id = item.get("target_id")
    url = rcsb_reference_url(target_id)
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/gzip,chemical/x-mmcif"},
    )
    try:
        response = opener(request, timeout=timeout_seconds)
        try:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url != url:
                raise WednesdayRevealNotReady("released reference redirected off its canonical URL")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_COMPRESSED_COORDINATE_BYTES:
                raise WednesdayRevealNotReady("released reference exceeds the compressed size limit")
            content = _read_bounded(response, MAX_COMPRESSED_COORDINATE_BYTES)
        finally:
            response.close()
    except WednesdayRevealNotReady:
        raise
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise WednesdayRevealNotReady(f"released reference is unavailable for {target_id}") from exc
    return {
        "content": content,
        "source_uri": url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "application/gzip",
        "pdb_id": str(target_id).upper(),
    }


def _aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise WednesdayRevealError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WednesdayRevealError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise WednesdayRevealError(f"{field} must include a timezone")
    return parsed


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WednesdayRevealError(f"{field} must be a finite non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise WednesdayRevealError(f"{field} must be a finite non-negative number")
    return normalized


def _private_index(round_record: Mapping[str, Any], content: bytes) -> dict[str, Any]:
    if not isinstance(round_record, Mapping):
        raise WednesdayRevealError("weekly round record must be an object")
    metadata = round_record.get("metadata")
    artifact = metadata.get("private_index") if isinstance(metadata, Mapping) else None
    expected = artifact.get("sha256") if isinstance(artifact, Mapping) else None
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise WednesdayRevealError("round metadata has no valid private-index digest")
    if not isinstance(content, bytes) or not content:
        raise WednesdayRevealError("private-index download returned no bytes")
    if hashlib.sha256(content).hexdigest() != expected:
        raise WednesdayRevealError("private-index content does not match its recorded digest")
    try:
        decoded = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WednesdayRevealError("private index is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, Mapping):
        raise WednesdayRevealError("private index must be an object")
    return deepcopy(dict(decoded))


def _validated_round(
    round_record: Mapping[str, Any], private: Mapping[str, Any]
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(round_record, Mapping):
        raise WednesdayRevealError("weekly round record must be an object")
    round_id = round_record.get("round_id")
    blind = round_record.get("blind_manifest")
    if not isinstance(round_id, str) or not round_id or not isinstance(blind, Mapping):
        raise WednesdayRevealError("weekly round identity or blind manifest is missing")
    blind = deepcopy(dict(blind))
    digest = manifest_sha256(blind)
    if round_record.get("blind_manifest_sha256") != digest:
        raise WednesdayRevealError("blind manifest does not match the round digest")
    if (
        blind.get("schema_version") != QUIZ_SCHEMA_VERSION
        or blind.get("round_id") != round_id
        or not isinstance(blind.get("items"), list)
        or not blind["items"]
    ):
        raise WednesdayRevealError("weekly blind manifest has an invalid shape")
    if (
        private.get("schema_version") != QUIZ_SCHEMA_VERSION
        or private.get("round_id") != round_id
        or private.get("blind_manifest_sha256") != digest
        or not isinstance(private.get("items"), list)
    ):
        raise WednesdayRevealError("private index does not match the weekly blind round")

    blind_items: dict[str, set[str]] = {}
    for raw_item in blind["items"]:
        if not isinstance(raw_item, Mapping) or not isinstance(raw_item.get("choices"), list):
            raise WednesdayRevealError("weekly blind item has an invalid shape")
        item_id = raw_item.get("id")
        ids = [choice.get("id") for choice in raw_item["choices"] if isinstance(choice, Mapping)]
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(ids) != len(raw_item["choices"])
            or any(not isinstance(value, str) or not value for value in ids)
            or len(ids) != len(set(ids))
            or item_id in blind_items
        ):
            raise WednesdayRevealError("weekly blind item/choice IDs are invalid or duplicated")
        blind_items[item_id] = set(ids)

    private_items: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for raw_item in private["items"]:
        if not isinstance(raw_item, Mapping):
            raise WednesdayRevealError("private index item must be an object")
        item = deepcopy(dict(raw_item))
        item_id = item.get("id")
        target_id = item.get("target_id")
        ligand = item.get("ligand")
        choices = item.get("choices")
        if (
            not isinstance(item_id, str)
            or item_id not in blind_items
            or item_id in seen_items
            or not isinstance(target_id, str)
            or not target_id
            or not isinstance(ligand, Mapping)
            or not isinstance(choices, list)
            or not choices
        ):
            raise WednesdayRevealError("private index item does not match the blind manifest")
        component = ligand.get("component_id")
        heavy_atoms = ligand.get("heavy_atoms")
        if (
            not isinstance(component, str)
            or not component
            or isinstance(heavy_atoms, bool)
            or not isinstance(heavy_atoms, int)
            or heavy_atoms < 1
        ):
            raise WednesdayRevealError("private index selected ligand is invalid")

        normalized_choices: list[dict[str, Any]] = []
        seen_choices: set[str] = set()
        for raw_choice in choices:
            if not isinstance(raw_choice, Mapping):
                raise WednesdayRevealError("private index choice must be an object")
            choice = deepcopy(dict(raw_choice))
            choice_id = choice.get("id")
            run_id = choice.get("run_id")
            sample_id = choice.get("sample_id")
            method = choice.get("method")
            method_version = choice.get("method_version")
            required_strings = (choice_id, run_id, sample_id, method, method_version)
            if any(not isinstance(value, str) or not value for value in required_strings):
                raise WednesdayRevealError("private choice identity is incomplete")
            expected_id = stable_id(
                "choice",
                {
                    "round_id": round_id,
                    "item_id": item_id,
                    "run_id": run_id,
                    "sample_id": sample_id,
                },
                length=16,
            )
            if (
                choice_id != expected_id
                or choice_id not in blind_items[item_id]
                or choice_id in seen_choices
            ):
                raise WednesdayRevealError("private choice identity does not match its blind choice")
            seen_choices.add(choice_id)
            normalized_choices.append(choice)
        if seen_choices != blind_items[item_id]:
            raise WednesdayRevealError("private choices are incomplete for a blind item")
        item["ligand"] = dict(ligand)
        item["choices"] = normalized_choices
        private_items.append(item)
        seen_items.add(item_id)
    if seen_items != set(blind_items):
        raise WednesdayRevealError("private item IDs do not match the blind manifest")
    private_items.sort(key=lambda item: item["id"])
    return round_id, blind, private_items


def _coordinate_artifact(
    raw: Mapping[str, Any], field: str, *, require_recorded_digest: bool
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise WednesdayRevealError(f"{field} resolver must return an object")
    content = raw.get("content")
    uri = raw.get("source_uri", raw.get("object_uri", raw.get("uri")))
    media_type = raw.get("media_type") or "chemical/x-mmcif"
    expected = raw.get("sha256")
    if not isinstance(content, bytes) or not content:
        raise WednesdayRevealError(f"{field} coordinate content is empty")
    if len(content) > MAX_COMPRESSED_COORDINATE_BYTES:
        raise WednesdayRevealError(f"{field} coordinate content exceeds the size limit")
    if not isinstance(uri, str) or not uri or not isinstance(media_type, str) or not media_type:
        raise WednesdayRevealError(f"{field} coordinate provenance is incomplete")
    if require_recorded_digest and (not isinstance(expected, str) or not _SHA256.fullmatch(expected)):
        raise WednesdayRevealError(f"{field} coordinate has no recorded SHA-256")
    digest = hashlib.sha256(content).hexdigest()
    if expected is not None and (not isinstance(expected, str) or expected != digest):
        raise WednesdayRevealError(f"{field} coordinate does not match its recorded digest")
    return {
        **deepcopy(dict(raw)),
        "content": content,
        "source_uri": uri,
        "media_type": media_type,
        "sha256": digest,
    }


def _decompress_coordinate(content: bytes, field: str) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
            decoded = stream.read(MAX_COORDINATE_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise WednesdayRevealError(f"{field} coordinate gzip stream is invalid") from exc
    if not decoded or len(decoded) > MAX_COORDINATE_BYTES:
        raise WednesdayRevealError(f"{field} coordinate is empty or exceeds the expanded size limit")
    return decoded


def _materialize_coordinate(root: Path, name: str, artifact: Mapping[str, Any], field: str) -> Path:
    content = artifact["content"]
    uri = str(artifact["source_uri"]).lower()
    media_type = str(artifact["media_type"]).lower()
    compressed = content.startswith(b"\x1f\x8b") or uri.endswith(".gz") or "gzip" in media_type
    if compressed:
        content = _decompress_coordinate(content, field)
    underlying_uri = uri[:-3] if uri.endswith(".gz") else uri
    suffix = ".pdb" if "pdb" in media_type or underlying_uri.endswith(".pdb") else ".cif"
    path = root / f"{name}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _evaluation_fields(score: Mapping[str, Any]) -> dict[str, Any]:
    evaluator_version = score.get("evaluator_version", EVALUATOR_VERSION)
    if not isinstance(evaluator_version, str) or not evaluator_version:
        raise WednesdayRevealError("evaluator result has no evaluator_version")
    result: dict[str, Any] = {"evaluator_version": evaluator_version}
    for field in ("receptor_rmsd", "sequence_similarity"):
        if field in score:
            result[field] = _finite_number(score[field], f"evaluator result {field}")
    for field in (
        "reference_receptor_chain",
        "predicted_receptor_chain",
        "reference_ligand_chain",
        "reference_ligand_residue",
        "predicted_ligand_chain",
        "predicted_ligand_residue",
    ):
        if isinstance(score.get(field), str) and score[field]:
            result[field] = score[field]
    return result


def run_wednesday_reveal(
    round_record: Mapping[str, Any],
    private_index_content: bytes,
    destination: str | Path,
    *,
    prediction_resolver: CoordinateResolver,
    reference_resolver: CoordinateResolver = fetch_rcsb_released_reference,
    evaluator: PoseEvaluator = evaluate_ligand_pose,
    reveal_publisher: RevealPublisher | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate every blind choice and optionally publish an atomic reveal.

    ``prediction_resolver`` must resolve the original complex artifact for the
    supplied private choice, not its public ligand-only ``pose_uri``.  Supplying
    ``reveal_publisher`` is the explicit mutation gate; omitting it performs the
    full deterministic evaluation without changing Supabase.
    """

    private = _private_index(round_record, private_index_content)
    round_id, blind, items = _validated_round(round_record, private)
    status = round_record.get("status")
    if status == "revealed":
        existing = round_record.get("reveal_manifest")
        if not isinstance(existing, Mapping):
            raise WednesdayRevealError("revealed round has no reveal manifest")
        existing = deepcopy(dict(existing))
        if (
            existing.get("blind_manifest_sha256") != manifest_sha256(blind)
            or round_record.get("reveal_manifest_sha256") != manifest_sha256(existing)
        ):
            raise WednesdayRevealError("existing reveal does not match the blind manifest")
        try:
            rebuilt = build_reveal_manifest(blind, existing.get("items", []))
        except Exception as exc:
            raise WednesdayRevealError("existing reveal manifest is invalid") from exc
        if rebuilt != existing:
            raise WednesdayRevealError("existing reveal manifest is not canonical")
        return {
            "status": "already-revealed",
            "round_id": round_id,
            "item_count": len(existing.get("items", [])),
            "choice_count": sum(
                len(item.get("choices", []))
                for item in existing.get("items", [])
                if isinstance(item, Mapping)
            ),
            "reveal_manifest": existing,
            "publish_response": None,
        }
    if status != "open":
        raise WednesdayRevealError("weekly round must be open before reveal")
    closes_at = _aware_datetime(round_record.get("closes_at"), "closes_at")
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise WednesdayRevealError("now must be a timezone-aware datetime")
    if current < closes_at:
        raise WednesdayRevealNotReady("weekly voting has not closed yet")

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    scored_items: list[dict[str, Any]] = []
    reference_rows: list[dict[str, Any]] = []
    for item in items:
        try:
            raw_reference = reference_resolver(deepcopy(item))
        except WednesdayRevealError:
            raise
        except Exception as exc:
            raise WednesdayRevealNotReady(
                f"released reference could not be resolved for {item['target_id']}"
            ) from exc
        reference = _coordinate_artifact(
            raw_reference, f"reference {item['target_id']}", require_recorded_digest=False
        )
        item_key = hashlib.sha256(item["id"].encode("utf-8")).hexdigest()[:20]
        reference_path = _materialize_coordinate(
            root, f"references/{item_key}", reference, f"reference {item['target_id']}"
        )
        reference_rows.append(
            {
                "item_id": item["id"],
                "target_id": item["target_id"],
                "source_uri": reference["source_uri"],
                "sha256": reference["sha256"],
            }
        )
        ligand = item["ligand"]
        scored_choices: list[dict[str, Any]] = []
        for choice in sorted(item["choices"], key=lambda value: value["id"]):
            try:
                raw_prediction = prediction_resolver(deepcopy(choice))
            except Exception as exc:
                raise WednesdayRevealError(
                    f"original predicted complex could not be resolved for {choice['id']}"
                ) from exc
            prediction = _coordinate_artifact(
                raw_prediction, f"prediction {choice['id']}", require_recorded_digest=True
            )
            prediction_path = _materialize_coordinate(
                root,
                f"predictions/{choice['id']}",
                prediction,
                f"prediction {choice['id']}",
            )
            try:
                raw_score = evaluator(
                    reference_path,
                    prediction_path,
                    component_id=ligand["component_id"],
                    heavy_atoms=ligand["heavy_atoms"],
                )
            except Exception as exc:
                raise WednesdayRevealError(
                    f"released-coordinate evaluation failed for {item['id']}/{choice['id']}"
                ) from exc
            if not isinstance(raw_score, Mapping):
                raise WednesdayRevealError("evaluator must return an object")
            score = deepcopy(dict(raw_score))
            rmsd = _finite_number(score.get("rmsd"), "evaluator result rmsd")
            scored_choices.append(
                {
                    "id": choice["id"],
                    "rmsd": rmsd,
                    "correct": rmsd < CORRECT_RMSD_ANGSTROM,
                    "method": choice["method"],
                    "method_version": choice["method_version"],
                    "run_id": choice["run_id"],
                    "sample_id": choice["sample_id"],
                    "reference_uri": reference["source_uri"],
                    "reference_sha256": reference["sha256"],
                    "prediction_sha256": prediction["sha256"],
                    "correct_rmsd_threshold_angstrom": CORRECT_RMSD_ANGSTROM,
                    "reveal_policy_version": REVEAL_POLICY_VERSION,
                    **_evaluation_fields(score),
                }
            )
        raw_correct_by_id = {
            choice["id"]: bool(choice["correct"])
            for choice in scored_choices
        }
        cluster_by_id = {
            choice["id"]: choice.get("cluster_id")
            for choice in item["choices"]
        }
        accepted_by_cluster: dict[str, bool] = {}
        for choice_id, cluster_id in cluster_by_id.items():
            if isinstance(cluster_id, str) and cluster_id:
                accepted_by_cluster[cluster_id] = (
                    accepted_by_cluster.get(cluster_id, False)
                    or raw_correct_by_id[choice_id]
                )
        for choice in scored_choices:
            cluster_id = cluster_by_id.get(choice["id"])
            choice["accepted_correct"] = (
                accepted_by_cluster[cluster_id]
                if isinstance(cluster_id, str) and cluster_id
                else bool(choice["correct"])
            )
            choice["acceptance_policy_version"] = ACCEPTANCE_POLICY_VERSION
        scored_items.append({"id": item["id"], "choices": scored_choices})

    reveal = build_reveal_manifest(blind, scored_items)
    # Check serializability before the only externally mutating call.
    try:
        canonical_json(reveal)
    except (TypeError, ValueError) as exc:
        raise WednesdayRevealError("reveal manifest is not finite JSON") from exc
    response: Any = None
    status_text = "evaluated-not-revealed"
    if reveal_publisher is not None:
        response = reveal_publisher(round_id=round_id, reveal_manifest=reveal)
        status_text = "revealed"
    return {
        "status": status_text,
        "round_id": round_id,
        "item_count": len(reveal["items"]),
        "choice_count": sum(len(item["choices"]) for item in reveal["items"]),
        "reveal_manifest_sha256": manifest_sha256(reveal),
        "reveal_manifest": reveal,
        "references": reference_rows,
        "publish_response": response,
    }


__all__ = [
    "CORRECT_RMSD_ANGSTROM",
    "MAX_COMPRESSED_COORDINATE_BYTES",
    "MAX_COORDINATE_BYTES",
    "RCSB_DOWNLOAD_ORIGIN",
    "REVEAL_POLICY_VERSION",
    "WednesdayRevealError",
    "WednesdayRevealNotReady",
    "fetch_rcsb_released_reference",
    "rcsb_reference_url",
    "run_wednesday_reveal",
]
