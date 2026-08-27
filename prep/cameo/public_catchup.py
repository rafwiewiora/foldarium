#!/usr/bin/env python3
"""Catch the static CAMEO quiz up from public released coordinates.

Examples (run with the pipeline evaluation environment):

  python prep/cameo/public_catchup.py --start-week 2026-06-27 --end-week 2026-08-01 --scan-only
  python prep/cameo/public_catchup.py --start-week 2026-06-27 --end-week 2026-08-01
  python prep/cameo/public_catchup.py --start-week 2026-06-27 --end-week 2026-08-01 --apply

Without ``--apply``, eligible assets and ``catchup-report.json`` are written to
the staging directory only.  ``--scan-only`` downloads pages but no coordinate
files.  Source pages and coordinates are cached so an interrupted run resumes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "pipeline" / "src"))

from foldarium_pipeline.archive import (  # noqa: E402
    build_archive_candidate,
    build_official_pose_result,
    build_static_quiz_item,
    classify_pose_ensemble,
    export_static_assets,
    parse_official_ligand_score,
)
from foldarium_pipeline.cameo import (  # noqa: E402
    CAMEO_SITEMAP_URL,
    parse_sitemap_targets,
    parse_target_page,
    target_url,
    validate_coordinate_url,
)
from foldarium_pipeline.evaluation import EvaluationError, evaluate_ligand_pose  # noqa: E402
from foldarium_pipeline.weekly import USER_AGENT, fetch_public  # noqa: E402

MAX_COORDINATE_BYTES = 64 * 1024 * 1024
QUIZ_FILES = {
    "game-able": "quiz_items.json",
    "all-wrong": "quiz_items_allwrong.json",
    "all-correct": "quiz_items_allcorrect.json",
}


class CatchupError(RuntimeError):
    """Raised when a catch-up run cannot safely continue."""


def _saturdays(start: date, end: date) -> list[date]:
    if start > end or start.weekday() != 5 or end.weekday() != 5:
        raise CatchupError("start-week and end-week must be ordered Saturdays")
    weeks: list[date] = []
    current = start
    while current <= end:
        weeks.append(current)
        current += timedelta(days=7)
    return weeks


def _existing_ids(repository_root: Path) -> set[str]:
    identifiers: set[str] = set()
    for filename in QUIZ_FILES.values():
        path = repository_root / filename
        if not path.exists():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        items = value.get("items", []) if isinstance(value, dict) else value
        if not isinstance(items, list):
            raise CatchupError(f"{filename} has an invalid quiz-item shape")
        identifiers.update(str(item["id"]).upper() for item in items)
    return identifiers


def _cached_bytes(path: Path, fetcher: Callable[[], bytes]) -> bytes:
    if path.exists():
        data = path.read_bytes()
        if data:
            return data
    data = fetcher()
    if not data:
        raise CatchupError(f"download was empty: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return data


def _coordinate_bytes(url: str) -> bytes:
    validate_coordinate_url(url)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        response = urlopen(request, timeout=120)
        try:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_COORDINATE_BYTES:
                raise CatchupError("coordinate file exceeds the size ceiling")
            data = response.read(MAX_COORDINATE_BYTES + 1)
        finally:
            response.close()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise CatchupError("public CAMEO coordinate is unavailable") from exc
    if not data or len(data) > MAX_COORDINATE_BYTES:
        raise CatchupError("coordinate file is empty or exceeds the size ceiling")
    return data


def _page_payload(target_id: str, cache: Path) -> dict[str, Any]:
    raw = _cached_bytes(
        cache / "pages" / f"{target_id}.html",
        lambda: fetch_public(target_url(target_id)),
    )
    try:
        payload = parse_target_page(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CatchupError("cached CAMEO target page is invalid") from exc
    if payload["target"]["id"] != target_id:
        raise CatchupError("cached CAMEO target page has the wrong identity")
    return payload


def _discover_payloads(
    weeks: list[date], cache: Path, workers: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sitemap = _cached_bytes(
        cache / "sitemap.xml",
        lambda: fetch_public(CAMEO_SITEMAP_URL),
    )
    try:
        sitemap_text = sitemap.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatchupError("CAMEO sitemap is not UTF-8") from exc
    target_ids = [
        target_id
        for week in weeks
        for target_id in parse_sitemap_targets(sitemap_text, week)
    ]
    if not target_ids:
        raise CatchupError("CAMEO sitemap has no targets in the requested range")
    payload_by_id: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_page_payload, target_id, cache): target_id for target_id in target_ids
        }
        for future in as_completed(futures):
            target_id = futures[future]
            try:
                payload_by_id[target_id] = future.result()
            except Exception as exc:
                failures[target_id] = type(exc).__name__
    if failures:
        sample = ", ".join(f"{key}:{failures[key]}" for key in sorted(failures)[:5])
        raise CatchupError(
            f"target-page snapshot is incomplete ({len(payload_by_id)}/{len(target_ids)}; {sample})"
        )
    return [payload_by_id[target_id] for target_id in target_ids], {
        "sitemap_sha256": hashlib.sha256(sitemap).hexdigest(),
        "target_pages": len(target_ids),
        "weeks": [week.isoformat() for week in weeks],
    }


def _reference_order(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    preferred = manifest.get("preferred_reference_assembly")
    return sorted(
        manifest["references"],
        key=lambda row: (row["assembly_id"] != preferred, row["assembly_id"]),
    )


def _download_candidate_coordinates(
    candidate: dict[str, Any], cache: Path, workers: int, assembly_id: int | None = None
) -> tuple[list[tuple[dict[str, Any], Path]], dict[int, Path]]:
    manifest = candidate["coordinate_manifest"]
    target_cache = cache / "coordinates" / candidate["target_id"]
    prediction_paths: dict[int, Path] = {}

    def prediction(row: dict[str, Any]) -> tuple[int, Path]:
        index = int(row["model_index"])
        path = target_cache / f"model-{index}.cif"
        _cached_bytes(path, lambda: _coordinate_bytes(row["url"]))
        return index, path

    with ThreadPoolExecutor(max_workers=min(workers, 5)) as executor:
        futures = [executor.submit(prediction, row) for row in manifest["models"]]
        for future in as_completed(futures):
            try:
                index, path = future.result()
                prediction_paths[index] = path
            except Exception:
                continue

    references: list[tuple[dict[str, Any], Path]] = []
    reference_rows = _reference_order(manifest)
    if assembly_id is not None:
        reference_rows = [row for row in reference_rows if row["assembly_id"] == assembly_id]
    for row in reference_rows:
        compressed_path = target_cache / f"reference-{int(row['assembly_id']):02d}.cif.gz"
        try:
            compressed = _cached_bytes(
                compressed_path,
                lambda row=row: _coordinate_bytes(row["url"]),
            )
            decompressed_path = compressed_path.with_suffix("")
            _cached_bytes(decompressed_path, lambda: gzip.decompress(compressed))
            references.append((row, decompressed_path))
        except Exception:
            continue
    return references, prediction_paths


def _evaluate_candidate(
    candidate: dict[str, Any], cache: Path, workers: int
) -> tuple[Path, dict[int, Path], dict[int, dict[str, Any]]]:
    references, prediction_paths = _download_candidate_coordinates(candidate, cache, workers)
    if len(prediction_paths) < 3 or not references:
        raise CatchupError("fewer-than-three-models-or-no-reference")
    best: tuple[Path, dict[int, dict[str, Any]]] | None = None
    for _, reference_path in references:
        scored: dict[int, dict[str, Any]] = {}
        for sample, prediction_path in sorted(prediction_paths.items()):
            try:
                scored[sample] = evaluate_ligand_pose(
                    reference_path,
                    prediction_path,
                    component_id=candidate["component_id"],
                    heavy_atoms=int(candidate["heavy_atoms"]),
                )
            except (EvaluationError, OSError, ValueError):
                continue
        if best is None or len(scored) > len(best[1]):
            best = reference_path, scored
        if len(scored) == len(prediction_paths):
            break
    if best is None or len(best[1]) < 3:
        raise CatchupError("fewer-than-three-scored-models")
    return best[0], prediction_paths, best[1]


def _official_score_paths(candidate: dict[str, Any], raw_root: Path) -> dict[int, Path]:
    base = (
        raw_root
        / "modeling"
        / str(candidate["week"]).replace("-", ".")
        / candidate["pdb_id"]
        / "servers"
        / "server993"
    )
    return {
        sample: base / f"model-{sample}" / "scores" / "ligand_pose.json"
        for sample in range(1, 6)
    }


def _official_candidate(
    candidate: dict[str, Any], raw_root: Path, cache: Path, workers: int
) -> tuple[Path, dict[int, Path], dict[int, dict[str, Any]]]:
    official: dict[int, dict[str, Any]] = {}
    for sample, path in _official_score_paths(candidate, raw_root).items():
        if not path.is_file():
            continue
        try:
            score = parse_official_ligand_score(
                json.loads(path.read_text(encoding="utf-8")), candidate["component_id"]
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            score = None
        if score is not None:
            official[sample] = score
    if len(official) < 3:
        raise CatchupError("official-model-scores-unavailable")
    by_assembly = Counter(score["assembly_id"] for score in official.values())
    assembly_id, count = by_assembly.most_common(1)[0]
    official = {
        sample: score
        for sample, score in official.items()
        if score["assembly_id"] == assembly_id
    }
    if count < 3 or assembly_id < 1:
        raise CatchupError("official-model-scores-use-incompatible-assemblies")
    atom_counts = Counter(
        score["atom_count"] for score in official.values() if score.get("atom_count")
    )
    if not atom_counts:
        raise CatchupError("official-model-scores-have-no-atom-count")
    candidate["heavy_atoms"] = int(atom_counts.most_common(1)[0][0])
    references, prediction_paths = _download_candidate_coordinates(
        candidate, cache, workers, assembly_id=assembly_id
    )
    if not references:
        raise CatchupError("official-reference-assembly-unavailable")
    reference_path = references[0][1]
    scored: dict[int, dict[str, Any]] = {}
    mapping_failures: Counter[str] = Counter()
    for sample, score in official.items():
        prediction_path = prediction_paths.get(sample)
        if prediction_path is None:
            mapping_failures["prediction-coordinate-unavailable"] += 1
            continue
        try:
            scored[sample] = build_official_pose_result(
                score, reference_path, prediction_path
            )
        except (EvaluationError, OSError, ValueError) as exc:
            mapping_failures[f"{type(exc).__name__}: {exc}"] += 1
            continue
    if len(scored) < 3:
        details = "; ".join(
            f"{count}x {reason}" for reason, count in mapping_failures.most_common()
        )
        raise CatchupError(
            f"fewer-than-three-officially-mapped-models ({details or 'unknown'})"
        )
    return reference_path, prediction_paths, scored


def _apply_items(repository_root: Path, stage: Path, items: list[dict[str, Any]]) -> None:
    destinations = [(stage / "data" / item["id"], repository_root / "data" / item["id"]) for item in items]
    collisions = [str(destination) for _, destination in destinations if destination.exists()]
    if collisions:
        raise CatchupError(f"refusing to overwrite existing quiz assets: {', '.join(collisions[:5])}")
    for source, destination in destinations:
        if not source.is_dir():
            raise CatchupError(f"staged assets are missing: {source}")

    documents: dict[str, Any] = {}
    for bucket, filename in QUIZ_FILES.items():
        path = repository_root / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        rows = value.get("items", []) if isinstance(value, dict) else value
        additions = [item for item in items if item["bucket"] == bucket]
        rows = sorted([*rows, *additions], key=lambda item: (item.get("week", ""), item["id"]))
        documents[filename] = {"items": rows} if isinstance(value, dict) else rows

    for source, destination in destinations:
        shutil.copytree(source, destination)
    for filename, value in documents.items():
        path = repository_root / filename
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-week", required=True, help="inclusive Saturday, YYYY-MM-DD")
    parser.add_argument("--end-week", required=True, help="inclusive Saturday, YYYY-MM-DD")
    parser.add_argument(
        "--cache-dir", default="/private/tmp/foldarium-cameo-public-cache"
    )
    parser.add_argument(
        "--stage-dir", default="/private/tmp/foldarium-cameo-catchup-stage"
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--heavy-atom-minimum", type=int, default=15)
    parser.add_argument("--max-evaluations", type=int, default=0)
    parser.add_argument("--only-target", help="one CAMEO target ID or released PDB ID")
    parser.add_argument(
        "--raw-root",
        help="extracted official CAMEO raw archive root containing modeling/YYYY.MM.DD",
    )
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers < 1 or args.heavy_atom_minimum < 1 or args.max_evaluations < 0:
        raise CatchupError("workers/heavy-atom-minimum must be positive; max-evaluations non-negative")
    if args.scan_only and args.apply:
        raise CatchupError("--scan-only and --apply cannot be combined")
    if args.apply and not args.raw_root:
        raise CatchupError("--apply requires --raw-root with official models 1-5 scores")
    weeks = _saturdays(date.fromisoformat(args.start_week), date.fromisoformat(args.end_week))
    cache = Path(args.cache_dir).resolve()
    stage = Path(args.stage_dir).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    existing = _existing_ids(REPOSITORY_ROOT)
    payloads, snapshot = _discover_payloads(weeks, cache, args.workers)
    candidates: list[dict[str, Any]] = []
    skip_reasons: Counter[str] = Counter()
    seen_pdb_ids = set(existing)
    wanted = args.only_target.upper() if args.only_target else None
    for payload in payloads:
        candidate = build_archive_candidate(
            payload, heavy_atom_minimum=args.heavy_atom_minimum
        )
        if candidate is None:
            skip_reasons["not-protein-drug-like-released-af3"] += 1
            continue
        if wanted and wanted not in {candidate["target_id"].upper(), candidate["pdb_id"]}:
            continue
        if candidate["pdb_id"] in seen_pdb_ids:
            skip_reasons["existing-or-duplicate-pdb-id"] += 1
            continue
        seen_pdb_ids.add(candidate["pdb_id"])
        candidates.append(candidate)

    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "scan-only" if args.scan_only else "staged",
        "snapshot": snapshot,
        "existing_quiz_ids": len(existing),
        "candidates": len(candidates),
        "candidate_by_week": dict(sorted(Counter(row["week"] for row in candidates).items())),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "items": [],
        "score_source": "official-cameo-raw" if args.raw_root else "local-evaluator-preview",
    }
    if args.scan_only:
        report["candidate_manifest"] = candidates
    else:
        attempted = candidates[: args.max_evaluations or None]
        for index, candidate in enumerate(attempted, start=1):
            print(
                f"[{index}/{len(attempted)}] {candidate['week']} {candidate['pdb_id']} "
                f"{candidate['component_id']}",
                flush=True,
            )
            try:
                if args.raw_root:
                    reference, predictions, scored = _official_candidate(
                        candidate, Path(args.raw_root).resolve(), cache, args.workers
                    )
                else:
                    reference, predictions, scored = _evaluate_candidate(
                        candidate, cache, args.workers
                    )
                classification = classify_pose_ensemble(scored)
                if not classification["eligible"]:
                    skip_reasons[str(classification["reason"])] += 1
                    continue
                item = build_static_quiz_item(candidate, scored, classification)
                export_static_assets(
                    stage / "data" / item["id"], reference, predictions, scored
                )
                report["items"].append(item)
            except Exception as exc:
                skip_reasons[f"evaluation:{type(exc).__name__}:{exc}"] += 1
                print(f"  skipped: {type(exc).__name__}: {exc}", flush=True)
        report["skip_reasons"] = dict(sorted(skip_reasons.items()))
        report["bucket_counts"] = dict(
            sorted(Counter(item["bucket"] for item in report["items"]).items())
        )
        if args.apply:
            _apply_items(REPOSITORY_ROOT, stage, report["items"])
            report["mode"] = "applied"

    destination = stage / "catchup-report.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["mode"],
                "report": str(destination),
                "target_pages": snapshot["target_pages"],
                "candidates": len(candidates),
                "items": len(report["items"]),
                "buckets": report.get("bucket_counts", {}),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
