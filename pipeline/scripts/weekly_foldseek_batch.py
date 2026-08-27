#!/usr/bin/env python3
"""Prepare or import a batched local Foldseek search for Weekly targets."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from foldarium_pipeline.training_similarity import (
    file_sha256,
    first_polymer_pdb,
    import_local_foldseek_tsv,
)
from foldarium_pipeline.weekly_training_audit import (
    DEFAULT_ORIGIN,
    download_blind_asset,
    download_rcsb_structure,
    load_all_targets,
)


def prepare(
    *,
    origin: str,
    cache_directory: Path,
    mode: str,
    query_directory: Path,
    manifest_path: Path,
    workers: int,
) -> dict:
    blind, exact = load_all_targets(origin, cache_directory)
    targets = exact if mode == "exact" else blind
    query_directory.mkdir(parents=True, exist_ok=True)

    def materialize(target):
        source = (
            download_rcsb_structure(target.item_id, cache_directory)
            if mode == "exact"
            else download_blind_asset(target.protein_uri, cache_directory)
        )
        query = query_directory / f"{target.item_id}.pdb"
        first_polymer_pdb(source, query)
        return {
            "item_id": target.item_id,
            "cache_label": mode,
            "source_sha256": file_sha256(source),
            "query_sha256": file_sha256(query),
        }

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(materialize, target): target for target in targets}
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{index}/{len(targets)}] {row['item_id']}", flush=True)
    manifest = {
        "format_version": "foldarium.weekly-local-foldseek-queries/v1",
        "mode": mode,
        "queries": sorted(rows, key=lambda row: row["item_id"]),
        "database_provenance": None,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    prepare_parser.add_argument("--cache-dir", required=True, type=Path)
    prepare_parser.add_argument("--mode", required=True, choices=("exact", "blind"))
    prepare_parser.add_argument("--query-dir", required=True, type=Path)
    prepare_parser.add_argument("--manifest", required=True, type=Path)
    prepare_parser.add_argument("--workers", type=int, default=8)
    import_parser = commands.add_parser("import")
    import_parser.add_argument("--cache-dir", required=True, type=Path)
    import_parser.add_argument("--manifest", required=True, type=Path)
    import_parser.add_argument("--tsv", required=True, type=Path)
    options = parser.parse_args(arguments)
    if options.command == "prepare":
        if options.workers < 1 or options.workers > 16:
            parser.error("--workers must be between 1 and 16")
        manifest = prepare(
            origin=options.origin,
            cache_directory=options.cache_dir,
            mode=options.mode,
            query_directory=options.query_dir,
            manifest_path=options.manifest,
            workers=options.workers,
        )
        print(json.dumps({"queries": len(manifest["queries"]), "mode": options.mode}))
    else:
        result = import_local_foldseek_tsv(
            options.tsv, options.manifest, options.cache_dir
        )
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
