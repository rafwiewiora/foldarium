"""Resumable training-similarity audit for published Foldarium Weekly rounds."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .training_similarity import (
    MAX_STRUCTURE_BYTES,
    SCORER_VERSION,
    TRAINING_HIT_LIMIT,
    atom_cloud,
    collect_training_analogs,
    download_rcsb_structure,
    file_sha256,
    foldseek_cache_path,
    foldseek_cache_provenance,
    ligand_cloud,
    read_model,
    search_pre_cutoff,
    similarity_result,
)

DEFAULT_ORIGIN = "https://www.foldarium.org"
CATALOG_FORMAT = "foldarium.weekly-retrospective-list/v1"
DETAIL_FORMAT = "foldarium.weekly-retrospective-detail/v1"
AUDIT_FORMAT = "foldarium.weekly-training-similarity-audit/v1"
MAX_API_BYTES = 32 * 1024 * 1024
EXPECTED_PUBLICATION_COUNT = 3
EXPECTED_TARGET_COUNT = 100
_PDB_ID = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WeeklyTrainingAuditError(RuntimeError):
    """Raised when public Weekly audit inputs violate their contract."""


@dataclass(frozen=True)
class BlindTarget:
    """Only information that was available to a quiz player before reveal."""

    round_id: str
    blind_week: str
    item_id: str
    ligand_component_id: str
    protein_uri: str
    pocket_uri: str
    choices: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ExactTarget:
    """Released answer information used only by the retrospective scorer."""

    round_id: str
    blind_week: str
    item_id: str
    ligand_component_id: str
    reference_uri: str
    crystal_ligand_pdb: str
    has_correct_pose: bool
    correct_choice_ids: tuple[str, ...]
    automated_correct: tuple[tuple[str, bool], ...]


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
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(_json_bytes(value, pretty=True))
    temporary.replace(path)


def _safe_origin(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WeeklyTrainingAuditError("audit origin must be a credential-free HTTPS origin")
    return value.rstrip("/")


def _fetch_json(
    url: str,
    *,
    cache_path: Path,
    opener: Any = urlopen,
) -> dict[str, Any]:
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise WeeklyTrainingAuditError(f"invalid JSON cache: {cache_path}") from exc
        if not isinstance(cached, dict):
            raise WeeklyTrainingAuditError(f"JSON cache is not an object: {cache_path}")
        return cached
    request = Request(url, headers={"User-Agent": "Foldarium weekly novelty audit/1.0"})
    try:
        with opener(request, timeout=120) as response:
            body = response.read(MAX_API_BYTES + 1)
    except Exception as exc:
        raise WeeklyTrainingAuditError(f"could not fetch {url}") from exc
    if not body or len(body) > MAX_API_BYTES:
        raise WeeklyTrainingAuditError(f"response is empty or too large: {url}")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeeklyTrainingAuditError(f"response is not JSON: {url}") from exc
    if not isinstance(value, dict):
        raise WeeklyTrainingAuditError(f"response is not an object: {url}")
    _write_json(cache_path, value)
    return value


def load_publications(
    origin: str,
    cache_directory: str | Path,
    *,
    opener: Any = urlopen,
) -> list[dict[str, Any]]:
    base = _safe_origin(origin)
    cache = Path(cache_directory)
    catalog = _fetch_json(
        f"{base}/api/weekly-retrospectives?limit=50",
        cache_path=cache / "api" / "catalog.json",
        opener=opener,
    )
    if catalog.get("format_version") != CATALOG_FORMAT or not isinstance(
        catalog.get("publications"), list
    ):
        raise WeeklyTrainingAuditError("retrospective catalog is invalid")
    publications = []
    for publication in catalog["publications"]:
        if not isinstance(publication, dict):
            raise WeeklyTrainingAuditError("retrospective publication is invalid")
        round_id = publication.get("round_id")
        item_count = publication.get("item_count")
        if not isinstance(round_id, str) or not isinstance(item_count, int):
            raise WeeklyTrainingAuditError("retrospective publication identity is invalid")
        publications.append(publication)
    return publications


def load_round_detail(
    origin: str,
    round_id: str,
    cache_directory: str | Path,
    *,
    opener: Any = urlopen,
) -> dict[str, Any]:
    base = _safe_origin(origin)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", round_id):
        raise WeeklyTrainingAuditError("round ID is invalid")
    detail = _fetch_json(
        f"{base}/api/weekly-retrospectives?round_id={quote(round_id)}",
        cache_path=Path(cache_directory) / "api" / f"{round_id}.json",
        opener=opener,
    )
    if detail.get("format_version") != DETAIL_FORMAT:
        raise WeeklyTrainingAuditError(f"{round_id} detail format is invalid")
    if detail.get("round", {}).get("round_id") != round_id:
        raise WeeklyTrainingAuditError(f"{round_id} detail identity is invalid")
    return detail


def _component_id(item: dict[str, Any]) -> str:
    ligand = item.get("ligand")
    component = ligand.get("component_id") if isinstance(ligand, dict) else ligand
    if not isinstance(component, str) or not re.fullmatch(r"[A-Za-z0-9]{1,8}", component):
        raise WeeklyTrainingAuditError(f"{item.get('id')} ligand identity is invalid")
    return component.upper()


def _reference_uri(item_id: str, reveal_item: dict[str, Any]) -> str:
    choices = reveal_item.get("choices")
    if not isinstance(choices, list) or not choices:
        raise WeeklyTrainingAuditError(f"{item_id} has no reveal choices")
    references = {choice.get("reference_uri") for choice in choices if isinstance(choice, dict)}
    expected = f"https://files.rcsb.org/download/{item_id.upper()}.cif.gz"
    if references != {expected}:
        raise WeeklyTrainingAuditError(f"{item_id} reference URI is invalid")
    return expected


def targets_from_detail(detail: dict[str, Any]) -> tuple[list[BlindTarget], list[ExactTarget]]:
    round_row = detail.get("round")
    blind_manifest = detail.get("blind_manifest")
    reveal_manifest = detail.get("reveal_manifest")
    overlays = detail.get("answer_overlays")
    retrospective = detail.get("retrospective")
    if not all(
        isinstance(value, dict)
        for value in (round_row, blind_manifest, reveal_manifest, retrospective)
    ) or not isinstance(overlays, list):
        raise WeeklyTrainingAuditError("retrospective detail is incomplete")
    round_id = round_row.get("round_id")
    blind_week = round_row.get("blind_week") or str(round_row.get("opens_at", ""))[:10]
    if not isinstance(round_id, str) or not isinstance(blind_week, str):
        raise WeeklyTrainingAuditError("round metadata is invalid")
    blind_items = blind_manifest.get("items")
    reveal_items = reveal_manifest.get("items")
    questions = retrospective.get("questions")
    if not all(isinstance(value, list) for value in (blind_items, reveal_items, questions)):
        raise WeeklyTrainingAuditError("retrospective item tables are invalid")
    reveal_by_id = {item.get("id"): item for item in reveal_items if isinstance(item, dict)}
    overlay_by_id = {item.get("item_id"): item for item in overlays if isinstance(item, dict)}
    question_by_id = {item.get("item_id"): item for item in questions if isinstance(item, dict)}
    blind_targets: list[BlindTarget] = []
    exact_targets: list[ExactTarget] = []
    for item in blind_items:
        if not isinstance(item, dict):
            raise WeeklyTrainingAuditError("blind item is invalid")
        item_id = str(item.get("id", "")).upper()
        if not _PDB_ID.fullmatch(item_id):
            raise WeeklyTrainingAuditError(f"invalid Weekly PDB ID: {item_id!r}")
        choices = item.get("choices")
        if not isinstance(choices, list) or not choices:
            raise WeeklyTrainingAuditError(f"{item_id} has no blind choices")
        blind_choices: list[tuple[str, str]] = []
        for choice in choices:
            if not isinstance(choice, dict):
                raise WeeklyTrainingAuditError(f"{item_id} blind choice is invalid")
            choice_id = choice.get("id")
            pose_uri = choice.get("pose_uri")
            if not isinstance(choice_id, str) or not isinstance(pose_uri, str):
                raise WeeklyTrainingAuditError(f"{item_id} blind choice asset is invalid")
            blind_choices.append((choice_id, pose_uri))
        protein_uri = item.get("protein_uri")
        pocket_uri = item.get("pocket_uri")
        if not isinstance(protein_uri, str) or not isinstance(pocket_uri, str):
            raise WeeklyTrainingAuditError(f"{item_id} blind receptor assets are invalid")
        component_id = _component_id(item)
        blind_targets.append(
            BlindTarget(
                round_id=round_id,
                blind_week=blind_week,
                item_id=item_id,
                ligand_component_id=component_id,
                protein_uri=protein_uri,
                pocket_uri=pocket_uri,
                choices=tuple(blind_choices),
            )
        )
        reveal = reveal_by_id.get(item_id)
        overlay = overlay_by_id.get(item_id)
        question = question_by_id.get(item_id)
        if not all(isinstance(value, dict) for value in (reveal, overlay, question)):
            raise WeeklyTrainingAuditError(f"{item_id} released audit data is missing")
        reveal_choices = reveal.get("choices")
        if not isinstance(reveal_choices, list) or len(reveal_choices) != len(choices):
            raise WeeklyTrainingAuditError(f"{item_id} reveal choices are invalid")
        correct_ids = tuple(
            choice["id"]
            for choice in reveal_choices
            if isinstance(choice, dict)
            and choice.get("correct") is True
            and isinstance(choice.get("id"), str)
        )
        crystal_ligand = overlay.get("crystal_ligand_pdb")
        if not isinstance(crystal_ligand, str) or "HETATM" not in crystal_ligand:
            raise WeeklyTrainingAuditError(f"{item_id} crystal ligand overlay is invalid")
        automated_rows = question.get("automated_entries", [])
        if not isinstance(automated_rows, list):
            raise WeeklyTrainingAuditError(f"{item_id} automated outcomes are invalid")
        automated: list[tuple[str, bool]] = []
        for row in automated_rows:
            if not isinstance(row, dict) or not isinstance(row.get("participant"), str) or not isinstance(
                row.get("correct"), bool
            ):
                raise WeeklyTrainingAuditError(f"{item_id} automated outcome is invalid")
            automated.append((row["participant"], row["correct"]))
        exact_targets.append(
            ExactTarget(
                round_id=round_id,
                blind_week=blind_week,
                item_id=item_id,
                ligand_component_id=component_id,
                reference_uri=_reference_uri(item_id, reveal),
                crystal_ligand_pdb=crystal_ligand,
                has_correct_pose=bool(correct_ids),
                correct_choice_ids=correct_ids,
                automated_correct=tuple(automated),
            )
        )
    if len(blind_targets) != int(round_row.get("item_count", len(blind_targets))):
        raise WeeklyTrainingAuditError(f"{round_id} item count is inconsistent")
    return blind_targets, exact_targets


def load_all_targets(
    origin: str,
    cache_directory: str | Path,
) -> tuple[list[BlindTarget], list[ExactTarget]]:
    blind: list[BlindTarget] = []
    exact: list[ExactTarget] = []
    publications = load_publications(origin, cache_directory)
    if len(publications) != EXPECTED_PUBLICATION_COUNT:
        raise WeeklyTrainingAuditError(
            f"expected {EXPECTED_PUBLICATION_COUNT} published rounds, "
            f"found {len(publications)}"
        )
    for publication in publications:
        round_id = publication["round_id"]
        detail = load_round_detail(origin, round_id, cache_directory)
        round_blind, round_exact = targets_from_detail(detail)
        if len(round_blind) != publication["item_count"]:
            raise WeeklyTrainingAuditError(f"{round_id} catalog count is inconsistent")
        blind.extend(round_blind)
        exact.extend(round_exact)
    identities = [target.item_id for target in blind]
    if len(identities) != EXPECTED_TARGET_COUNT or len(exact) != EXPECTED_TARGET_COUNT:
        raise WeeklyTrainingAuditError(
            f"expected {EXPECTED_TARGET_COUNT} published Weekly targets"
        )
    if len(identities) != len(set(identities)):
        raise WeeklyTrainingAuditError("published Weekly targets are not unique")
    return blind, exact


def _asset_digest(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".supabase.co")
        or "/storage/v1/object/public/" not in parsed.path
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WeeklyTrainingAuditError("blind asset URL is not a public Supabase object")
    digest = parsed.path.rsplit("/", 1)[-1]
    if not _SHA256.fullmatch(digest):
        raise WeeklyTrainingAuditError("blind asset URL is not content-addressed")
    return digest


def download_blind_asset(
    url: str,
    cache_directory: str | Path,
    *,
    opener: Any = urlopen,
) -> Path:
    expected = _asset_digest(url)
    destination = Path(cache_directory) / "blind-assets" / f"{expected}.pdb"
    if destination.is_file() and file_sha256(destination) == expected:
        return destination
    request = Request(url, headers={"User-Agent": "Foldarium weekly novelty audit/1.0"})
    try:
        with opener(request, timeout=120) as response:
            body = response.read(MAX_STRUCTURE_BYTES + 1)
    except Exception as exc:
        raise WeeklyTrainingAuditError("could not download blind asset") from exc
    if not body or len(body) > MAX_STRUCTURE_BYTES or sha256(body).hexdigest() != expected:
        raise WeeklyTrainingAuditError("blind asset failed content-address verification")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=destination.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(body)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _crystal_ligand_path(target: ExactTarget, cache_directory: Path) -> Path:
    digest = sha256(target.crystal_ligand_pdb.encode("utf-8")).hexdigest()
    destination = cache_directory / "crystal-ligands" / f"{target.item_id}-{digest[:16]}.pdb"
    if not destination.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(target.crystal_ligand_pdb)
    return destination


def _reference_loader(cache_directory: Path) -> Any:
    return lambda pdb_id: download_rcsb_structure(pdb_id, cache_directory)


def score_exact_target(target: ExactTarget, cache_directory: str | Path) -> dict[str, Any]:
    cache = Path(cache_directory)
    query = download_rcsb_structure(target.item_id, cache)
    ligand_path = _crystal_ligand_path(target, cache)
    query_ligand = ligand_cloud(ligand_path)
    hits = search_pre_cutoff(
        query,
        exclude_pdb=target.item_id,
        cache_directory=cache,
        cache_label="exact",
    )
    analogs, failures = collect_training_analogs(
        query,
        query_ligand,
        hits,
        reference_loader=_reference_loader(cache),
    )
    result = similarity_result(query_ligand, analogs, failures, hits)
    hit_cache = foldseek_cache_path(
        query,
        exclude_pdb=target.item_id,
        cache_directory=cache,
        cache_label="exact",
    )
    result.update(
        {
            "mode": "exact",
            "round_id": target.round_id,
            "blind_week": target.blind_week,
            "item_id": target.item_id,
            "ligand_component_id": target.ligand_component_id,
            "query_structure_sha256": file_sha256(query),
            "query_ligand_sha256": file_sha256(ligand_path),
            "foldseek_hits_sha256": file_sha256(hit_cache),
            "has_correct_pose": target.has_correct_pose,
            "correct_choice_ids": list(target.correct_choice_ids),
            "automated_correct": dict(target.automated_correct),
            **foldseek_cache_provenance(hit_cache),
        }
    )
    return result


def score_blind_target(target: BlindTarget, cache_directory: str | Path) -> dict[str, Any]:
    """Score one target without accepting any reveal-side object."""

    cache = Path(cache_directory)
    query = download_blind_asset(target.protein_uri, cache)
    pocket_path = download_blind_asset(target.pocket_uri, cache)
    pocket = atom_cloud(read_model(pocket_path))
    hits = search_pre_cutoff(
        query,
        exclude_pdb=target.item_id,
        cache_directory=cache,
        cache_label="blind",
    )
    analogs, failures = collect_training_analogs(
        query,
        pocket,
        hits,
        reference_loader=_reference_loader(cache),
    )
    nearest_ligand_bearing_rank = min(
        (analog.hit_rank for analog in analogs),
        default=min(len(hits), TRAINING_HIT_LIMIT) or 1,
    )
    choices: list[dict[str, Any]] = []
    for choice_id, pose_uri in target.choices:
        pose_path = download_blind_asset(pose_uri, cache)
        pose = ligand_cloud(pose_path)
        nearest = similarity_result(
            pose,
            analogs,
            failures,
            hits,
            maximum_hit_rank=nearest_ligand_bearing_rank,
        )
        pocket_aware = similarity_result(
            pose, analogs, failures, hits, maximum_hit_rank=TRAINING_HIT_LIMIT
        )
        choices.append(
            {
                "choice_id": choice_id,
                "pose_sha256": file_sha256(pose_path),
                "nearest_training_system": nearest,
                "pocket_aware": pocket_aware,
            }
        )

    def best_choice(method: str) -> tuple[dict[str, Any] | None, float | None]:
        rows = [
            row
            for row in choices
            if isinstance(row[method].get("train_shape_overlap"), (int, float))
        ]
        if not rows:
            return None, None
        best = max(
            rows,
            key=lambda row: (
                row[method]["train_shape_overlap"],
                row["choice_id"],
            ),
        )
        return best, float(best[method]["train_shape_overlap"])

    hit_cache = foldseek_cache_path(
        query,
        exclude_pdb=target.item_id,
        cache_directory=cache,
        cache_label="blind",
    )
    output: dict[str, Any] = {
        "mode": "blind",
        "round_id": target.round_id,
        "blind_week": target.blind_week,
        "item_id": target.item_id,
        "ligand_component_id": target.ligand_component_id,
        "query_structure_sha256": file_sha256(query),
        "query_pocket_sha256": file_sha256(pocket_path),
        "foldseek_hits_sha256": file_sha256(hit_cache),
        "nearest_ligand_bearing_hit_rank": nearest_ligand_bearing_rank,
        "choices": choices,
        "scorer_version": SCORER_VERSION,
        **foldseek_cache_provenance(hit_cache),
    }
    for method in ("nearest_training_system", "pocket_aware"):
        best, score = best_choice(method)
        output[method] = {
            "choice_id": best["choice_id"] if best else None,
            "score": score,
            "classification": (
                best[method]["classification"] if best else "unknown"
            ),
            "predict_none": (
                None if score is None else score < 0.25
            ),
        }
    return output


def _empty_audit(mode: str) -> dict[str, Any]:
    return {
        "format_version": AUDIT_FORMAT,
        "mode": mode,
        "scorer_version": SCORER_VERSION,
        "generated_at": None,
        "records": [],
    }


def _load_audit(path: Path, mode: str) -> dict[str, Any]:
    if not path.is_file():
        return _empty_audit(mode)
    try:
        audit = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WeeklyTrainingAuditError(f"audit output is invalid: {path}") from exc
    if (
        not isinstance(audit, dict)
        or audit.get("format_version") != AUDIT_FORMAT
        or audit.get("mode") != mode
        or not isinstance(audit.get("records"), list)
    ):
        raise WeeklyTrainingAuditError(f"audit output contract is invalid: {path}")
    return audit


def run_audit(
    *,
    origin: str,
    cache_directory: str | Path,
    output: str | Path,
    mode: str,
    limit: int | None = None,
    only: set[str] | None = None,
    force: bool = False,
    workers: int = 1,
    reverse: bool = False,
) -> dict[str, Any]:
    if mode not in {"exact", "blind"}:
        raise WeeklyTrainingAuditError("audit mode must be exact or blind")
    blind_targets, exact_targets = load_all_targets(origin, cache_directory)
    targets: list[BlindTarget | ExactTarget] = (
        exact_targets if mode == "exact" else blind_targets
    )
    selected = [
        target
        for target in targets
        if not only or target.item_id in only
    ]
    if limit is not None:
        selected = selected[:limit]
    if reverse:
        selected.reverse()
    if workers < 1 or workers > 8:
        raise WeeklyTrainingAuditError("audit workers must be between 1 and 8")
    output_path = Path(output)
    audit = _load_audit(output_path, mode)
    records = {
        record["item_id"]: record
        for record in audit["records"]
        if isinstance(record, dict) and isinstance(record.get("item_id"), str)
    }
    pending: list[BlindTarget | ExactTarget] = []
    for index, target in enumerate(selected, 1):
        previous = records.get(target.item_id)
        if (
            previous
            and not force
            and previous.get("scorer_version") == SCORER_VERSION
            and previous.get("status") == "complete"
        ):
            print(f"[{index}/{len(selected)}] {target.item_id}: cached", flush=True)
            continue
        pending.append(target)

    def score(target: BlindTarget | ExactTarget) -> dict[str, Any]:
        try:
            result = (
                score_exact_target(target, cache_directory)
                if isinstance(target, ExactTarget)
                else score_blind_target(target, cache_directory)
            )
            result["status"] = "complete"
        except Exception as exc:
            result = {
                "mode": mode,
                "round_id": target.round_id,
                "blind_week": target.blind_week,
                "item_id": target.item_id,
                "status": "unknown",
                "classification": "unknown",
                "reason": type(exc).__name__,
                "error": str(exc)[:300],
                "scorer_version": SCORER_VERSION,
            }
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_targets = {}
        for target in pending:
            print(f"{target.item_id}: scoring {mode}", flush=True)
            future_targets[executor.submit(score, target)] = target
        for completed, future in enumerate(as_completed(future_targets), 1):
            target = future_targets[future]
            result = future.result()
            records[target.item_id] = result
            audit["records"] = sorted(
                records.values(), key=lambda row: (row["blind_week"], row["item_id"])
            )
            audit["generated_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(output_path, audit)
            print(
                f"[{completed}/{len(pending)}] {target.item_id}: "
                f"{result.get('status')} {result.get('classification', '')}",
                flush=True,
            )
    return audit


def summarize_audits(
    exact: dict[str, Any],
    blind: dict[str, Any],
) -> dict[str, Any]:
    exact_by_id = {
        row["item_id"]: row
        for row in exact.get("records", [])
        if row.get("status") == "complete"
    }
    blind_by_id = {
        row["item_id"]: row
        for row in blind.get("records", [])
        if row.get("status") == "complete"
    }
    all_exact = list(exact_by_id.values())
    summary: dict[str, Any] = {
        "target_count": len(exact.get("records", [])),
        "complete_exact_count": len(exact_by_id),
        "complete_blind_count": len(blind_by_id),
        "exact_classification": dict(
            sorted(Counter(row.get("classification", "unknown") for row in all_exact).items())
        ),
        "weeks": {},
        "blind_estimators": {},
    }
    for week in sorted({row["blind_week"] for row in exact.get("records", [])}):
        rows = [row for row in all_exact if row["blind_week"] == week]
        summary["weeks"][week] = {
            "count": len(rows),
            "classification": dict(
                sorted(Counter(row.get("classification", "unknown") for row in rows).items())
            ),
        }
    for method in ("nearest_training_system", "pocket_aware"):
        pairs = [
            (exact_by_id[item_id], blind_by_id[item_id])
            for item_id in sorted(exact_by_id.keys() & blind_by_id.keys())
            if isinstance(blind_by_id[item_id].get(method), dict)
        ]
        classification_pairs = [
            (
                exact_row.get("classification"),
                blind_row[method].get("classification"),
            )
            for exact_row, blind_row in pairs
        ]
        comparable = [
            pair
            for pair in classification_pairs
            if pair[0] in {"novel", "familiar"} and pair[1] in {"novel", "familiar"}
        ]
        selected = [
            (exact_row, blind_row[method])
            for exact_row, blind_row in pairs
            if blind_row[method].get("choice_id")
        ]
        correct_pose_pick = sum(
            blind_method["choice_id"] in set(exact_row.get("correct_choice_ids", []))
            for exact_row, blind_method in selected
        )
        none_rows = [
            (exact_row, blind_method)
            for exact_row, blind_method in selected
            if isinstance(blind_method.get("predict_none"), bool)
        ]
        none_correct = sum(
            blind_method["predict_none"] is (not exact_row.get("has_correct_pose", False))
            for exact_row, blind_method in none_rows
        )
        summary["blind_estimators"][method] = {
            "paired_count": len(pairs),
            "comparable_classification_count": len(comparable),
            "classification_accuracy": (
                round(sum(left == right for left, right in comparable) / len(comparable), 4)
                if comparable
                else None
            ),
            "selected_pose_count": len(selected),
            "exact_correct_pose_pick_count": correct_pose_pick,
            "exact_correct_pose_pick_rate": (
                round(correct_pose_pick / len(selected), 4) if selected else None
            ),
            "pose_or_none_count": len(none_rows),
            "pose_or_none_correct_count": none_correct,
            "pose_or_none_accuracy": (
                round(none_correct / len(none_rows), 4) if none_rows else None
            ),
        }
    return summary


def _parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("exact", "blind"), required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--reverse", action="store_true")
    options = parser.parse_args(arguments)
    if options.limit is not None and options.limit <= 0:
        parser.error("--limit must be positive")
    return options


def main(arguments: list[str] | None = None) -> int:
    options = _parse_args(arguments)
    only = {
        value.strip().upper()
        for value in options.only.split(",")
        if value.strip()
    }
    audit = run_audit(
        origin=options.origin,
        cache_directory=options.cache_dir,
        output=options.output,
        mode=options.mode,
        limit=options.limit,
        only=only or None,
        force=options.force,
        workers=options.workers,
        reverse=options.reverse,
    )
    status = Counter(
        row.get("status", "unknown") for row in audit.get("records", [])
    )
    print(json.dumps({"mode": options.mode, "status": dict(status)}, sort_keys=True))
    return 0 if status.get("unknown", 0) == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AUDIT_FORMAT",
    "BlindTarget",
    "ExactTarget",
    "WeeklyTrainingAuditError",
    "download_blind_asset",
    "load_all_targets",
    "load_publications",
    "load_round_detail",
    "main",
    "run_audit",
    "score_blind_target",
    "score_exact_target",
    "summarize_audits",
    "targets_from_detail",
]
