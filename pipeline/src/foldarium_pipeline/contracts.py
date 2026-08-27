"""Versioned JSON contracts shared by every execution backend and method.

The contract intentionally contains no provider SDK, Supabase, OpenFold3, or Boltz
SDK types. A task can be stored in Postgres, placed on a queue, or handed to a
local process without translation.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping

SCHEMA_VERSION = "foldarium.prediction/v1"
ENTITY_TYPES = frozenset({"protein", "dna", "rna", "ligand"})
METHODS = frozenset({"openfold3", "boltz2"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHAIN_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class ContractError(ValueError):
    """Raised when a pipeline payload violates the public contract."""


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for identities and manifests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_id(prefix: str, value: Any, length: int = 24) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:length]}"


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return deepcopy(dict(value))


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractError(f"{field} must be a safe identifier")
    return value


def _nonempty_string(value: Any, field: str, max_length: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ContractError(f"{field} exceeds {max_length} characters")
    return value.strip()


def _positive_int(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ContractError(f"{field} must be an integer from 1 to {maximum}")
    return value


def _validate_entity(raw: Any, index: int) -> dict[str, Any]:
    entity = _mapping(raw, f"target.entities[{index}]")
    kind = entity.get("type")
    if kind not in ENTITY_TYPES:
        raise ContractError(f"target.entities[{index}].type must be one of {sorted(ENTITY_TYPES)}")

    raw_ids = entity.get("chain_ids")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ContractError(f"target.entities[{index}].chain_ids must be a non-empty list")
    chain_ids: list[str] = []
    for chain_id in raw_ids:
        if not isinstance(chain_id, str) or not _CHAIN_RE.fullmatch(chain_id):
            raise ContractError(f"target.entities[{index}].chain_ids contains an unsafe chain ID")
        if chain_id in chain_ids:
            raise ContractError(f"target.entities[{index}].chain_ids contains a duplicate")
        chain_ids.append(chain_id)

    normalized: dict[str, Any] = {"type": kind, "chain_ids": chain_ids}
    if kind in {"protein", "dna", "rna"}:
        sequence = _nonempty_string(entity.get("sequence"), f"target.entities[{index}].sequence")
        sequence = re.sub(r"\s+", "", sequence).upper()
        if not sequence.isalpha():
            raise ContractError(f"target.entities[{index}].sequence must contain letters only")
        normalized["sequence"] = sequence
        if "msa" in entity:
            msa = _mapping(entity["msa"], f"target.entities[{index}].msa")
            mode = msa.get("mode")
            if mode not in {"server", "empty", "artifact"}:
                raise ContractError("MSA mode must be server, empty, or artifact")
            normalized_msa: dict[str, Any] = {"mode": mode}
            if mode == "artifact":
                normalized_msa["uri"] = _nonempty_string(msa.get("uri"), "msa.uri", 4096)
                normalized_msa["sha256"] = _nonempty_string(msa.get("sha256"), "msa.sha256", 64)
                if not re.fullmatch(r"[0-9a-f]{64}", normalized_msa["sha256"]):
                    raise ContractError("msa.sha256 must be a lowercase SHA-256 digest")
            normalized["msa"] = normalized_msa
    else:
        representations = [key for key in ("smiles", "ccd_codes") if entity.get(key)]
        if len(representations) != 1:
            raise ContractError(
                f"target.entities[{index}] ligand must have exactly one of smiles or ccd_codes"
            )
        if representations[0] == "smiles":
            normalized["smiles"] = _nonempty_string(
                entity["smiles"], f"target.entities[{index}].smiles", 10_000
            )
        else:
            ccd_codes = entity["ccd_codes"]
            if isinstance(ccd_codes, str):
                ccd_codes = [ccd_codes]
            if not isinstance(ccd_codes, list) or not ccd_codes:
                raise ContractError(f"target.entities[{index}].ccd_codes must be a non-empty list")
            normalized["ccd_codes"] = [
                _identifier(code, f"target.entities[{index}].ccd_codes") for code in ccd_codes
            ]

    if "options" in entity:
        normalized["options"] = _mapping(entity["options"], f"target.entities[{index}].options")
    return normalized


def validate_target(raw: Any) -> dict[str, Any]:
    """Validate and normalize a method-neutral target package."""

    target = _mapping(raw, "target")
    if target.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ContractError(f"unsupported schema_version; expected {SCHEMA_VERSION}")
    target_id = _identifier(target.get("target_id"), "target.target_id")
    entities_raw = target.get("entities")
    if not isinstance(entities_raw, list) or not entities_raw:
        raise ContractError("target.entities must be a non-empty list")
    entities = [_validate_entity(entity, index) for index, entity in enumerate(entities_raw)]
    all_chain_ids = [chain_id for entity in entities for chain_id in entity["chain_ids"]]
    if len(all_chain_ids) != len(set(all_chain_ids)):
        raise ContractError("chain IDs must be unique across target entities")
    if not any(entity["type"] in {"protein", "dna", "rna"} for entity in entities):
        raise ContractError("target must contain at least one polymer entity")

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target_id": target_id,
        "entities": entities,
    }
    if "source" in target:
        normalized["source"] = _mapping(target["source"], "target.source")
    if "metadata" in target:
        normalized["metadata"] = _mapping(target["metadata"], "target.metadata")
    return normalized


def make_prediction_task(
    *,
    campaign_id: str,
    target: Mapping[str, Any],
    method: str,
    method_version: str,
    container_image: str,
    config: Mapping[str, Any],
    output_uri_prefix: str,
    resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic task; identical scientific inputs get the same ID."""

    normalized_target = validate_target(target)
    campaign_id = _identifier(campaign_id, "campaign_id")
    if method not in METHODS:
        raise ContractError(f"method must be one of {sorted(METHODS)}")
    method_version = _nonempty_string(method_version, "method_version", 128)
    container_image = _nonempty_string(container_image, "container_image", 2048)
    config_dict = _mapping(config, "config")
    resources_dict = _mapping(resources or {}, "resources")
    output_uri_prefix = _nonempty_string(output_uri_prefix, "output_uri_prefix", 4096).rstrip("/")
    identity = {
        "campaign_id": campaign_id,
        "target": normalized_target,
        "method": method,
        "method_version": method_version,
        "container_image": container_image,
        "config": config_dict,
    }
    task_id = stable_id("run", identity)
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        **identity,
        "output_uri_prefix": f"{output_uri_prefix}/{task_id}",
        "resources": resources_dict,
    }


def validate_prediction_task(raw: Any) -> dict[str, Any]:
    task = _mapping(raw, "task")
    if task.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"task.schema_version must be {SCHEMA_VERSION}")
    expected = make_prediction_task(
        campaign_id=task.get("campaign_id"),
        target=task.get("target"),
        method=task.get("method"),
        method_version=task.get("method_version"),
        container_image=task.get("container_image"),
        config=task.get("config"),
        output_uri_prefix=_nonempty_string(task.get("output_uri_prefix"), "output_uri_prefix", 4096)
        .rsplit("/", 1)[0],
        resources=task.get("resources", {}),
    )
    if task.get("task_id") != expected["task_id"]:
        raise ContractError("task_id does not match the task's scientific identity")
    if task.get("output_uri_prefix") != expected["output_uri_prefix"]:
        raise ContractError("output_uri_prefix must end in the deterministic task_id")
    return expected


def validate_int_config(
    config: Mapping[str, Any], key: str, default: int, maximum: int
) -> int:
    return _positive_int(config.get(key, default), f"config.{key}", maximum)
