#!/usr/bin/env python3
"""Offline CLI for deterministic weekly trace archive export and verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from foldarium_pipeline.trace_archive import (
    TraceArchiveError,
    export_session_archive,
    read_source,
    verify_session_archive,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Export or verify a local Foldarium weekly trace archive (no network or deletion)."
    )
    commands = result.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source", required=True, type=Path)
    export.add_argument("--output-dir", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--archive", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--source", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "export":
            result = export_session_archive(read_source(args.source), args.output_dir)
            report = {
                "archive": str(result.archive_path),
                "manifest": str(result.manifest_path),
                "archive_id": result.manifest["archive_id"],
                "reused_existing": result.reused_existing,
                "verified": True,
            }
        else:
            source = read_source(args.source) if args.source else None
            report = verify_session_archive(args.archive, args.manifest, source=source)
    except TraceArchiveError as error:
        raise SystemExit(f"archive validation failed: {error}") from error
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
