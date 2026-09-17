"""Fail-closed checkpoint resume helpers for weekly LLM scoring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .weekly_llm_contract import sha256_hex
from .weekly_selector import WeeklySelectorError, canonical_json


class WeeklyLlmResumeError(WeeklySelectorError):
    """Raised when checkpoint resume validation fails."""

_CHECKPOINT_NAMES = ("prompt", "candidate_evidence", "validated_response", "raw_envelope")
_FORBIDDEN_RESUME_ARTIFACTS = (
    "benchmark.execution.json",
    "benchmark.public.json",
    "submission.json",
)


def item_checkpoint_paths(resume_root: Path, item_id: str) -> dict[str, Path]:
    item_private = resume_root / "private" / "items" / item_id
    return {
        "prompt": item_private / "prompt.txt",
        "candidate_evidence": item_private / "candidate-evidence.json",
        "validated_response": item_private / "validated-response.json",
        "raw_envelope": resume_root / "private" / f"{item_id}.raw.json",
    }


def checkpoint_presence(paths: Mapping[str, Path]) -> str:
    exists = {name: path.is_file() for name, path in paths.items()}
    if all(exists.values()):
        return "complete"
    if not any(exists.values()):
        return "absent"
    if (
        exists["prompt"]
        and exists["candidate_evidence"]
        and not exists["validated_response"]
        and not exists["raw_envelope"]
    ):
        return "prepared_only"
    return "partial"


def validate_resume_source(*, resume_root: Path, execution_id: str) -> None:
    if not resume_root.is_dir():
        raise WeeklyLlmResumeError(f"resume source {resume_root} is not a directory")
    private_dir = resume_root / "private"
    if not private_dir.is_dir():
        raise WeeklyLlmResumeError(f"resume source {resume_root} is missing private/")
    if resume_root.name != execution_id:
        raise WeeklyLlmResumeError(
            f"resume source directory name {resume_root.name!r} must match execution id {execution_id!r}"
        )
    for artifact in _FORBIDDEN_RESUME_ARTIFACTS:
        if (resume_root / artifact).exists():
            raise WeeklyLlmResumeError(
                f"resume source must not include completed artifact {artifact}; only item checkpoints"
            )


def load_validated_checkpoint_response(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WeeklyLlmResumeError(f"resume checkpoint {path} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise WeeklyLlmResumeError(f"resume checkpoint {path} must be a JSON object")
    return payload


def load_raw_envelope_checkpoint(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WeeklyLlmResumeError(f"resume raw envelope {path} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise WeeklyLlmResumeError(f"resume raw envelope {path} must be a JSON object")
    return dict(payload)


def assert_checkpoint_bytes_match(
    *,
    item_id: str,
    prompt_bytes: bytes,
    evidence_json_bytes: bytes,
    checkpoint_paths: Mapping[str, Path],
) -> None:
    prior_prompt = checkpoint_paths["prompt"].read_bytes()
    prior_evidence = checkpoint_paths["candidate_evidence"].read_bytes()
    if prior_prompt != prompt_bytes:
        raise WeeklyLlmResumeError(
            f"resume checkpoint prompt bytes for {item_id} do not match current kit render"
        )
    if prior_evidence != evidence_json_bytes:
        raise WeeklyLlmResumeError(
            f"resume checkpoint candidate evidence for {item_id} does not match current kit render"
        )


def validated_response_matches_checkpoint(
    *,
    validated: Mapping[str, Any],
    checkpoint_validated: Mapping[str, Any],
) -> bool:
    return canonical_json(dict(validated)) == canonical_json(dict(checkpoint_validated))


def choice_id_from_contact_sheet_path(path: str) -> tuple[str, str]:
    parts = Path(path).parts
    if "evidence" not in parts:
        raise WeeklyLlmResumeError("resume contact sheet path must live under evidence/")
    evidence_index = parts.index("evidence")
    if len(parts) != evidence_index + 4:
        raise WeeklyLlmResumeError(
            "resume contact sheet path must be evidence/<item_id>/<choice_id>/contact_sheet.png"
        )
    item_id = parts[evidence_index + 1]
    choice_id = parts[evidence_index + 2]
    if Path(path).name != "contact_sheet.png":
        raise WeeklyLlmResumeError("resume contact sheet path must end with contact_sheet.png")
    return item_id, choice_id


def map_resume_contact_sheet_paths(
    *,
    item_id: str,
    old_allowed_paths: Sequence[str],
    current_image_paths: Sequence[str],
    attachment_shas_by_choice: Mapping[str, str],
) -> dict[str, str]:
    current_by_choice: dict[str, str] = {}
    for raw_path in current_image_paths:
        mapped_item_id, choice_id = choice_id_from_contact_sheet_path(raw_path)
        if mapped_item_id != item_id:
            raise WeeklyLlmResumeError("resume contact sheet item_id mismatch")
        if choice_id in current_by_choice:
            raise WeeklyLlmResumeError("resume contact sheet choice_id collision")
        current_by_choice[choice_id] = str(Path(raw_path).resolve())

    mapping: dict[str, str] = {}
    for old_path in old_allowed_paths:
        if not isinstance(old_path, str) or not old_path.strip():
            raise WeeklyLlmResumeError("resume allowed_contact_sheet_paths entry is invalid")
        old_item_id, choice_id = choice_id_from_contact_sheet_path(old_path)
        if old_item_id != item_id:
            raise WeeklyLlmResumeError("resume envelope contact sheet item_id mismatch")
        current_path = current_by_choice.get(choice_id)
        if current_path is None:
            raise WeeklyLlmResumeError(
                f"resume checkpoint references contact sheet choice {choice_id} missing from current evidence"
            )
        current_file = Path(current_path)
        if not current_file.is_file():
            raise WeeklyLlmResumeError(f"resume contact sheet missing at current path: {current_path}")
        digest = sha256_hex(current_file.read_bytes())
        expected = attachment_shas_by_choice.get(choice_id)
        if expected is None or digest != expected:
            raise WeeklyLlmResumeError(
                f"resume contact sheet digest mismatch for choice {choice_id}"
            )
        mapping[str(Path(old_path).resolve())] = current_path
    if set(current_by_choice.keys()) != {choice_id_from_contact_sheet_path(path)[1] for path in old_allowed_paths}:
        raise WeeklyLlmResumeError("resume contact sheet mapping must cover every current attachment")
    return mapping


__all__ = [
    "assert_checkpoint_bytes_match",
    "checkpoint_presence",
    "choice_id_from_contact_sheet_path",
    "item_checkpoint_paths",
    "load_raw_envelope_checkpoint",
    "load_validated_checkpoint_response",
    "map_resume_contact_sheet_paths",
    "validate_resume_source",
    "validated_response_matches_checkpoint",
]
