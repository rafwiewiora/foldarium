"""Safe cache-metadata backfill for published Weekly structure objects."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .quiz import manifest_sha256
from .supabase import IMMUTABLE_PUBLIC_CACHE_CONTROL, SupabasePublicationError

_CONTENT_PATH = re.compile(r"sha256/([0-9a-f]{2})/([0-9a-f]{64})")


def verified_public_object_inventory(
    manifest: Mapping[str, Any],
    *,
    round_id: str,
    expected_manifest_sha256: str,
    public_bucket: str,
) -> list[dict[str, str]]:
    """Extract digest-bound public objects from one verified blind manifest."""

    if manifest.get("round_id") != round_id:
        raise SupabasePublicationError("blind manifest belongs to a different round")
    if manifest_sha256(manifest) != expected_manifest_sha256:
        raise SupabasePublicationError("blind manifest SHA-256 does not match the round")

    uris: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            media_type = value.get("media_type")
            if media_type is not None and media_type != "chemical/x-pdb":
                raise SupabasePublicationError(
                    "cache backfill supports only the current PDB manifest contract"
                )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and value.startswith("supabase://"):
            parsed = urlsplit(value)
            object_path = parsed.path.lstrip("/")
            match = _CONTENT_PATH.fullmatch(object_path)
            if (
                parsed.netloc != public_bucket
                or parsed.query
                or parsed.fragment
                or match is None
                or match.group(1) != match.group(2)[:2]
            ):
                raise SupabasePublicationError(
                    "blind manifest contains an unexpected public object URI"
                )
            uris.add(value)

    visit(manifest)
    if not uris:
        raise SupabasePublicationError("blind manifest contains no public structure objects")
    return [
        {
            "object_uri": uri,
            "sha256": uri.rsplit("/", 1)[-1],
            "media_type": "chemical/x-pdb",
        }
        for uri in sorted(uris)
    ]


def backfill_immutable_cache(
    coordinator: Any,
    inventory: list[Mapping[str, str]],
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Verify every object digest, then optionally re-upload identical bytes."""

    with tempfile.TemporaryDirectory(prefix="foldarium-cache-backfill-") as temporary:
        verified: list[tuple[Mapping[str, str], Path]] = []
        for item in inventory:
            content = coordinator.download_content_object(
                item["object_uri"],
                expected_sha256=item["sha256"],
            )
            verified_path = Path(temporary, item["sha256"])
            verified_path.write_bytes(content)
            verified.append((item, verified_path))

        if apply:
            for item, verified_path in verified:
                coordinator.replace_content_object(
                    item["object_uri"],
                    verified_path.read_bytes(),
                    item["media_type"],
                    cache_control=IMMUTABLE_PUBLIC_CACHE_CONTROL,
                )

    return {
        "mode": "apply" if apply else "dry-run",
        "verified_objects": len(verified),
        "updated_objects": len(verified) if apply else 0,
        "cache_control": IMMUTABLE_PUBLIC_CACHE_CONTROL,
    }
