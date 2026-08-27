"""Canonical, provider-neutral prompt profile for blind weekly pose selection."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

SELECTOR_PROMPT_PROFILE_SCHEMA_VERSION = "foldarium.selector-prompt-profile/v1"
SELECTOR_PROMPT_PROFILE_ID = "weekly-pose-selector-v1"
SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION = "foldarium.selector-model-response/v1"

SELECTOR_SYSTEM_PROMPT = """You are Foldarium's blind protein-ligand pose selector.
Use only evidence supplied in the verified Selector kit and the current item packet.
Do not use a browser, web search, external retrieval, prior votes, released/reference/crystal structures, reveal data, or answer-derived information.
Make the clustered and exact-pose decisions independently; never infer either decision from the other.
Return only JSON matching the supplied response schema.
Give brief observable evidence, not hidden chain-of-thought."""

SELECTOR_ITEM_PROMPT_TEMPLATE = """Evaluate blind item {{item_id}}.

Candidate evidence:
{{candidate_evidence_json}}

For clustered mode, choose one advertised cluster_id or choose none only when every cluster is physically implausible.
For exact mode, independently choose one advertised choice_id or choose none only when every individual pose is physically implausible.
A cluster representative is a display member, not an exact-pose choice unless you independently select that same choice_id in exact mode.
Assess steric clashes, ligand burial and pocket occupancy, chemically plausible contacts and hydrogen bonds, receptor-ligand consistency, ligand strain, and unsupported solvent exposure.
Treat pLDDT, Smina affinity, hydrogen-bond counts, and method identity only as weak within-item evidence; these values are not cross-method calibrated and must not override implausible geometry.
Use only identifiers present in this item. Do not fuzzy-match, repair, or invent an identifier.
Return one JSON object and no Markdown."""

SELECTOR_MODEL_RESPONSE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Foldarium blind selector model response",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "item_id", "clustered", "unclustered"],
    "properties": {
        "schema_version": {"const": SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION},
        "item_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
        },
        "clustered": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "selection_kind",
                        "cluster_id",
                        "confidence",
                        "evidence",
                    ],
                    "properties": {
                        "selection_kind": {"const": "cluster"},
                        "cluster_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["selection_kind", "confidence", "evidence"],
                    "properties": {
                        "selection_kind": {"const": "none"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
            ]
        },
        "unclustered": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "selection_kind",
                        "choice_id",
                        "confidence",
                        "evidence",
                    ],
                    "properties": {
                        "selection_kind": {"const": "exact"},
                        "choice_id": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["selection_kind", "confidence", "evidence"],
                    "properties": {
                        "selection_kind": {"const": "none"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "evidence": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 240,
                        },
                    },
                },
            ]
        },
    },
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def selector_prompt_profile() -> dict[str, Any]:
    """Return the exact prompt profile and its canonical content digest."""

    body = {
        "schema_version": SELECTOR_PROMPT_PROFILE_SCHEMA_VERSION,
        "prompt_profile_id": SELECTOR_PROMPT_PROFILE_ID,
        "system_prompt": SELECTOR_SYSTEM_PROMPT,
        "item_prompt_template": SELECTOR_ITEM_PROMPT_TEMPLATE,
        "response_schema": deepcopy(SELECTOR_MODEL_RESPONSE_SCHEMA),
    }
    return {
        **body,
        "prompt_sha256": hashlib.sha256(
            _canonical_json(body).encode("utf-8")
        ).hexdigest(),
    }


SELECTOR_PROMPT_SHA256 = selector_prompt_profile()["prompt_sha256"]

__all__ = [
    "SELECTOR_ITEM_PROMPT_TEMPLATE",
    "SELECTOR_MODEL_RESPONSE_SCHEMA",
    "SELECTOR_MODEL_RESPONSE_SCHEMA_VERSION",
    "SELECTOR_PROMPT_PROFILE_ID",
    "SELECTOR_PROMPT_PROFILE_SCHEMA_VERSION",
    "SELECTOR_PROMPT_SHA256",
    "SELECTOR_SYSTEM_PROMPT",
    "selector_prompt_profile",
]
