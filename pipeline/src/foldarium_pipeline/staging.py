"""Turn planned tasks into idempotent control-plane registration SQL.

A worker refuses to spend GPU time on a task that has no claimable
``prediction_runs`` row, and the schema rejects any drift between the stored
task payload and the duplicated searchable columns.  This module derives those
rows from an already-validated task so the two can never disagree.

It deliberately has no database connectivity.  The generated script is reviewed
and applied by an operator, which keeps the privileged credential out of the
pipeline entirely and makes registration auditable.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

from .contracts import SCHEMA_VERSION, canonical_json, validate_prediction_task

DEFAULT_EXECUTION_BACKEND = "local"
DEFAULT_SELECTION_POLICY_VERSION = "manual/v1"
DEFAULT_OPENFOLD3_CHECKPOINT = "openfold3-p2-155k"

_RUN_STATUSES = frozenset({"pending", "queued"})


class StagingError(ValueError):
    """Raised when staging rows cannot be derived safely."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def inline_target_uri(target_id: str, digest: str) -> str:
    """Return the smoke-only URI for a target carried inline in the task.

    Production targets must point at an immutable stored package.  This scheme
    is intentionally not resolvable so it cannot be mistaken for one.
    """

    return f"foldarium-inline://target/{target_id}/sha256/{digest}"


def _checkpoint_ref(task: Mapping[str, Any]) -> str:
    if task["method"] == "openfold3":
        return str(task["config"].get("checkpoint", DEFAULT_OPENFOLD3_CHECKPOINT))
    # Boltz downloads its own weights on first use, so the reproducible identity
    # is the package version rather than a named checkpoint.
    return f"boltz2-{task['method_version']}"


def build_run_row(
    task: Mapping[str, Any],
    *,
    adapter_version: str,
    execution_backend: str = DEFAULT_EXECUTION_BACKEND,
    input_uri: str | None = None,
    checkpoint_ref: str | None = None,
    max_attempts: int = 1,
    status: str = "pending",
) -> dict[str, Any]:
    """Derive one ``prediction_runs`` row from a validated task."""

    validated = validate_prediction_task(task)
    if not isinstance(adapter_version, str) or not adapter_version.strip():
        raise StagingError("adapter_version must be a non-empty string")
    if status not in _RUN_STATUSES:
        raise StagingError(f"staged status must be one of {sorted(_RUN_STATUSES)}")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise StagingError("max_attempts must be a positive integer")

    target = validated["target"]
    input_sha256 = _digest(target)
    return {
        "run_id": validated["task_id"],
        "target_id": target["target_id"],
        "task_payload": validated,
        "task_sha256": _digest(validated),
        "method": validated["method"],
        "method_version": validated["method_version"],
        "adapter_version": adapter_version.strip(),
        "method_configuration": validated["config"],
        "method_config_sha256": _digest(validated["config"]),
        "status": status,
        "max_attempts": max_attempts,
        "execution_backend": execution_backend,
        "image_ref": validated["container_image"],
        "checkpoint_ref": checkpoint_ref or _checkpoint_ref(validated),
        "input_uri": input_uri or inline_target_uri(target["target_id"], input_sha256),
        "input_sha256": input_sha256,
        "output_prefix": validated["output_uri_prefix"],
    }


def _target_summary(target: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "entities": [
            {"type": entity["type"], "chain_ids": list(entity["chain_ids"])}
            for entity in target["entities"]
        ],
    }


def build_staging_plan(
    tasks: Iterable[Mapping[str, Any]],
    *,
    adapter_version: str,
    campaign_name: str | None = None,
    campaign_source: str = "synthetic-smoke-test",
    selection_policy_version: str = DEFAULT_SELECTION_POLICY_VERSION,
    campaign_status: str = "predicting",
    **run_options: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Derive campaign, target, and run rows for a set of planned tasks."""

    task_list = list(tasks)
    if not task_list:
        raise StagingError("at least one task is required")

    campaigns: dict[str, dict[str, Any]] = {}
    targets: dict[str, dict[str, Any]] = {}
    runs: list[dict[str, Any]] = []
    seen_runs: set[str] = set()

    for task in task_list:
        run = build_run_row(task, adapter_version=adapter_version, **run_options)
        if run["run_id"] in seen_runs:
            raise StagingError(f"duplicate task {run['run_id']} in the staging set")
        seen_runs.add(run["run_id"])
        runs.append(run)

        validated = run["task_payload"]
        campaign_id = validated["campaign_id"]
        campaigns.setdefault(
            campaign_id,
            {
                "campaign_id": campaign_id,
                "name": campaign_name or campaign_id,
                "source": campaign_source,
                "selection_policy_version": selection_policy_version,
                "status": campaign_status,
            },
        )

        target = validated["target"]
        target_id = target["target_id"]
        existing = targets.get(target_id)
        if existing is not None:
            if existing["campaign_id"] != campaign_id:
                raise StagingError(
                    f"target {target_id} is staged under two campaigns; targets are "
                    "owned by exactly one campaign"
                )
            if existing["package_sha256"] != run["input_sha256"]:
                raise StagingError(
                    f"target {target_id} has two different normalized definitions"
                )
            continue
        targets[target_id] = {
            "target_id": target_id,
            "campaign_id": campaign_id,
            "source_id": target_id,
            "package_uri": run["input_uri"],
            "package_sha256": run["input_sha256"],
            "package_schema_version": 1,
            "input_summary": _target_summary(target),
        }

    return {
        "campaigns": list(campaigns.values()),
        "targets": list(targets.values()),
        "runs": runs,
    }


def _sql_text(value: str) -> str:
    if not isinstance(value, str):
        raise StagingError("SQL text values must be strings")
    if "\x00" in value:
        raise StagingError("SQL text values must not contain NUL")
    return "'" + value.replace("'", "''") + "'"


def _sql_json(value: Any) -> str:
    encoded = canonical_json(value)
    if "\\u0000" in encoded:
        raise StagingError("JSON values must not contain NUL")
    return _sql_text(encoded) + "::jsonb"


def _sql_int(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StagingError("SQL integer values must be integers")
    return str(value)


def _insert(table: str, rows: Sequence[Mapping[str, Any]], conflict: str) -> str:
    columns = list(rows[0].keys())
    rendered_rows = []
    for row in rows:
        if list(row.keys()) != columns:
            raise StagingError(f"inconsistent columns for {table}")
        values = []
        for column in columns:
            value = row[column]
            if isinstance(value, bool):
                raise StagingError(f"{table}.{column} must not be a boolean")
            if isinstance(value, int):
                values.append(_sql_int(value))
            elif isinstance(value, str):
                values.append(_sql_text(value))
            else:
                values.append(_sql_json(value))
        rendered_rows.append("  (" + ", ".join(values) + ")")
    return (
        f"insert into public.{table} (\n  "
        + ", ".join(columns)
        + "\n)\nvalues\n"
        + ",\n".join(rendered_rows)
        + f"\n{conflict};\n"
    )


def render_staging_sql(plan: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """Render an idempotent, single-transaction registration script.

    Campaign and target descriptions are refreshed on conflict, but an existing
    run is left untouched: re-running this script must never reset a run that is
    already leased, succeeded, or has recorded attempts.
    """

    for key in ("campaigns", "targets", "runs"):
        if not plan.get(key):
            raise StagingError(f"staging plan is missing {key}")

    sections = [
        "-- Generated by foldarium_pipeline.staging. Review before applying.\n"
        "-- Re-running is safe: existing prediction runs are never modified.\n",
        "begin;\n",
        _insert(
            "campaigns",
            plan["campaigns"],
            "on conflict (campaign_id) do update\n"
            "   set name = excluded.name,\n"
            "       source = excluded.source,\n"
            "       selection_policy_version = excluded.selection_policy_version",
        ),
        _insert(
            "targets",
            plan["targets"],
            "on conflict (target_id) do update\n"
            "   set package_uri = excluded.package_uri,\n"
            "       package_sha256 = excluded.package_sha256,\n"
            "       package_schema_version = excluded.package_schema_version,\n"
            "       input_summary = excluded.input_summary",
        ),
        _insert("prediction_runs", plan["runs"], "on conflict (run_id) do nothing"),
        "commit;\n",
    ]
    return "\n".join(sections)


__all__ = [
    "StagingError",
    "build_run_row",
    "build_staging_plan",
    "inline_target_uri",
    "render_staging_sql",
]
