#!/usr/bin/env python3
"""Verify and optionally update cache metadata for one revealed Weekly round."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from foldarium_pipeline.cache_backfill import (
    backfill_immutable_cache,
    verified_public_object_inventory,
)
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replace verified identical bytes to update cache metadata.",
    )
    args = parser.parse_args()

    coordinator = SupabaseCoordinator.from_env()
    round_row = coordinator.weekly_quiz_round(args.round_id)
    if round_row.get("environment") != "production":
        raise SupabasePublicationError("cache backfill accepts production rounds only")
    if args.apply and round_row.get("status") != "revealed":
        raise SupabasePublicationError("refusing to update a round that is not revealed")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest != round_row.get("blind_manifest"):
        raise SupabasePublicationError(
            "local blind manifest does not exactly match the stored round manifest"
        )
    inventory = verified_public_object_inventory(
        manifest,
        round_id=args.round_id,
        expected_manifest_sha256=round_row["blind_manifest_sha256"],
        public_bucket=coordinator.storage_bucket,
    )
    summary = backfill_immutable_cache(coordinator, inventory, apply=args.apply)
    summary.update({"round_id": args.round_id, "object_count": len(inventory)})
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.apply:
        print("Dry run only. Re-run with --apply after the round is revealed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
