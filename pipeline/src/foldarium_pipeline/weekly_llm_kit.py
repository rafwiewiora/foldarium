"""Safe selector kit extraction and per-item workspace construction."""

from __future__ import annotations

import io
import json
import os
import stat
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .contracts import validate_target
from .weekly_selector import WeeklySelectorError, canonical_json, verify_selector_kit_zip
from .weekly_llm_contract import sha256_hex
from .weekly_llm_evidence import MAX_EVIDENCE_JSON_BYTES, WeeklyLlmEvidenceError

MAX_KIT_ZIP_BYTES = 200_000_000
MAX_UNCOMPRESSED_BYTES = 500_000_000


class WeeklyLlmKitError(WeeklySelectorError):
    """Raised when kit extraction or workspace construction fails."""


def _chmod_or_raise(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as error:
        raise WeeklyLlmKitError(f"unable to set permissions on {path}: {error}") from error


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
    uncompressed_total = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            _safe_zip_member_name(info.filename)
            if info.file_size < 0:
                raise WeeklyLlmKitError(f"invalid uncompressed size for {info.filename}")
            uncompressed_total += info.file_size
            if uncompressed_total > MAX_UNCOMPRESSED_BYTES:
                raise WeeklyLlmKitError(
                    f"kit ZIP uncompressed payload exceeds {MAX_UNCOMPRESSED_BYTES} bytes"
                )
    manifest = verify_selector_kit_zip(zip_bytes)
    output_dir.mkdir(parents=True, exist_ok=True)
    _chmod_or_raise(output_dir, 0o700)
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
            _chmod_or_raise(target, 0o400)
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


def _load_normalized_target(target_path: Path) -> dict[str, Any]:
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    return validate_target(payload)


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
    _chmod_or_raise(evidence_dir, 0o700)

    target_path = item_root / "target.json"
    if not target_path.is_file():
        raise WeeklyLlmKitError(f"missing target.json for {item_id}")
    normalized_target = _load_normalized_target(target_path)

    candidate_evidence: list[dict[str, Any]] = []
    image_attachments: list[dict[str, Any]] = []
    attachment_index = 0
    for choice in sorted(item["choices"], key=lambda row: row["choice_id"]):
        choice_id = choice["choice_id"]
        choice_dir = item_root / "choices" / choice_id
        from .weekly_llm_evidence import build_choice_evidence

        evidence, images = build_choice_evidence(
            choice_id=choice_id,
            cluster_id=choice["cluster_id"],
            is_rep=choice["is_rep"],
            attachment_index=attachment_index,
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
            _chmod_or_raise(image_path, 0o600)
            image_attachments.append(
                {
                    "attachment_index": attachment_index,
                    "choice_id": choice_id,
                    "filename": filename,
                    "path": str(image_path),
                    "sha256": sha256_hex(content),
                }
            )
            attachment_index += 1
        candidate_evidence.append(evidence)

    item_evidence = {
        "target": normalized_target,
        "candidates": candidate_evidence,
    }
    evidence_json = canonical_json(item_evidence)
    if len(evidence_json.encode("utf-8")) > MAX_EVIDENCE_JSON_BYTES:
        raise WeeklyLlmEvidenceError(
            f"{item_id} candidate evidence exceeds {MAX_EVIDENCE_JSON_BYTES} bytes"
        )
    evidence_digest = sha256_hex(item_evidence)
    return {
        "item_id": item_id,
        "target_path": target_path,
        "item_evidence": item_evidence,
        "candidate_evidence": candidate_evidence,
        "candidate_evidence_digest": evidence_digest,
        "image_attachments": image_attachments,
        "evidence_dir": evidence_dir,
    }


__all__ = [
    "MAX_UNCOMPRESSED_BYTES",
    "WeeklyLlmKitError",
    "build_item_workspace",
    "extract_verified_kit",
]
