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
sys.path.insert(0, str(REPOSITORY_ROOT / "benchmark" / "prep"))

import numpy as np  # noqa: E402

import build_training_similarity as bts  # noqa: E402
from foldarium_pipeline.contracts import canonical_json, stable_id  # noqa: E402
from foldarium_pipeline.supabase import SupabaseCoordinator  # noqa: E402

CUTOFF = "2021-09-30"
NOVEL_THRESHOLD = 0.25
SCORER_VERSION = "foldseek-pdb100-carried-ligand-overlap/v1"


class StageNoveltyError(RuntimeError):
    """Raised when a staged item cannot be classified safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _stage_ligand(model: Any) -> Any:
    candidates = [
        residue
        for chain in model
        for residue in chain
        if residue.het_flag == "H" and not residue.is_water()
    ]
    if len(candidates) != 1:
        raise StageNoveltyError("xtal_lig.pdb must contain exactly one ligand residue")
    return candidates[0]


def score_item(item: dict[str, Any], stage: Path) -> dict[str, Any]:
    """Return one complete novelty record or raise without a classification."""

    item_id = str(item.get("id", "")).upper()
    if not item_id or item_id != str(item.get("id", "")):
        raise StageNoveltyError("stage item has an invalid uppercase PDB ID")
    item_dir = stage / "data" / item_id
    protein_path = item_dir / "protein.pdb"
    ligand_path = item_dir / "xtal_lig.pdb"
    if not protein_path.is_file() or not ligand_path.is_file():
        raise StageNoveltyError("staged protein or crystal ligand is missing")

    protein_model = bts.load(str(protein_path))
    ligand_model = bts.load(str(ligand_path))
    query_ligand = _stage_ligand(ligand_model)
    query_positions, query_radii = bts.lig_arrays(query_ligand)
    sequence = bts.longest_seq(protein_model)
    if not sequence:
        raise StageNoveltyError("staged protein has no searchable polymer")

    hits = bts.search_pre_cutoff(sequence, item_id, _cif=str(protein_path))
    if hits is None:
        raise StageNoveltyError("Foldseek search failed; novelty remains unknown")
    maximum_identity = max(
        (hit["identity"] for hit in hits if hit.get("identity") is not None),
        default=None,
    )
    query_polymer = bts._first_poly(protein_model)
    query_ca = bts._poly_ca(query_polymer) if query_polymer is not None else None
    if query_ca is None:
        raise StageNoveltyError("staged protein has no Foldseek-aligned polymer")

    best: tuple[float, str, str, float, float | None] | None = None
    for hit in hits[:25]:
        if not hit.get("qAln"):
            continue
        try:
            reference_model = bts.load(hit["pdb"])
            superposition = bts.align_superpose(hit, query_ca, query_positions)
        except Exception:
            continue
        if superposition is None:
            continue
        transform, rmsd, _local_residues = superposition
        if rmsd > bts.MAX_LOCAL_RMSD:
            continue
        ligand_names = sorted(
            {
                residue.name
                for chain in reference_model
                for residue in chain
                if not residue.is_water()
                and residue.het_flag == "H"
                and bts.druglike(residue.name)
            }
        )
        for ligand_name in ligand_names:
            reference_ligand = bts.lig_atoms(reference_model, ligand_name)
            if reference_ligand is None:
                continue
            atoms = [atom for atom in reference_ligand if atom.element.name != "H"]
            carried = [transform.apply(atom.pos) for atom in atoms]
            reference_positions = np.array([[pos.x, pos.y, pos.z] for pos in carried])
            reference_radii = np.array([bts.vdw(atom.element.name) for atom in atoms])
            overlap = bts.vol_tanimoto(
                query_positions, query_radii, reference_positions, reference_radii
            )
            if best is None or overlap > best[0]:
                best = (overlap, hit["pdb"], ligand_name, rmsd, hit.get("identity"))

    result: dict[str, Any] = {
        "item_id": item_id,
        "week": item.get("week"),
        "ligand": item.get("ligand"),
        "protein_sha256": _sha256(protein_path),
        "xtal_ligand_sha256": _sha256(ligand_path),
        "cutoff": CUTOFF,
        "novel_threshold": NOVEL_THRESHOLD,
        "scorer_version": SCORER_VERSION,
        "foldseek_database": "pdb100",
        "train_max_protein_identity": (
            round(maximum_identity, 3) if maximum_identity is not None else None
        ),
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    if best is None:
        result.update(
            {
                "train_pdb": None,
                "train_het": None,
                "train_identity": None,
                "train_align_rmsd": None,
                "train_shape_overlap": None,
                "novel": True,
            }
        )
        return result
    overlap, pdb_id, ligand_name, rmsd, identity = best
    result.update(
        {
            "train_pdb": pdb_id,
            "train_het": ligand_name,
            "train_identity": round(identity, 3) if identity is not None else None,
            "train_align_rmsd": round(rmsd, 2),
            "train_shape_overlap": round(overlap, 3),
            "novel": overlap < NOVEL_THRESHOLD,
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
            "cutoff": result.get("cutoff"),
            "novel_threshold": result.get("novel_threshold"),
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

    bts.FSHITS = cache / "foldseek-hits"
    bts.REFCACHE = cache / "rcsb-structures"
    bts.FSHITS.mkdir(parents=True, exist_ok=True)
    bts.REFCACHE.mkdir(parents=True, exist_ok=True)

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
            result = score_item(item, stage)
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
