"""Deterministic, no-submit planning for bounded recent-week backfills."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from .contracts import canonical_json
from .intake import IntakeError, WeeklyPolicy, build_method_tasks
from .sizing import SizingError, resolve_gpu_class

BACKFILL_SCHEMA_VERSION = "foldarium.historical-backfill/v1"


class BackfillError(ValueError):
    """Raised when a historical plan is unbounded or lacks replay provenance."""


def _signature(target: Mapping[str, Any]) -> str:
    polymers = sorted(
        (entity["type"], entity["sequence"])
        for entity in target["entities"]
        if entity["type"] != "ligand"
    )
    return hashlib.sha256(canonical_json(polymers).encode("utf-8")).hexdigest()


def _priority(candidate: Mapping[str, Any]) -> tuple[int, str]:
    target = candidate["prediction_target"]
    label = str(target.get("metadata", {}).get("cameo_label") or "").lower()
    label_priority = {"ligand": 0, "hard": 1, "medium": 2, "easy": 3}.get(label, 4)
    return label_priority, hashlib.sha256(str(target["target_id"]).encode("ascii")).hexdigest()


def build_backfill_plan(
    candidates: Iterable[Mapping[str, Any]],
    *,
    start_week: date,
    end_week: date,
    source_snapshot_sha256: str,
    output_prefix: str,
    max_targets_per_week: int = 2,
    methods: Iterable[str] = ("openfold3", "boltz2"),
    diffusion_samples: int = 5,
    timeout_seconds: int = 20 * 60,
    msa_mode: str = "server",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Plan recent released targets; never registers or launches them."""

    if not isinstance(start_week, date) or not isinstance(end_week, date) or start_week > end_week:
        raise BackfillError("start_week/end_week must be ordered dates")
    if start_week.weekday() != 5 or end_week.weekday() != 5:
        raise BackfillError("start_week/end_week must be Saturdays")
    if (
        not isinstance(source_snapshot_sha256, str)
        or len(source_snapshot_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_snapshot_sha256)
    ):
        raise BackfillError("source_snapshot_sha256 must be a lowercase SHA-256")
    if isinstance(max_targets_per_week, bool) or not isinstance(max_targets_per_week, int) or max_targets_per_week < 1:
        raise BackfillError("max_targets_per_week must be positive")
    selected_methods = tuple(methods)
    policy = WeeklyPolicy(
        max_targets=max_targets_per_week,
        diffusion_samples=diffusion_samples,
        timeout_seconds=timeout_seconds,
        msa_mode=msa_mode,
        protein_only=True,
    ).validate()
    campaign_id = f"cameo-backfill-{start_week.isoformat()}-{end_week.isoformat()}"
    by_week: dict[str, list[Mapping[str, Any]]] = {}
    skipped: list[dict[str, str]] = []
    for raw in candidates:
        candidate = dict(raw)
        week = candidate.get("week")
        target = candidate.get("prediction_target")
        if not isinstance(week, str) or not isinstance(target, Mapping):
            raise BackfillError("every candidate requires week and prediction_target")
        try:
            parsed_week = date.fromisoformat(week)
        except ValueError as exc:
            raise BackfillError("candidate week must be an ISO date") from exc
        if not start_week <= parsed_week <= end_week:
            continue
        polymers = {entity["type"] for entity in target["entities"] if entity["type"] != "ligand"}
        if polymers != {"protein"}:
            skipped.append({"target_id": str(target["target_id"]), "reason": "unsupported-nonprotein-polymer"})
            continue
        try:
            resolve_gpu_class(target, {"msa_mode": msa_mode})
        except (SizingError, ValueError) as exc:
            skipped.append({"target_id": str(target["target_id"]), "reason": str(exc)})
            continue
        by_week.setdefault(week, []).append(candidate)

    selected: list[Mapping[str, Any]] = []
    for week in sorted(by_week):
        seen: set[str] = set()
        diversified: list[Mapping[str, Any]] = []
        for candidate in sorted(by_week[week], key=_priority):
            signature = _signature(candidate["prediction_target"])
            if signature in seen:
                skipped.append(
                    {"target_id": str(candidate["target_id"]), "reason": "duplicate-polymer-complex"}
                )
                continue
            seen.add(signature)
            diversified.append(candidate)
        selected.extend(diversified[:max_targets_per_week])
        skipped.extend(
            {"target_id": str(candidate["target_id"]), "reason": "weekly-target-cap"}
            for candidate in diversified[max_targets_per_week:]
        )

    targets = [dict(candidate["prediction_target"]) for candidate in selected]
    tasks = [
        task
        for target in targets
        for task in build_method_tasks(
            target, campaign_id, output_prefix, policy, methods=selected_methods
        )
    ]
    created = generated_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        raise BackfillError("generated_at must be timezone-aware")
    plan = {
        "schema_version": BACKFILL_SCHEMA_VERSION,
        "campaign": {
            "campaign_id": campaign_id,
            "name": f"Foldarium CAMEO backfill {start_week.isoformat()} to {end_week.isoformat()}",
            "source": "released public CAMEO targets",
            "start_week": start_week.isoformat(),
            "end_week": end_week.isoformat(),
            "source_snapshot_sha256": source_snapshot_sha256,
            "configuration": {
                "max_targets_per_week": max_targets_per_week,
                "methods": list(selected_methods),
                "diffusion_samples": diffusion_samples,
                "timeout_seconds": timeout_seconds,
                "msa_mode": msa_mode,
                "protein_only": True,
            },
        },
        "targets": targets,
        "tasks": tasks,
        "skipped": sorted(skipped, key=lambda row: (row["target_id"], row["reason"])),
        "budget": {
            "selected_targets": len(targets),
            "gpu_tasks": len(tasks),
            "maximum_gpu_seconds": len(tasks) * timeout_seconds,
            "gpu_classes": {
                name: sum(task["resources"]["gpu_class"] == name for task in tasks)
                for name in sorted({task["resources"]["gpu_class"] for task in tasks})
            },
        },
        "generated_at": created.astimezone(timezone.utc).isoformat(),
    }
    identity = {key: value for key, value in plan.items() if key != "generated_at"}
    plan["plan_sha256"] = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return plan


__all__ = ["BACKFILL_SCHEMA_VERSION", "BackfillError", "build_backfill_plan"]
