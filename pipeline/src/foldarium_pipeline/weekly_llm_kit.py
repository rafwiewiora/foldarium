"""Safe selector kit extraction and per-item workspace construction."""

from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .weekly_selector import WeeklySelectorError, verify_selector_kit_zip
from .weekly_llm_contract import sha256_hex

MAX_KIT_ZIP_BYTES = 200_000_000


class WeeklyLlmKitError(WeeklySelectorError):
    """Raised when kit extraction or workspace construction fails."""


def _safe_zip_member_name(name: str) -> str:
    if not name or name.startswith("/") or "\\" in name:
        raise WeeklyLlmKitError(f"unsafe ZIP path: {name!r}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise WeeklyLlmKitError(f"unsafe ZIP path: {name!r}")
    return name


def extract_verified_kit(
    zip_bytes: bytes,
    *,
    output_dir: Path,
) -> dict[str, Any]:
    if len(zip_bytes) > MAX_KIT_ZIP_BYTES:
        raise WeeklyLlmKitError(f"kit ZIP exceeds {MAX_KIT_ZIP_BYTES} bytes")
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            _safe_zip_member_name(info.filename)
    manifest = verify_selector_kit_zip(zip_bytes)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            path = _safe_zip_member_name(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise WeeklyLlmKitError(f"symlink entries are forbidden: {path}")
            target = output_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            content = archive.read(info.filename)
            declared = _declared_digest(manifest, path)
            if declared is not None and sha256_hex(content) != declared:
                raise WeeklyLlmKitError(f"declared digest mismatch for {path}")
            target.write_bytes(content)
            try:
                os.chmod(target, 0o400)
            except OSError:
                pass
    return manifest


def _declared_digest(manifest: Mapping[str, Any], path: str) -> str | None:
    files = manifest.get("files")
    if not isinstance(files, list):
        return None
    for entry in files:
        if isinstance(entry, Mapping) and entry.get("path") == path:
            digest = entry.get("sha256")
            return digest if isinstance(digest, str) else None
    return None


def build_item_workspace(
    *,
    kit_dir: Path,
    item: Mapping[str, Any],
    evidence_dir: Path,
) -> dict[str, Any]:
    item_id = item["item_id"]
    item_root = kit_dir / "items" / item_id
    if not item_root.is_dir():
        raise WeeklyLlmKitError(f"missing item directory for {item_id}")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(evidence_dir, 0o700)
    except OSError:
        pass

    candidate_evidence: list[dict[str, Any]] = []
    image_paths: list[Path] = []
    for choice in sorted(item["choices"], key=lambda row: row["choice_id"]):
        choice_id = choice["choice_id"]
        choice_dir = item_root / "choices" / choice_id
        from .weekly_llm_evidence import build_choice_evidence

        evidence, images = build_choice_evidence(
            choice_id=choice_id,
            cluster_id=choice["cluster_id"],
            is_rep=choice["is_rep"],
            descriptors=choice["descriptors"],
            pose_bytes=(choice_dir / "pose.pdb").read_bytes(),
            protein_bytes=(choice_dir / "protein.pdb").read_bytes(),
            pocket_bytes=(choice_dir / "pocket.pdb").read_bytes(),
        )
        choice_evidence_dir = evidence_dir / choice_id
        choice_evidence_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in sorted(images.items()):
            image_path = choice_evidence_dir / filename
            image_path.write_bytes(content)
            try:
                os.chmod(image_path, 0o600)
            except OSError:
                pass
            image_paths.append(image_path)
        candidate_evidence.append(evidence)

    return {
        "item_id": item_id,
        "target_path": item_root / "target.json",
        "candidate_evidence": candidate_evidence,
        "image_paths": image_paths,
        "evidence_dir": evidence_dir,
    }


__all__ = [
    "WeeklyLlmKitError",
    "build_item_workspace",
    "extract_verified_kit",
]
