#!/usr/bin/env python3
"""Score a staged CAMEO catch-up with Foldarium's Foldseek novelty rule.

The catch-up exporter writes a receptor-only ``protein.pdb`` and a separate
``xtal_lig.pdb`` for each item.  This command consumes that real layout, resumes
from ``novelty.json``, and optionally copies completed classifications into the
stage's ``catchup-report.json``.  A remote/API failure stops the run without
recording the item, so an unavailable search can never become ``novel=true``.

Run with the evaluation extra (Gemmi + NumPy), for example::

  python prep/cameo/score_stage_novelty.py \
    --stage-dir /private/tmp/foldarium-cameo-official-full \
    --cache-dir /private/tmp/foldarium-novelty-cache \
    --update-report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "pipeline" / "src"))

from foldarium_pipeline.contracts import canonical_json, stable_id  # noqa: E402
from foldarium_pipeline.supabase import SupabaseCoordinator  # noqa: E402
from foldarium_pipeline.training_similarity import (  # noqa: E402
    NOVELTY_THRESHOLD,
    TRAINING_CUTOFF,
    atom_cloud_for_residue,
    collect_training_analogs,
    download_rcsb_structure,
    file_sha256,
    read_model,
    search_pre_cutoff,
    similarity_result,
)


class StageNoveltyError(RuntimeError):
    """Raised when a staged item cannot be classified safely."""


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageNoveltyError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise StageNoveltyError(f"{label} must be a JSON object")
    return value


def _save_object(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def score_item(
    item: dict[str, Any],
    stage: Path,
    cache: Path | None = None,
) -> dict[str, Any]:
    """Return one complete novelty record or raise without a classification."""

    item_id = str(item.get("id", "")).upper()
    if not item_id or item_id != str(item.get("id", "")):
        raise StageNoveltyError("stage item has an invalid uppercase PDB ID")
    item_dir = stage / "data" / item_id
    protein_path = item_dir / "protein.pdb"
    ligand_path = item_dir / "xtal_lig.pdb"
    if not protein_path.is_file() or not ligand_path.is_file():
        raise StageNoveltyError("staged protein or crystal ligand is missing")

    cache_directory = cache or stage / "_novelty-cache"
    ligand_model = read_model(ligand_path)
    ligand_residues = [
        residue
        for chain in ligand_model
        for residue in chain
        if residue.het_flag == "H" and not residue.is_water()
    ]
    if len(ligand_residues) != 1:
        raise StageNoveltyError(
            "xtal_lig.pdb must contain exactly one ligand residue"
        )
    query_ligand = atom_cloud_for_residue(ligand_residues[0])
    hits = search_pre_cutoff(
        protein_path,
        exclude_pdb=item_id,
        cache_directory=cache_directory,
        cache_label="cameo",
    )
    analogs, failures = collect_training_analogs(
        protein_path,
        query_ligand,
        hits,
        reference_loader=lambda pdb_id: download_rcsb_structure(
            pdb_id, cache_directory
        ),
    )
    result = similarity_result(query_ligand, analogs, failures, hits)
    if result["classification"] == "unknown":
        raise StageNoveltyError(
            "training candidate evaluation failed; novelty remains unknown"
        )
    result.update(
        {
            "item_id": item_id,
            "week": item.get("week"),
            "ligand": item.get("ligand"),
            "protein_sha256": file_sha256(protein_path),
            "xtal_ligand_sha256": file_sha256(ligand_path),
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return result


def update_report(report: dict[str, Any], results: dict[str, Any]) -> int:
    """Copy only complete boolean classifications into staged quiz items."""

    items = report.get("items")
    if not isinstance(items, list):
        raise StageNoveltyError("catchup report has no item list")
    updated = 0
    for item in items:
        if not isinstance(item, dict):
            raise StageNoveltyError("catchup report contains a non-object item")
        result = results.get(str(item.get("id", "")))
        if not isinstance(result, dict) or not isinstance(result.get("novel"), bool):
            continue
        item["novel"] = result["novel"]
        item["novelty"] = {
            key: value
            for key, value in result.items()
            if key not in {"item_id", "week", "ligand", "novel"}
        }
        updated += 1
    report["novelty_scored_items"] = updated
    report["novelty_pending_items"] = len(items) - updated
    return updated


def curation_rows(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Build immutable Supabase rows from completed novelty classifications."""

    rows: list[dict[str, Any]] = []
    for item_id, result in sorted(results.items()):
        if not isinstance(result, dict) or not isinstance(result.get("novel"), bool):
            continue
        input_digest = hashlib.sha256(
            canonical_json(
                {
                    "protein_sha256": result.get("protein_sha256"),
                    "xtal_ligand_sha256": result.get("xtal_ligand_sha256"),
                }
            ).encode("utf-8")
        ).hexdigest()
        decision = "novel" if result["novel"] else "familiar"
        overlap = result.get("train_shape_overlap")
        if decision == "novel":
            reason = (
                "no-eligible-pre-cutoff-training-ligand"
                if overlap is None
                else "training-ligand-overlap-below-0.25"
            )
        else:
            reason = "training-ligand-overlap-at-least-0.25"
        week = result.get("week")
        release_week = str(week).replace(".", "-") if week else None
        provenance = {
            "scorer_version": result.get("scorer_version"),
            "foldseek_database": result.get("foldseek_database"),
            "cutoff": result.get("cutoff", TRAINING_CUTOFF),
            "novel_threshold": result.get("novel_threshold", NOVELTY_THRESHOLD),
            "protein_sha256": result.get("protein_sha256"),
            "xtal_ligand_sha256": result.get("xtal_ligand_sha256"),
            "evaluated_at": result.get("evaluated_at"),
        }
        metrics = {
            key: result.get(key)
            for key in (
                "train_pdb",
                "train_het",
                "train_identity",
                "train_max_protein_identity",
                "train_align_rmsd",
                "train_shape_overlap",
            )
        }
        identity = {
            "source": "cameo-public-catchup",
            "stage": "foldseek-novelty",
            "target_id": item_id,
            "input_sha256": input_digest,
            "scorer_version": result.get("scorer_version"),
        }
        rows.append(
            {
                "decision_id": stable_id("curation", identity),
                "source": "cameo-public-catchup",
                "stage": "foldseek-novelty",
                "target_id": item_id,
                "campaign_id": None,
                "snapshot_id": None,
                "release_week": release_week,
                "decision": decision,
                "reason": reason,
                "input_sha256": input_digest,
                "metrics": metrics,
                "provenance": provenance,
            }
        )
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", help="comma-separated staged PDB IDs")
    parser.add_argument("--update-report", action="store_true")
    parser.add_argument(
        "--publish-supabase",
        action="store_true",
        help="record every completed novelty decision through the private Supabase RPC",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 0:
        raise StageNoveltyError("limit must be non-negative")
    stage = Path(args.stage_dir).resolve()
    cache = Path(args.cache_dir).resolve()
    report_path = stage / "catchup-report.json"
    output = Path(args.output).resolve() if args.output else stage / "novelty.json"
    report = _load_object(report_path, "catchup report")
    items = report.get("items")
    if not isinstance(items, list):
        raise StageNoveltyError("catchup report has no item list")
    results = _load_object(output, "novelty output") if output.exists() else {}
    only = {value.strip().upper() for value in args.only.split(",")} if args.only else None

    completed = 0
    for item in items:
        if not isinstance(item, dict):
            raise StageNoveltyError("catchup report contains a non-object item")
        item_id = str(item.get("id", "")).upper()
        if only is not None and item_id not in only:
            continue
        if item_id in results and only is None:
            continue
        try:
            result = score_item(item, stage, cache)
        except Exception as exc:
            print(f"{item_id}: HARD-ERROR {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            break
        results[item_id] = result
        _save_object(output, results)
        completed += 1
        print(
            f"{item_id}: train={result.get('train_pdb')} "
            f"overlap={result.get('train_shape_overlap')} novel={result['novel']}",
            flush=True,
        )
        if args.limit and completed >= args.limit:
            break

    updated = 0
    if args.update_report:
        updated = update_report(report, results)
        _save_object(report_path, report)
    published = 0
    if args.publish_supabase:
        rows = curation_rows(results)
        coordinator = SupabaseCoordinator.from_env()
        for offset in range(0, len(rows), 100):
            batch = rows[offset : offset + 100]
            if batch:
                coordinator.record_curation_decisions(batch)
                published += len(batch)
    print(
        json.dumps(
            {
                "status": "scored",
                "new_items": completed,
                "total_results": len(results),
                "report_items_updated": updated,
                "supabase_decisions_recorded": published,
                "output": str(output),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
