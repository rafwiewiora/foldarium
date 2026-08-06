"""Small adapter interface used identically by local, Modal, and GCP workers."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CommandPlan:
    argv: tuple[str, ...]
    input_path: Path
    output_dir: Path
    environment: Mapping[str, str] = field(default_factory=dict)

    def public_dict(self, work_root: Path) -> dict[str, Any]:
        def display(value: str) -> str:
            try:
                return str(Path(value).relative_to(work_root))
            except (ValueError, OSError):
                return value

        return {
            "argv": [display(arg) for arg in self.argv],
            "input_path": display(str(self.input_path)),
            "output_dir": display(str(self.output_dir)),
            "environment_keys": sorted(self.environment),
        }


class MethodAdapter(ABC):
    name: str

    @abstractmethod
    def plan(self, task: Mapping[str, Any], work_dir: Path) -> CommandPlan:
        """Materialize method input and return an allowlisted argument vector."""

    @abstractmethod
    def collect(self, task: Mapping[str, Any], output_dir: Path) -> list[dict[str, Any]]:
        """Normalize completed model outputs into samples and artifacts."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def artifact(path: Path, root: Path, role: str) -> dict[str, Any]:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"artifact escapes output directory: {path}") from exc
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    media_type, _ = mimetypes.guess_type(resolved.name)
    if resolved.suffix == ".cif":
        media_type = "chemical/x-mmcif"
    return {
        "role": role,
        "relative_path": relative.as_posix(),
        "sha256": digest.hexdigest(),
        "size_bytes": resolved.stat().st_size,
        "media_type": media_type or "application/octet-stream",
    }


def confidence_summary(path: Path) -> dict[str, float]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    allowed = {
        "avg_plddt",
        "avg_pLDDT",
        "confidence_score",
        "complex_plddt",
        "complex_iplddt",
        "iptm",
        "ptm",
        "ranking_score",
        "gpde",
        "clash",
    }
    return {
        key: float(item)
        for key, item in value.items()
        if key in allowed and isinstance(item, (int, float)) and not isinstance(item, bool)
    }
