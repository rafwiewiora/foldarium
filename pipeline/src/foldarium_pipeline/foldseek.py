"""Small fail-closed client for the public Foldseek structure-search service.

The historical Foldarium novelty analysis used this public API but imported its
client from an absolute path in another checkout.  Keeping the HTTP boundary in
the portable pipeline makes the novelty calculation runnable locally or in a
CPU-only worker.  Scientific classification remains in the caller:
this module only submits a protein structure, returns Foldseek alignments, and
retrieves authoritative PDB release dates in batches.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FOLDSEEK_API = "https://search.foldseek.com/api"
RCSB_GRAPHQL_API = "https://data.rcsb.org/graphql"
USER_AGENT = "Foldarium novelty/0.2 (public scientific data)"
MAX_QUERY_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_RELEASE_IDS = 100

Opener = Callable[..., Any]


class FoldseekError(RuntimeError):
    """Raised when a remote response is unavailable or structurally invalid."""


def _json_request(
    url: str,
    *,
    opener: Opener = urlopen,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout_seconds: float = 60.0,
) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        response = opener(request, timeout=timeout_seconds)
        try:
            body = response.read(MAX_JSON_BYTES + 1)
        finally:
            response.close()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        raise FoldseekError("public structure-search request failed") from None
    if not body or len(body) > MAX_JSON_BYTES:
        raise FoldseekError("public structure-search response is empty or too large")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoldseekError("public structure-search response is not JSON") from exc


def _multipart_query(content: bytes) -> tuple[bytes, str]:
    boundary = "foldarium-" + uuid.uuid4().hex
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            b'Content-Disposition: form-data; name="q"; filename="query.pdb"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            content,
            b"\r\n",
        ]
    )
    field("mode", "3diaa")
    field("database[]", "pdb100")
    field("iterativesearch", "false")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def submit(
    pdb_path: str | Path,
    *,
    opener: Opener = urlopen,
    timeout_seconds: float = 60.0,
) -> tuple[str, str]:
    """Submit one PDB query against ``pdb100`` and return ``(ticket, status)``."""

    path = Path(pdb_path)
    content = path.read_bytes()
    if not content or len(content) > MAX_QUERY_BYTES:
        raise FoldseekError("Foldseek query is empty or too large")
    body, content_type = _multipart_query(content)
    value = _json_request(
        FOLDSEEK_API + "/ticket",
        opener=opener,
        data=body,
        content_type=content_type,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(value, Mapping):
        raise FoldseekError("Foldseek submission returned an invalid ticket")
    ticket = value.get("id")
    status = value.get("status")
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", ticket):
        raise FoldseekError("Foldseek submission returned an invalid ticket")
    if not isinstance(status, str):
        raise FoldseekError("Foldseek submission omitted its status")
    return ticket, status.upper()


def poll(
    ticket: str,
    *,
    every: float = 3.0,
    cap: float = 180.0,
    opener: Opener = urlopen,
) -> str:
    """Poll an existing ticket until it is complete, terminal, or times out."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", ticket):
        raise FoldseekError("invalid Foldseek ticket")
    if every <= 0 or cap <= 0:
        raise FoldseekError("Foldseek poll intervals must be positive")
    deadline = time.monotonic() + cap
    while True:
        value = _json_request(
            f"{FOLDSEEK_API}/ticket/{ticket}", opener=opener, timeout_seconds=60
        )
        status = value.get("status") if isinstance(value, Mapping) else None
        if not isinstance(status, str):
            raise FoldseekError("Foldseek poll returned an invalid status")
        status = status.upper()
        if status in {"COMPLETE", "ERROR", "MAINTENANCE", "UNKNOWN"}:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "TIMEOUT"
        time.sleep(min(every, remaining))


def fetch_result(
    ticket: str, query_index: int = 0, *, opener: Opener = urlopen
) -> dict[str, Any]:
    """Fetch one completed query's JSON alignments."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", ticket):
        raise FoldseekError("invalid Foldseek ticket")
    if isinstance(query_index, bool) or not isinstance(query_index, int) or query_index < 0:
        raise FoldseekError("query_index must be a non-negative integer")
    value = _json_request(
        f"{FOLDSEEK_API}/result/{ticket}/{query_index}",
        opener=opener,
        timeout_seconds=120,
    )
    if not isinstance(value, Mapping):
        raise FoldseekError("Foldseek result is not an object")
    return dict(value)


def parse_pdbid(target: str) -> str | None:
    """Extract a legacy four-character PDB ID from a ``pdb100`` target label."""

    if not isinstance(target, str):
        return None
    match = re.match(r"^(?:pdb\|)?([0-9][A-Za-z0-9]{3})(?:[_|.\s-]|$)", target.strip())
    return match.group(1).upper() if match else None


def release_dates(
    pdb_ids: Iterable[str], *, opener: Opener = urlopen
) -> dict[str, str | None]:
    """Return initial release dates for PDB IDs using batched RCSB GraphQL calls."""

    identifiers = sorted({str(value).upper() for value in pdb_ids})
    if any(not re.fullmatch(r"[0-9][A-Z0-9]{3}", value) for value in identifiers):
        raise FoldseekError("release-date lookup contains an invalid PDB ID")
    dates: dict[str, str | None] = {value: None for value in identifiers}
    query = (
        "query FoldariumReleaseDates($ids: [String!]!) { "
        "entries(entry_ids: $ids) { rcsb_id rcsb_accession_info { initial_release_date } } }"
    )
    for offset in range(0, len(identifiers), MAX_RELEASE_IDS):
        batch = identifiers[offset : offset + MAX_RELEASE_IDS]
        payload = json.dumps(
            {"query": query, "variables": {"ids": batch}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        value = _json_request(
            RCSB_GRAPHQL_API,
            opener=opener,
            data=payload,
            content_type="application/json",
            timeout_seconds=60,
        )
        rows = value.get("data", {}).get("entries") if isinstance(value, Mapping) else None
        if not isinstance(rows, list):
            raise FoldseekError("RCSB release-date response is invalid")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            pdb_id = str(row.get("rcsb_id", "")).upper()
            accession = row.get("rcsb_accession_info")
            released = accession.get("initial_release_date") if isinstance(accession, Mapping) else None
            if pdb_id in dates and (released is None or isinstance(released, str)):
                dates[pdb_id] = released
    return dates


def release_date(pdb_id: str, *, opener: Opener = urlopen) -> str | None:
    """Compatibility wrapper for the historical single-ID client."""

    normalized = str(pdb_id).upper()
    return release_dates([normalized], opener=opener)[normalized]


__all__ = [
    "FOLDSEEK_API",
    "FoldseekError",
    "fetch_result",
    "parse_pdbid",
    "poll",
    "release_date",
    "release_dates",
    "submit",
]
