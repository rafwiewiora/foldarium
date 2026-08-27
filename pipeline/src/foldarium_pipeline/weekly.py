"""Network/coordinator seam for the Saturday blind-prediction campaign.

This module is safe by default: :func:`build_public_weekly_plan` only downloads
public inputs and plans work.  The deployment hook registers rows only when
``FOLDARIUM_WEEKLY_REGISTER=1``; the deployment adapter has a second independent
``FOLDARIUM_WEEKLY_SUBMIT=1`` guard before any GPU call is spawned.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .cameo import (
    CAMEO_SITEMAP_URL,
    af3_availability,
    parse_sitemap_targets,
    parse_target_page,
    target_url,
)
from .contracts import canonical_json
from .intake import (
    ADAPTER_VERSION,
    WWPDB_NONPOLYMER_URL,
    WWPDB_SEQUENCE_URL,
    WeeklyPolicy,
    build_weekly_plan,
    parse_wwpdb_snapshot,
)
from .supabase import SupabaseCoordinator

USER_AGENT = "Foldarium weekly benchmark/0.2 (public scientific data intake)"
DEFAULT_FETCH_WORKERS = 12
MAX_PUBLIC_FILE_BYTES = 64 * 1024 * 1024

PublicFetcher = Callable[[str], bytes]


class WeeklyNotReady(RuntimeError):
    """Raised when the complete public Saturday input set is not available yet."""

    def __init__(self, message: str, *, availability: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.availability = dict(availability or {})


def _wwpdb_availability(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return log-safe evidence that both official prerelease files were decoded."""

    return {
        "wwpdb_sequence_rows": snapshot["sequence_rows"],
        "wwpdb_sequence_sha256": snapshot["sequence_sha256"],
        "wwpdb_nonpolymer_rows": snapshot["nonpolymer_rows"],
        "wwpdb_nonpolymer_sha256": snapshot["nonpolymer_sha256"],
        "wwpdb_entry_count": snapshot["entry_count"],
    }


def fetch_public(url: str, *, timeout_seconds: float = 60.0) -> bytes:
    """Fetch only an allow-listed wwPDB/CAMEO public URL with a size ceiling."""

    allowed = {WWPDB_SEQUENCE_URL, WWPDB_NONPOLYMER_URL, CAMEO_SITEMAP_URL}
    if url not in allowed:
        # ``target_url`` performs strict target-ID validation and returns the
        # canonical URL, preventing this helper becoming an arbitrary fetcher.
        target_id = url.rsplit("/", 1)[-1]
        if url != target_url(target_id):
            raise WeeklyNotReady("refusing a non-CAMEO/wwPDB public URL")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        response = urlopen(request, timeout=timeout_seconds)
        try:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_PUBLIC_FILE_BYTES:
                raise WeeklyNotReady(f"public input exceeds size limit: {url}")
            data = response.read(MAX_PUBLIC_FILE_BYTES + 1)
        finally:
            response.close()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise WeeklyNotReady(f"public input is unavailable: {url}") from exc
    if not data or len(data) > MAX_PUBLIC_FILE_BYTES:
        raise WeeklyNotReady(f"public input is empty or exceeds size limit: {url}")
    return data


def collect_wwpdb_inputs(
    release_date: date,
    *,
    fetcher: PublicFetcher = fetch_public,
) -> dict[str, Any]:
    """Fetch the two authoritative Saturday prerelease files without CAMEO."""

    if not isinstance(release_date, date):
        raise WeeklyNotReady("release_date must be a date")
    sequence = fetcher(WWPDB_SEQUENCE_URL)
    nonpolymer = fetcher(WWPDB_NONPOLYMER_URL)
    try:
        snapshot = parse_wwpdb_snapshot(sequence, nonpolymer)
    except ValueError as exc:
        raise WeeklyNotReady("wwPDB prerelease files could not be decoded") from exc
    readiness = _wwpdb_availability(snapshot)
    return {
        "snapshot": snapshot,
        "source_files": {
            "wwpdb_sequence": (sequence, "text/tab-separated-values"),
            "wwpdb_nonpolymer": (nonpolymer, "text/tab-separated-values"),
        },
        "availability": {
            "release_date": release_date.isoformat(),
            **readiness,
        },
    }


def collect_public_inputs(
    release_date: date,
    *,
    fetcher: PublicFetcher = fetch_public,
    fetch_workers: int = DEFAULT_FETCH_WORKERS,
) -> dict[str, Any]:
    """Download the complete replay source set, failing on partial target pages."""

    if not isinstance(release_date, date):
        raise WeeklyNotReady("release_date must be a date")
    if isinstance(fetch_workers, bool) or not isinstance(fetch_workers, int) or fetch_workers < 1:
        raise WeeklyNotReady("fetch_workers must be a positive integer")
    sequence = fetcher(WWPDB_SEQUENCE_URL)
    nonpolymer = fetcher(WWPDB_NONPOLYMER_URL)
    sitemap = fetcher(CAMEO_SITEMAP_URL)
    try:
        snapshot = parse_wwpdb_snapshot(sequence, nonpolymer)
    except ValueError as exc:
        raise WeeklyNotReady("wwPDB prerelease files could not be decoded") from exc
    readiness = _wwpdb_availability(snapshot)
    try:
        target_ids = parse_sitemap_targets(sitemap.decode("utf-8"), release_date)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WeeklyNotReady(
            "CAMEO sitemap could not be decoded", availability=readiness
        ) from exc
    if not target_ids:
        raise WeeklyNotReady(
            f"CAMEO has not advertised targets for {release_date.isoformat()} yet",
            availability={**readiness, "cameo_target_pages": 0},
        )

    pages: dict[str, bytes] = {}
    failures: dict[str, str] = {}

    def fetch_one(target_id: str) -> tuple[str, bytes]:
        return target_id, fetcher(target_url(target_id))

    # Injected single-threaded fetchers make deterministic unit tests easy; the
    # real ~hundreds-of-pages Saturday crawl uses bounded concurrency.
    if fetch_workers == 1:
        for target_id in target_ids:
            try:
                key, value = fetch_one(target_id)
                pages[key] = value
            except Exception as exc:  # summarized without leaking response bodies
                failures[target_id] = type(exc).__name__
    else:
        with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
            futures = {executor.submit(fetch_one, target_id): target_id for target_id in target_ids}
            for future in as_completed(futures):
                target_id = futures[future]
                try:
                    key, value = future.result()
                    pages[key] = value
                except Exception as exc:  # summarized without leaking response bodies
                    failures[target_id] = type(exc).__name__

    payloads: list[dict[str, Any]] = []
    for target_id in target_ids:
        raw = pages.get(target_id)
        if raw is None:
            continue
        try:
            payload = parse_target_page(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            failures[target_id] = type(exc).__name__
            continue
        if payload["target"]["id"] != target_id:
            failures[target_id] = "TargetIdentityMismatch"
            continue
        payloads.append(payload)
    if failures or len(payloads) != len(target_ids):
        sample = ", ".join(sorted(failures)[:5])
        raise WeeklyNotReady(
            f"CAMEO target pages are incomplete ({len(payloads)}/{len(target_ids)} decoded; "
            f"failures: {sample or 'unknown'})",
            availability={
                **readiness,
                "cameo_advertised_targets": len(target_ids),
                "cameo_target_pages": len(payloads),
            },
        )

    snapshot.update(
        {
            "cameo_sitemap_url": CAMEO_SITEMAP_URL,
            "cameo_sitemap_sha256": hashlib.sha256(sitemap).hexdigest(),
            "cameo_target_count": len(target_ids),
            "cameo_pages_sha256": hashlib.sha256(
                canonical_json(
                    {target_id: hashlib.sha256(pages[target_id]).hexdigest() for target_id in target_ids}
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    # One gzip bundle keeps replay provenance to four immutable objects instead
    # of hundreds of small Storage writes.
    page_bundle = gzip.compress(
        canonical_json(
            {target_id: pages[target_id].decode("utf-8") for target_id in target_ids}
        ).encode("utf-8"),
        mtime=0,
    )
    return {
        "snapshot": snapshot,
        "payloads": payloads,
        "source_files": {
            "wwpdb_sequence": (sequence, "text/tab-separated-values"),
            "wwpdb_nonpolymer": (nonpolymer, "text/tab-separated-values"),
            "cameo_sitemap": (sitemap, "application/xml"),
            "cameo_target_pages": (page_bundle, "application/gzip"),
        },
        "availability": {
            "release_date": release_date.isoformat(),
            "target_pages": len(payloads),
            "af3_advertised_targets": sum(
                bool(af3_availability(payload)["advertised_models"]) for payload in payloads
            ),
        },
    }


def build_public_weekly_plan(
    release_date: date,
    *,
    policy: WeeklyPolicy | None = None,
    output_prefix: str = "supabase://foldarium-predictions/runs",
    fetcher: PublicFetcher = fetch_public,
    fetch_workers: int = DEFAULT_FETCH_WORKERS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch public sources and return ``(plan, replay_inputs)`` without writes."""

    if isinstance(fetch_workers, bool) or not isinstance(fetch_workers, int) or fetch_workers < 1:
        raise WeeklyNotReady("fetch_workers must be a positive integer")
    inputs = collect_wwpdb_inputs(release_date, fetcher=fetcher)
    plan = build_weekly_plan(
        release_date=release_date,
        ww_pdb_snapshot=inputs["snapshot"],
        output_prefix=output_prefix,
        policy=policy,
    )
    return plan, inputs


def _environment_release_date() -> date:
    override = os.environ.get("FOLDARIUM_RELEASE_DATE")
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError as exc:
            raise WeeklyNotReady("FOLDARIUM_RELEASE_DATE must be an ISO date") from exc
    today = datetime.now(timezone.utc).date()
    if today.weekday() != 5:
        raise WeeklyNotReady(
            "automatic intake only runs on Saturday UTC; set FOLDARIUM_RELEASE_DATE for a replay"
        )
    return today


def deployment_weekly_hook() -> Mapping[str, Any]:
    """Plan, optionally register, and return guarded task metadata."""

    release_date = _environment_release_date()
    try:
        max_targets = int(os.environ.get("FOLDARIUM_WEEKLY_MAX_TARGETS", "8"))
    except ValueError as exc:
        raise WeeklyNotReady("FOLDARIUM_WEEKLY_MAX_TARGETS must be an integer") from exc
    bucket = os.environ.get("FOLDARIUM_STORAGE_BUCKET", "foldarium-predictions")
    gpu_class = os.environ.get("FOLDARIUM_WEEKLY_GPU_CLASS") or None
    coordinator: SupabaseCoordinator | None = None
    register = os.environ.get("FOLDARIUM_WEEKLY_REGISTER") == "1"
    campaign_id = f"wwpdb-{release_date.isoformat()}"
    if register:
        coordinator = SupabaseCoordinator.from_env()
        if coordinator.weekly_campaign_exists(campaign_id):
            return {
                "tasks": [],
                "status": "already-registered",
                "release_date": release_date.isoformat(),
                "campaign_id": campaign_id,
                "registration": {"status": "already-registered"},
            }
    try:
        plan, inputs = build_public_weekly_plan(
            release_date,
            policy=WeeklyPolicy(max_targets=max_targets, gpu_class=gpu_class),
            output_prefix=f"supabase://{bucket}/runs",
        )
    except WeeklyNotReady as exc:
        return {
            "tasks": [],
            "status": "waiting-for-inputs",
            "release_date": release_date.isoformat(),
            "campaign_id": campaign_id,
            "reason": str(exc),
            "availability": exc.availability,
            "registration": {"status": "not-requested"},
        }
    if not plan["tasks"]:
        raise WeeklyNotReady("the complete public intake contains no eligible bounded targets")

    registration: Any = {"status": "not-requested"}
    if register:
        assert coordinator is not None
        registration = coordinator.register_weekly_plan(
            plan,
            inputs["source_files"],
            adapter_version=ADAPTER_VERSION,
            max_attempts=1,
        )
    target_summaries = []
    for target in plan["targets"]:
        ligand = target.get("metadata", {}).get("selected_ligand", {})
        target_tasks = [
            task for task in plan["tasks"] if task["target"]["target_id"] == target["target_id"]
        ]
        target_summaries.append(
            {
                "target_id": target["target_id"],
                "ligand": ligand.get("component_id"),
                "ligand_heavy_atoms": ligand.get("heavy_atoms"),
                "polymer_entities": sum(
                    entity["type"] != "ligand" for entity in target["entities"]
                ),
                "polymer_residues": sum(
                    len(entity.get("sequence", ""))
                    for entity in target["entities"]
                    if entity["type"] != "ligand"
                ),
                "gpu_classes": {
                    task["method"]: task["resources"]["gpu_class"] for task in target_tasks
                },
            }
        )
    return {
        "tasks": plan["tasks"],
        "plan_sha256": plan["plan_sha256"],
        "budget": plan["budget"],
        "selected_targets": target_summaries,
        "availability": inputs["availability"],
        "registration": registration,
    }


__all__ = [
    "DEFAULT_FETCH_WORKERS",
    "MAX_PUBLIC_FILE_BYTES",
    "USER_AGENT",
    "WeeklyNotReady",
    "build_public_weekly_plan",
    "collect_public_inputs",
    "collect_wwpdb_inputs",
    "fetch_public",
    "deployment_weekly_hook",
]
