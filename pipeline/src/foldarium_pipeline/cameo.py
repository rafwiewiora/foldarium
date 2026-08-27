"""Public CAMEO discovery and import helpers.

CAMEO currently exposes target pages as a Next.js application rather than a
versioned target JSON API.  This adapter isolates that unstable detail, validates
the decoded payload, and emits ordinary provider-neutral mappings.  Callers can
therefore retain and replay the exact source HTML if the public page changes.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import date
from typing import Any, Mapping
from urllib.parse import quote

CAMEO_ORIGIN = "https://cameo3d.org"
CAMEO_SITEMAP_URL = f"{CAMEO_ORIGIN}/api/sitemap"
CAMEO_AF3_SERVER_ID = "993"
CAMEO_DATA_LICENSE = "CC-BY-SA-4.0"

_TARGET_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{8}$")
_FLIGHT_SCRIPT = re.compile(r"<script>self\.__next_f\.push\((.*?)\)</script>", re.S)
_PREDICTION_COORDINATE_URL = re.compile(
    rf"^{re.escape(CAMEO_ORIGIN)}/api/coords/(\d{{4}}-\d{{2}}-\d{{2}}_\d{{8}})/"
    rf"{CAMEO_AF3_SERVER_ID}/([1-5])/model-([1-5])\.cif$"
)
_REFERENCE_COORDINATE_URL = re.compile(
    rf"^{re.escape(CAMEO_ORIGIN)}/api/coords/(\d{{4}}-\d{{2}}-\d{{2}}_\d{{8}})/"
    r"biounit/(\d{2})/reference\.cif\.gz$"
)


class CameoError(ValueError):
    """Raised when public CAMEO data does not satisfy the import contract."""


def _week(value: str | date) -> str:
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise CameoError("week must be an ISO date") from exc


def target_url(target_id: str) -> str:
    if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
        raise CameoError("invalid CAMEO target ID")
    return f"{CAMEO_ORIGIN}/target/{quote(target_id, safe='')}"


def parse_sitemap_targets(xml_text: str, week: str | date | None = None) -> list[str]:
    """Return sorted target IDs from CAMEO's public sitemap."""

    try:
        root = ET.fromstring(xml_text)
    except (TypeError, ET.ParseError) as exc:
        raise CameoError("CAMEO sitemap is not valid XML") from exc
    prefix = _week(week) + "_" if week is not None else None
    targets: set[str] = set()
    for node in root.iter():
        if not node.tag.endswith("loc") or not node.text:
            continue
        match = re.search(r"/target/(\d{4}-\d{2}-\d{2}_\d{8})/?$", node.text.strip())
        if match and (prefix is None or match.group(1).startswith(prefix)):
            targets.add(match.group(1))
    return sorted(targets)


def parse_target_page(page_html: str) -> dict[str, Any]:
    """Decode and validate the target payload embedded in a public target page."""

    if not isinstance(page_html, str) or not page_html:
        raise CameoError("CAMEO target page is empty")
    candidates: list[Mapping[str, Any]] = []
    for raw in _FLIGHT_SCRIPT.findall(page_html):
        try:
            value = json.loads(html.unescape(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, list) or len(value) < 2 or not isinstance(value[1], str):
            continue
        for line in value[1].splitlines():
            marker = line.find(":")
            if marker < 0 or not line[marker + 1 :].startswith("["):
                continue
            try:
                decoded = json.loads(line[marker + 1 :])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(decoded, list)
                and len(decoded) > 3
                and isinstance(decoded[3], Mapping)
                and {"target", "entities", "biounits", "predictions"}.issubset(decoded[3])
            ):
                candidates.append(decoded[3])
    if len(candidates) != 1:
        raise CameoError(f"expected one CAMEO target payload, found {len(candidates)}")

    payload = deepcopy(dict(candidates[0]))
    target = payload.get("target")
    entities = payload.get("entities")
    predictions = payload.get("predictions")
    if not isinstance(target, Mapping) or not _TARGET_ID.fullmatch(str(target.get("id", ""))):
        raise CameoError("CAMEO target payload has an invalid target ID")
    if not isinstance(entities, list) or not all(isinstance(item, Mapping) for item in entities):
        raise CameoError("CAMEO target payload has invalid entities")
    if not isinstance(predictions, list):
        raise CameoError("CAMEO target payload has invalid predictions")
    return payload


def af3_prediction_urls(target_id: str, models: int = 5) -> list[str]:
    """Return the public coordinate URLs used by CAMEO for AF3 models 1..N."""

    target_url(target_id)  # validation
    if isinstance(models, bool) or not isinstance(models, int) or not 1 <= models <= 5:
        raise CameoError("models must be an integer from 1 to 5")
    return [
        f"{CAMEO_ORIGIN}/api/coords/{target_id}/{CAMEO_AF3_SERVER_ID}/{model}/model-{model}.cif"
        for model in range(1, models + 1)
    ]


def reference_urls(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return every released reference assembly advertised on a target page."""

    target = payload.get("target")
    biounits = payload.get("biounits")
    if not isinstance(target, Mapping) or not isinstance(biounits, list):
        raise CameoError("invalid decoded CAMEO payload")
    target_id = str(target.get("id", ""))
    target_url(target_id)
    rows: list[dict[str, Any]] = []
    for raw in biounits:
        if not isinstance(raw, Mapping):
            continue
        assembly = raw.get("assembly_id")
        if isinstance(assembly, bool) or not isinstance(assembly, int) or assembly < 1:
            continue
        rows.append(
            {
                "assembly_id": assembly,
                "url": (
                    f"{CAMEO_ORIGIN}/api/coords/{target_id}/biounit/"
                    f"{assembly:02d}/reference.cif.gz"
                ),
            }
        )
    return sorted(rows, key=lambda row: row["assembly_id"])


def validate_coordinate_url(url: str) -> dict[str, Any]:
    """Validate a generated public coordinate URL without becoming an open proxy."""

    if not isinstance(url, str):
        raise CameoError("coordinate URL must be a string")
    prediction = _PREDICTION_COORDINATE_URL.fullmatch(url)
    if prediction:
        model = int(prediction.group(2))
        if model != int(prediction.group(3)):
            raise CameoError("CAMEO prediction coordinate model path is inconsistent")
        return {
            "kind": "prediction",
            "target_id": prediction.group(1),
            "model_index": model,
        }
    reference = _REFERENCE_COORDINATE_URL.fullmatch(url)
    if reference and int(reference.group(2)) >= 1:
        return {
            "kind": "reference",
            "target_id": reference.group(1),
            "assembly_id": int(reference.group(2)),
        }
    raise CameoError("coordinate URL is not an allow-listed CAMEO artifact")


def af3_import_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize public AF3/reference download provenance after Wednesday release."""

    availability = af3_availability(payload)
    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise CameoError("invalid decoded CAMEO payload")
    references = reference_urls(payload)
    predictions = payload.get("predictions", [])
    af3_rows = [
        row
        for row in predictions
        if isinstance(row, Mapping)
        and str(row.get("server_id", "")).replace("_3D", "") == CAMEO_AF3_SERVER_ID
    ]
    preferred_assembly = next(
        (
            row.get("complex_assembly_id")
            for row in af3_rows
            if isinstance(row.get("complex_assembly_id"), int)
        ),
        None,
    )
    return {
        "provider": "cameo",
        "method": "alphafold3",
        "provider_server_id": CAMEO_AF3_SERVER_ID,
        "provider_target_id": availability["target_id"],
        "week": target.get("week_id"),
        "pdb_id": target.get("pdbid"),
        "license": CAMEO_DATA_LICENSE,
        "source_page": target_url(availability["target_id"]),
        "models": [
            {"model_index": index, "url": url}
            for index, url in enumerate(availability["coordinate_urls"], start=1)
        ],
        "references": references,
        "preferred_reference_assembly": preferred_assembly,
        "public_model_1_advertised": bool(availability["advertised_models"]),
    }


def af3_availability(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize whether model 1 is advertised in a decoded public target page."""

    target = payload.get("target")
    predictions = payload.get("predictions")
    if not isinstance(target, Mapping) or not isinstance(predictions, list):
        raise CameoError("invalid decoded CAMEO payload")
    target_id = str(target.get("id", ""))
    target_url(target_id)
    rows = [
        dict(row)
        for row in predictions
        if isinstance(row, Mapping)
        and str(row.get("server_id", "")).replace("_3D", "") == CAMEO_AF3_SERVER_ID
    ]
    return {
        "target_id": target_id,
        "server_id": CAMEO_AF3_SERVER_ID,
        "advertised_models": sorted(
            {int(row["model"]) for row in rows if str(row.get("model", "")).isdigit()}
        ),
        "coordinate_urls": af3_prediction_urls(target_id),
    }


__all__ = [
    "CAMEO_AF3_SERVER_ID",
    "CAMEO_DATA_LICENSE",
    "CAMEO_ORIGIN",
    "CAMEO_SITEMAP_URL",
    "CameoError",
    "af3_availability",
    "af3_import_manifest",
    "af3_prediction_urls",
    "parse_sitemap_targets",
    "parse_target_page",
    "reference_urls",
    "target_url",
    "validate_coordinate_url",
]
