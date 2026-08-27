#!/usr/bin/env python3
"""Reference CLI for Foldarium weekly selector kits."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
import zipfile
from pathlib import Path

from foldarium_pipeline.weekly_selector import (
    WeeklySelectorError,
    build_selector_submission,
    build_submission_template,
    canonical_json,
    digest_selector_submission,
    submit_selector_submission,
    validate_selector_submission,
    verify_selector_kit_zip,
)


def _write_kit_temp(zip_bytes: bytes) -> Path:
    path = Path("/tmp") / f"foldarium-selector-kit-{uuid.uuid4().hex}.zip"
    path.write_bytes(zip_bytes)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify kit ZIP entry hashes and deterministic manifest binding",
    )
    verify_parser.add_argument("kit", type=Path, help="path to selector kit ZIP")

    template_parser = subparsers.add_parser(
        "template",
        help="print a complete submission template",
    )
    template_parser.add_argument("kit", type=Path)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a completed submission JSON against a kit",
    )
    validate_parser.add_argument("kit", type=Path)
    validate_parser.add_argument("submission", type=Path)

    build_parser = subparsers.add_parser(
        "build",
        help="build and validate a submission from items JSON",
    )
    build_parser.add_argument("kit", type=Path)
    build_parser.add_argument("--submission-id", default=str(uuid.uuid4()))
    build_parser.add_argument("--items-json", type=Path, required=True)

    submit_parser = subparsers.add_parser(
        "submit",
        help="validate and POST a submission to the selector API",
    )
    submit_parser.add_argument("kit", type=Path)
    submit_parser.add_argument("submission", type=Path)
    submit_parser.add_argument("--api-url", required=True)
    submit_parser.add_argument("--bearer-token", required=True)

    digest_parser = subparsers.add_parser(
        "digest",
        help="print the payload digest for a validated submission",
    )
    digest_parser.add_argument("kit", type=Path)
    digest_parser.add_argument("submission", type=Path)

    args = parser.parse_args(argv)
    try:
        zip_bytes = args.kit.read_bytes()
        kit = verify_selector_kit_zip(zip_bytes)

        if args.command == "verify":
            print(
                canonical_json(
                    {
                        "ok": True,
                        "environment": kit["environment"],
                        "round_id": kit["round_id"],
                        "blind_manifest_sha256": kit["blind_manifest_sha256"],
                        "kit_sha256": kit["kit_sha256"],
                        "item_count": len(kit["items"]),
                    }
                )
            )
            return 0

        if args.command == "template":
            print(canonical_json(build_submission_template(kit)))
            return 0

        submission_raw = json.loads(args.submission.read_text(encoding="utf-8")) if args.command != "build" else None

        if args.command == "validate":
            normalized = validate_selector_submission(submission_raw, kit)
            print(canonical_json(normalized))
            return 0

        if args.command == "build":
            items = json.loads(args.items_json.read_text(encoding="utf-8"))
            submission = build_selector_submission(
                kit,
                submission_id=args.submission_id,
                items=items,
            )
            print(canonical_json(submission))
            return 0

        if args.command == "digest":
            normalized = validate_selector_submission(submission_raw, kit)
            print(digest_selector_submission(normalized))
            return 0

        if args.command == "submit":
            normalized = validate_selector_submission(submission_raw, kit)
            receipt = submit_selector_submission(
                args.api_url,
                args.bearer_token,
                normalized,
            )
            print(canonical_json(receipt))
            return 0
    except (WeeklySelectorError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
