"""Portable repository and external-data paths for CAMEO prep scripts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
BENCHMARK_PREP = REPOSITORY_ROOT / "benchmark" / "prep"


def default_cameo_dir() -> Path:
    """Return the configured CAMEO extraction root."""
    configured = os.environ.get("FOLDARIUM_CAMEO_DIR")
    return Path(configured) if configured else REPOSITORY_ROOT / "cameo_data" / "extracted" / "modeling"


def add_cameo_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cameo-dir",
        type=Path,
        default=default_cameo_dir(),
        help=(
            "extracted CAMEO modeling directory (default: FOLDARIUM_CAMEO_DIR, "
            "otherwise <repository>/cameo_data/extracted/modeling)"
        ),
    )
