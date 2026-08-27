"""Shared, portable path arguments for the legacy Runs-n-Poses scripts."""

from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Paths:
    rnp_dir: Path
    quiz_dir: Path
    annotations: Path
    work_dir: Path


def parse_paths(description: str) -> Paths:
    """Parse the common data locations before a script starts doing any I/O."""
    parser = argparse.ArgumentParser(
        description=description,
        epilog=(
            "The RnP archive/data are not bundled with this repository. "
            "Pass their location with --rnp-dir or FOLDARIUM_RNP_DATA_DIR. "
            "Individual files are expected at the names used by the RnP pipeline."
        ),
    )
    parser.add_argument(
        "--rnp-dir",
        type=Path,
        default=Path(os.environ.get("FOLDARIUM_RNP_DATA_DIR", REPOSITORY_ROOT / "rnp_data")),
        help=(
            "RnP data directory (default: FOLDARIUM_RNP_DATA_DIR, otherwise "
            "<repository>/rnp_data)"
        ),
    )
    parser.add_argument(
        "--quiz-dir",
        type=Path,
        default=Path(os.environ.get("FOLDARIUM_QUIZ_DIR", REPOSITORY_ROOT)),
        help=(
            "directory containing quiz_items_rnp*.json and data_rnp_v2 "
            "(default: FOLDARIUM_QUIZ_DIR, otherwise repository root)"
        ),
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=(
            Path(os.environ["FOLDARIUM_RNP_ANNOTATIONS"])
            if "FOLDARIUM_RNP_ANNOTATIONS" in os.environ
            else None
        ),
        help=(
            "rnp_annotations.csv path (default: FOLDARIUM_RNP_ANNOTATIONS, "
            "otherwise <rnp-dir>/rnp_annotations.csv)"
        ),
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(os.environ.get("FOLDARIUM_RNP_WORK_DIR", tempfile.gettempdir())),
        help=(
            "scratch/cache directory (default: FOLDARIUM_RNP_WORK_DIR, "
            "otherwise the system temporary directory)"
        ),
    )
    args = parser.parse_args()
    rnp_dir = args.rnp_dir.expanduser().resolve()
    if not rnp_dir.is_dir():
        parser.error(
            f"RnP data directory does not exist: {rnp_dir}. "
            "Pass --rnp-dir or set FOLDARIUM_RNP_DATA_DIR."
        )
    annotations = args.annotations or rnp_dir / "rnp_annotations.csv"
    return Paths(
        rnp_dir=rnp_dir,
        quiz_dir=args.quiz_dir.expanduser().resolve(),
        annotations=annotations.expanduser().resolve(),
        work_dir=args.work_dir.expanduser().resolve(),
    )
