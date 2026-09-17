"""Thin Modal deployment adapter for Foldarium prediction tasks.

This module intentionally contains no campaign logic, database schema, or model
input translation.  Those live in ``foldarium_pipeline`` so the same task can be
executed locally, on Modal, or in a GCP job.

Install Modal only in the deployment environment, then use the reviewed wrapper::

    python3 pipeline/deploy/deploy_profile.py

The module is importable without Modal installed so local tests do not need the
deployment SDK.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import tempfile
import importlib
import json
import math
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any

from foldarium_pipeline.weekly_intake_recovery import (
    DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS,
    validate_intake_replay_release_date,
)
from foldarium_pipeline.weekly_lifecycle_journal import (
    DEFAULT_JOURNAL_ROOT,
    LIFECYCLE_JOURNAL_VOLUME_NAME,
    commit_journal_volume,
    read_lifecycle_journal_events,
    run_with_lifecycle_journal,
)

try:  # Modal is an optional deployment dependency, not a core dependency.
    import modal
except ModuleNotFoundError:  # pragma: no cover - exercised without deployment extras
    modal = None  # type: ignore[assignment]


APP_NAME = "foldarium-predictions"
WEEKLY_SCORING_APP_NAME = "foldarium-weekly-scoring"
WEEKLY_SCORING_FUNCTION_NAME = "score_pose"
WEEKLY_SCORING_MAX_WORKERS = 8
WEEKLY_ASSEMBLY_TARGET_WORKERS = 8
WEEKLY_ASSEMBLY_TIMEOUT_SECONDS = 45 * 60
PUBLIC_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DEPLOYMENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
DEPLOYMENT_CONFIG_SHA256_ENV = "FOLDARIUM_DEPLOYMENT_CONFIG_SHA256"


def _require_reviewed_deployment() -> None:
    """Refuse a direct deploy that would silently discard reviewed gates."""

    if "deploy" not in sys.argv[1:]:
        return
    digest = os.environ.get(DEPLOYMENT_CONFIG_SHA256_ENV, "")
    if not DEPLOYMENT_DIGEST.fullmatch(digest):
        raise RuntimeError(
            "direct deployment is disabled; use pipeline/deploy/deploy_profile.py"
        )


_require_reviewed_deployment()


def _weekly_public_bucket(explicit: str | None = None) -> str:
    value = explicit if explicit is not None else os.environ.get(
        "FOLDARIUM_PUBLIC_QUIZ_BUCKET"
    )
    if not isinstance(value, str) or not PUBLIC_BUCKET_NAME.fullmatch(value):
        raise ValueError(
            "public quiz bucket must be an explicit safe Storage bucket name"
        )
    return value


def _score_weekly_choices_concurrently(
    choice_scorer: Any,
    requests: tuple[Mapping[str, Any], ...],
) -> tuple[Mapping[str, Any], ...]:
    """Score an ordered batch with a hard eight-call concurrency ceiling."""

    if not requests:
        return ()
    results: list[Mapping[str, Any] | None] = [None] * len(requests)
    with ThreadPoolExecutor(
        max_workers=min(WEEKLY_SCORING_MAX_WORKERS, len(requests)),
        thread_name_prefix="foldarium-pose-score",
    ) as executor:
        futures = {
            executor.submit(choice_scorer, **dict(request)): index
            for index, request in enumerate(requests)
        }
        try:
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    if any(result is None for result in results):
        raise RuntimeError("weekly pose scoring batch returned an incomplete result set")
    return tuple(result for result in results if result is not None)

# Official OpenFold3 0.4-pixi (OpenFold3 0.4.4) OCI index. Keep this immutable;
# upgrading the model runtime should be an explicit, reviewed change with a new
# digest and matching task provenance.
OPENFOLD3_IMAGE_REF = (
    "docker.io/openfoldconsortium/openfold3:0.4-pixi@"
    "sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec"
)

BOLTZ2_VERSION = "2.2.1"
# Upstream 2.2.1 exposes PAE/PDE flags but ignores them in Boltz-2. Pin the
# minimal upstream fix so --no_write_full_pae/--no_write_full_pde actually avoid
# materializing the quadratic matrices. This commit is 2.2.1 plus that bounded
# three-file fix; changing it requires explicit runtime/provenance review.
BOLTZ2_NO_FULL_ERRORS_COMMIT = "43f36705508d1a85bd0236370434d7bdfd94b169"
BOLTZ2_PACKAGE = (
    "boltz[cuda] @ git+https://github.com/jwohlwend/boltz.git@"
    f"{BOLTZ2_NO_FULL_ERRORS_COMMIT}"
)

OPENFOLD3_CONTROL_PYTHON = "3.12"
OPENFOLD3_ACTIVATE = "/opt/activate.sh"

WORK_ROOT = "/tmp/foldarium"

# Translation from the core's backend-neutral accelerator classes to Modal's
# names. The sizing decision itself belongs to foldarium_pipeline.sizing so every
# backend makes it identically; only this mapping is Modal-specific, and a GCP
# adapter supplies its own.
MODAL_GPU_BY_CLASS = {
    "l4": "L4",
    "a100-40gb": "A100-40GB",
    "l40s": "L40S",
    "a100-80gb": "A100-80GB",
}

# Host resources scale with the accelerator so a large card is not starved by a
# small loader, and a small card does not reserve a large machine.
MODAL_HOST_BY_CLASS = {
    "l4": (4.0, 16384),
    "a100-40gb": (8.0, 32768),
    "l40s": (4.0, 16384),
    "a100-80gb": (8.0, 65536),
}

# Outer container budget for GPU work. The method subprocess is already capped by
# the task's ``resources.timeout_seconds``, but a container can also stall outside
# that subprocess: image pull, checkpoint reload, cache commit, or publication. On
# a credit-limited account those phases must not be able to hold a GPU for hours,
# so this ceiling is deliberately tight. Five-sample L4 tasks receive a
# thirty-minute command budget; the additional five minutes lets the worker
# validate and durably publish completed outputs without leaving a stale lease.
GPU_FUNCTION_TIMEOUT_SECONDS = 35 * 60
LEASE_GRACE_SECONDS = 15 * 60
OPENFOLD_CACHE_ROOT = "/cache/openfold"
BOLTZ_CACHE_ROOT = "/cache/boltz"


def _bounded_prediction_concurrency() -> int:
    """Resolve an operator-reviewed per-method GPU concurrency ceiling."""

    raw = os.environ.get("FOLDARIUM_PREDICTION_MAX_CONTAINERS", "1")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("FOLDARIUM_PREDICTION_MAX_CONTAINERS must be an integer") from exc
    if not 1 <= value <= 12:
        raise ValueError("FOLDARIUM_PREDICTION_MAX_CONTAINERS must be from 1 to 12")
    return value


PREDICTION_MAX_CONTAINERS = _bounded_prediction_concurrency()

# Saturday intake follows the lifecycle documented at the repository root. The
# cron belongs to this adapter, not to the provider-neutral pipeline core. CAMEO
# publication can lag the nominal 03:00 UTC boundary, so the deployed poller
# checks every 15 minutes through 12:45. Once a campaign exists in Supabase the
# hook exits before touching the public feeds or spawning any work.
WEEKLY_CRON_UTC = os.environ.get("FOLDARIUM_WEEKLY_CRON", "*/15 3-12 * * 6")
WEEKLY_HOOK_ENV = "FOLDARIUM_WEEKLY_HOOK"
PUBLIC_QUIZ_BUCKET_ENV = "FOLDARIUM_PUBLIC_QUIZ_BUCKET"
WEEKLY_CRON_ENABLED = os.environ.get("FOLDARIUM_ENABLE_WEEKLY_CRON") == "1"
NEXTWEEKLY_CRON_UTC = os.environ.get(
    "FOLDARIUM_NEXTWEEKLY_CRON", "5 * * * 6,0,1"
)
NEXTWEEKLY_CRON_ENABLED = (
    os.environ.get("FOLDARIUM_ENABLE_NEXTWEEKLY_CRON") == "1"
)
NEXTWEEKLY_ENVIRONMENT = os.environ.get(
    "FOLDARIUM_NEXTWEEKLY_ENVIRONMENT", "preview"
)
NEXTWEEKLY_INCLUDE_POSE_METRICS = (
    os.environ.get("FOLDARIUM_NEXTWEEKLY_INCLUDE_POSE_METRICS") == "1"
)
NEXTWEEKLY_ROUND_VERSION = os.environ.get(
    "FOLDARIUM_NEXTWEEKLY_ROUND_VERSION", "v2"
)
NEXTWEEKLY_RETRY_BATCH_SIZE = 80
NEXTWEEKLY_ORIGINAL_GPU_COMMAND_BUDGET_SECONDS = 40 * 60 * 60
NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS = 30 * 60
NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS = 75 * 60
NEXTWEEKLY_MAX_RETRY_RESERVATION_SECONDS = (
    NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
)
NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS = 5 * 60
# Modal's published per-second accelerator + reviewed host allocations. The
# campaign ceiling covers the original 40-hour L4 budget plus one conservatively
# reserved retry for every possible run in a 40-target, two-method campaign.
NEXTWEEKLY_L4_RATE_USD_PER_SECOND = 0.00030992
NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND = 0.00075884
NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS = {
    "l4": NEXTWEEKLY_L4_RATE_USD_PER_SECOND,
    "a100-40gb": NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND,
}
NEXTWEEKLY_MAX_RETRY_RESERVATION_COST_USD = max(
    NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
    * NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND,
    NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
    * NEXTWEEKLY_L4_RATE_USD_PER_SECOND,
)
NEXTWEEKLY_GPU_COMMAND_BUDGET_SECONDS = (
    NEXTWEEKLY_ORIGINAL_GPU_COMMAND_BUDGET_SECONDS
    + NEXTWEEKLY_RETRY_BATCH_SIZE * NEXTWEEKLY_MAX_RETRY_RESERVATION_SECONDS
)
NEXTWEEKLY_GPU_COST_BUDGET_USD = (
    40 * 60 * 60 * NEXTWEEKLY_L4_RATE_USD_PER_SECOND
    + NEXTWEEKLY_RETRY_BATCH_SIZE
    * NEXTWEEKLY_MAX_RETRY_RESERVATION_COST_USD
)
# These legacy rows were reviewed against bounded Modal logs on 2026-08-15.
# Preserve their known upgraded retry tier; unclassified attempt-1 failures now
# receive the same-resource `repeat_once` policy instead.
NEXTWEEKLY_REVIEWED_LEGACY_RETRIES = {
    "run_fe3f5b2f13d64c508aa61f39": {
        "target_id": "31ZN",
        "method": "boltz2",
        "source_error_code": "output_validation_failed",
        "retry_kind": "gpu_out_of_memory",
    },
    "run_ebb8012256ebff410610bbd3": {
        "target_id": "9S7U",
        "method": "openfold3",
        "source_error_code": "output_validation_failed",
        "retry_kind": "gpu_out_of_memory",
    },
    "run_62f75d944367889691bfc897": {
        "target_id": "32QB",
        "method": "openfold3",
        "source_error_code": "timeout",
        "retry_kind": "msa_generation_timeout",
    },
}
WEDNESDAY_REVEAL_CRON_UTC = os.environ.get(
    "FOLDARIUM_WEDNESDAY_REVEAL_CRON", "5 0-5 * * 3"
)
WEDNESDAY_REVEAL_ENABLED = os.environ.get("FOLDARIUM_ENABLE_WEDNESDAY_REVEAL") == "1"
WEDNESDAY_REVEAL_PUBLISH_ENV = "FOLDARIUM_WEDNESDAY_REVEAL_PUBLISH"
WEEKLY_RETROSPECTIVE_CRON_UTC = os.environ.get(
    "FOLDARIUM_WEEKLY_RETROSPECTIVE_CRON", "15 0-5 * * 3"
)
WEEKLY_RETROSPECTIVE_ENABLED = (
    os.environ.get("FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE") == "1"
)
WEEKLY_RETROSPECTIVE_PUBLICATION_CRON_UTC = os.environ.get(
    "FOLDARIUM_WEEKLY_RETROSPECTIVE_PUBLICATION_CRON", "45 0-5 * * 3"
)
WEEKLY_RETROSPECTIVE_PUBLICATION_ENABLED = (
    os.environ.get("FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE_PUBLICATION") == "1"
)
WEEKLY_PRODUCTION_CRON_UTC = os.environ.get(
    "FOLDARIUM_WEEKLY_PRODUCTION_CRON", "15 * * * 6,0,1"
)
WEEKLY_PRODUCTION_ENABLED = (
    os.environ.get("FOLDARIUM_ENABLE_WEEKLY_PRODUCTION_PROMOTION") == "1"
)
WEEKLY_PRODUCTION_OPEN_ENV = "FOLDARIUM_WEEKLY_PRODUCTION_OPEN"
WEEKLY_PRODUCTION_ROUND_SUFFIX = os.environ.get(
    "FOLDARIUM_WEEKLY_PRODUCTION_ROUND_SUFFIX", "beta-v1"
)
WEEKLY_REGISTER_SELECTOR_KIT_ENV = "FOLDARIUM_WEEKLY_REGISTER_SELECTOR_KIT"
REQUIRED_RETROSPECTIVE_MIGRATION = (
    "20260826190000_require_retrospective_vote_scope.sql"
)
WEEKLY_LIFECYCLE_JOURNAL_MOUNT = DEFAULT_JOURNAL_ROOT
WEEKLY_INTAKE_REPLAY_MAX_AGE_DAYS = DEFAULT_INTAKE_REPLAY_MAX_AGE_DAYS
_lifecycle_journal_hooks: dict[str, Callable[[], None] | None] = {
    "commit": None,
    "reload": None,
}
# Six hourly Wednesday ticks cover a delayed coordinate release without an
# unbounded poller. Each tick receives two short infrastructure retries; a
# scientifically incomplete item still aborts the whole atomic reveal.
WEDNESDAY_REVEAL_MODAL_RETRIES = 2
QUIZ_EVALUATION_PACKAGES = (
    "gemmi==0.7.5",
    "numpy==2.3.2",
    "rdkit==2025.3.6",
)
WEEKLY_RUNTIME_ENV = {
    key: os.environ[key]
    for key in (
        "FOLDARIUM_ENABLE_WEEKLY_CRON",
        "FOLDARIUM_WEEKLY_CRON",
        WEEKLY_HOOK_ENV,
        "FOLDARIUM_WEEKLY_REGISTER",
        "FOLDARIUM_WEEKLY_SUBMIT",
        "FOLDARIUM_WEEKLY_MAX_TARGETS",
        "FOLDARIUM_WEEKLY_GPU_CLASS",
        PUBLIC_QUIZ_BUCKET_ENV,
        "FOLDARIUM_PREDICTION_MAX_CONTAINERS",
        "FOLDARIUM_ENABLE_NEXTWEEKLY_CRON",
        "FOLDARIUM_NEXTWEEKLY_CRON",
        "FOLDARIUM_NEXTWEEKLY_ROUND_VERSION",
        "FOLDARIUM_NEXTWEEKLY_ENVIRONMENT",
        "FOLDARIUM_NEXTWEEKLY_INCLUDE_POSE_METRICS",
        "FOLDARIUM_ENABLE_WEDNESDAY_REVEAL",
        "FOLDARIUM_WEDNESDAY_REVEAL_CRON",
        WEDNESDAY_REVEAL_PUBLISH_ENV,
        "FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE",
        "FOLDARIUM_WEEKLY_RETROSPECTIVE_CRON",
        "FOLDARIUM_ENABLE_WEEKLY_RETROSPECTIVE_PUBLICATION",
        "FOLDARIUM_WEEKLY_RETROSPECTIVE_PUBLICATION_CRON",
        "FOLDARIUM_ENABLE_WEEKLY_PRODUCTION_PROMOTION",
        "FOLDARIUM_WEEKLY_PRODUCTION_CRON",
        WEEKLY_PRODUCTION_OPEN_ENV,
        "FOLDARIUM_WEEKLY_PRODUCTION_ROUND_SUFFIX",
        WEEKLY_REGISTER_SELECTOR_KIT_ENV,
        DEPLOYMENT_CONFIG_SHA256_ENV,
    )
    if key in os.environ
}

_PIPELINE_ROOT = Path(__file__).resolve().parents[1]
_CORE_SOURCE = _PIPELINE_ROOT / "src" / "foldarium_pipeline"
_REMOTE_SOURCE_ROOT = "/opt/foldarium"


def _normalise_task_json(task_json: str | Mapping[str, Any]) -> str:
    """Return a validated JSON object without interpreting the core schema."""

    if isinstance(task_json, Mapping):
        payload: Any = dict(task_json)
    elif isinstance(task_json, str):
        payload = json.loads(task_json)
    else:
        raise TypeError("task_json must be a JSON string or mapping")
    if not isinstance(payload, dict):
        raise ValueError("task_json must encode a JSON object")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _method_name(task_json: str) -> str:
    payload = json.loads(task_json)
    method = payload.get("method")
    if method not in {"openfold3", "boltz2"}:
        raise ValueError(f"unsupported prediction method: {method!r}")
    return method


def _execute(task_json: str | Mapping[str, Any]) -> dict[str, Any]:
    """Run and durably publish one task inside a prediction image.

    Publishing configuration is resolved before GPU execution. This fail-closed
    ordering prevents a successful prediction from existing only on ephemeral
    container storage.
    """

    canonical_json = _normalise_task_json(task_json)
    try:
        from foldarium_pipeline.contracts import validate_prediction_task
        from foldarium_pipeline.supabase import SupabasePublisher
        from foldarium_pipeline.worker import execute_task_json
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "the Foldarium worker and Supabase publisher must both be present in "
            "the deployment image; build the complete pipeline package first"
        ) from exc

    task = validate_prediction_task(json.loads(canonical_json))
    worker_token = (
        os.environ.get("MODAL_TASK_ID")
        or os.environ.get("FOLDARIUM_WORKER_ID")
        or os.environ.get("HOSTNAME")
        or "unknown"
    )
    worker_id = f"modal:{worker_token}"
    try:
        publisher = SupabasePublisher.from_env()
    except Exception as exc:
        raise RuntimeError(
            "durable publisher configuration is unavailable; refusing to start "
            "a prediction whose outputs would exist only on Modal scratch storage"
        ) from exc

    requested_timeout = task["resources"].get(
        "timeout_seconds", GPU_FUNCTION_TIMEOUT_SECONDS
    )
    if (
        isinstance(requested_timeout, bool)
        or not isinstance(requested_timeout, int)
        or requested_timeout < 1
    ):
        raise ValueError("resources.timeout_seconds must be a positive integer")
    # The lease must outlive the container so a killed worker cannot be reclaimed
    # while it is still writing, but it must still expire so a crash is
    # recoverable without manual intervention.
    lease_seconds = requested_timeout + LEASE_GRACE_SECONDS
    if not publisher.claim_run(task["task_id"], worker_id, lease_seconds):
        raise RuntimeError(
            f"prediction run {task['task_id']} is already claimed by another worker"
        )

    result = execute_task_json(task, work_root=WORK_ROOT, dry_run=False)
    if not isinstance(result, dict):
        raise TypeError("execute_task_json must return a dict")
    if result.get("status") == "failed":
        # The core result remains deliberately terse. Preserve bounded command
        # tails and an output inventory in private Modal logs so an operator can
        # distinguish CLI/configuration, filtering, and collector failures
        # without launching a diagnostic GPU retry. These diagnostics must never
        # be copied into a public quiz payload.
        task_root = Path(WORK_ROOT) / task["task_id"]
        diagnostic: dict[str, Any] = {
            "task_id": task["task_id"],
            "method": task["method"],
            "error_code": result.get("error_code"),
        }
        for stream in ("stdout", "stderr"):
            log_path = task_root / "logs" / f"{stream}.log"
            if log_path.is_file():
                diagnostic[f"{stream}_tail"] = log_path.read_text(
                    encoding="utf-8", errors="replace"
                )[-8_000:]
        output_root = task_root / "output"
        if output_root.is_dir():
            diagnostic["output_files"] = [
                path.relative_to(output_root).as_posix()
                for path in sorted(output_root.rglob("*"))
                if path.is_file()
            ][:200]
        print("foldarium.worker.diagnostic " + json.dumps(diagnostic, sort_keys=True))
    publisher.publish_result(
        result,
        Path(WORK_ROOT) / task["task_id"] / "output",
        worker_id,
    )
    return result


def _load_weekly_hook(reference: str):
    """Load an explicitly configured ``module:function`` campaign producer."""

    module_name, separator, function_name = reference.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError(f"{WEEKLY_HOOK_ENV} must use module:function syntax")
    function = getattr(importlib.import_module(module_name), function_name)
    if not callable(function):
        raise TypeError(f"weekly hook {reference!r} is not callable")
    return function


def _default_weekly_round_id(now: datetime | None = None) -> str:
    """Return the round opened on the most recent UTC Saturday."""

    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    current = current.astimezone(timezone.utc)
    saturday = current.date() - timedelta(days=(current.weekday() - 5) % 7)
    return f"weekly-{saturday.isoformat()}"


def _default_weekly_campaign_id(now: datetime | None = None) -> str:
    return _default_weekly_round_id(now).replace("weekly-", "wwpdb-", 1)


def _wednesday_publish_enabled(explicit: bool | None) -> bool:
    """Resolve the explicit mutation gate for a manual or scheduled call."""

    if explicit is not None:
        if not isinstance(explicit, bool):
            raise TypeError("publish must be a boolean or null")
        return explicit
    configured = os.environ.get(WEDNESDAY_REVEAL_PUBLISH_ENV, "0")
    if configured not in {"0", "1"}:
        raise ValueError(f"{WEDNESDAY_REVEAL_PUBLISH_ENV} must be 0 or 1")
    return configured == "1"


def _weekly_production_window(
    release_date: str | date | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the immutable production identity for one Saturday campaign."""

    preview = _nextweekly_window(release_date, now=now)
    suffix = WEEKLY_PRODUCTION_ROUND_SUFFIX.strip()
    if not suffix or "/" in suffix or " " in suffix:
        raise ValueError("FOLDARIUM_WEEKLY_PRODUCTION_ROUND_SUFFIX is invalid")
    return {
        **preview,
        "preview_round_id": preview["round_id"],
        "round_id": f"weekly-{preview['release_date']}-{suffix}",
        "environment": "production",
    }


def _weekly_quiz_public_private_coordinators() -> tuple[Any, Any]:
    """Return private and public coordinators using the reviewed bucket split."""

    from foldarium_pipeline.supabase import SupabaseConfigurationError, SupabaseCoordinator

    private = SupabaseCoordinator.from_env()
    try:
        public_bucket = _weekly_public_bucket()
    except ValueError as exc:
        raise SupabaseConfigurationError(
            f"missing or invalid {PUBLIC_QUIZ_BUCKET_ENV}"
        ) from exc
    public_environment = dict(os.environ)
    public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
    public = SupabaseCoordinator.from_env(public_environment)
    if public.storage_bucket == private.storage_bucket:
        raise SupabaseConfigurationError(
            "public quiz bucket must differ from the private prediction bucket"
        )
    return private, public


def _attempt_production_selector_kit_registration(
    round_id: str,
    *,
    private_coordinator: Any,
    public_coordinator: Any,
) -> dict[str, Any]:
    """Register one production selector kit and surface retryable failures."""

    try:
        from foldarium_pipeline.weekly_quiz import backfill_selector_kit_for_round
    except ImportError:
        return {
            "status": "skipped-module-unavailable",
            "retryable": False,
        }

    try:
        round_row = private_coordinator.weekly_quiz_round(round_id)
        result = backfill_selector_kit_for_round(
            round_row,
            public_coordinator=public_coordinator,
            private_coordinator=private_coordinator,
            register_catalog=True,
        )
    except Exception as exc:
        return {
            "status": f"failed:{type(exc).__name__}",
            "retryable": True,
            "error": str(exc),
        }
    return {
        "status": "registered",
        "retryable": False,
        "kit_sha256": result.get("kit_sha256"),
        "registered": result.get("registered"),
    }


def _production_selector_kit_status(
    *,
    open_round: bool,
    register_selector_kit: bool,
    selector_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize selector-kit fields for one production promotion tick."""

    if not register_selector_kit:
        return {
            "selector_kit_status": "not-requested",
            "selector_kit_retryable": False,
        }
    if not open_round:
        return {
            "selector_kit_status": "pending-open-gate",
            "selector_kit_retryable": False,
        }
    if selector_result is None:
        return {
            "selector_kit_status": "not-requested",
            "selector_kit_retryable": False,
        }
    return {
        "selector_kit_status": selector_result["status"],
        "selector_kit_retryable": bool(selector_result.get("retryable")),
        **(
            {"selector_kit_error": selector_result["error"]}
            if selector_result.get("error")
            else {}
        ),
        **(
            {"selector_kit_sha256": selector_result["kit_sha256"]}
            if selector_result.get("kit_sha256")
            else {}
        ),
    }


def _production_tick_status(
    *,
    production_exists: bool,
    preview_exists: bool,
    open_round: bool,
    selector_kit_retryable: bool,
    selector_kit_status: str,
    just_promoted: bool,
) -> str:
    if not production_exists:
        if not preview_exists:
            return "waiting-for-preview"
        return "production-opened" if open_round else "production-staged"
    if selector_kit_status == "skipped-module-unavailable":
        return "production-ready-selector-kit-skipped"
    if selector_kit_retryable:
        return "production-ready-selector-kit-retryable"
    if just_promoted:
        return "production-opened" if open_round else "production-staged"
    return "production-ready"


def _lifecycle_deployment_report() -> dict[str, Any]:
    """Return the non-secret deployment gates visible to preflight callers."""

    return {
        "app_name": APP_NAME,
        "config_sha256": os.environ.get(DEPLOYMENT_CONFIG_SHA256_ENV),
        "weekly": {
            "enabled": WEEKLY_CRON_ENABLED,
            "cron": WEEKLY_CRON_UTC,
            "hook": os.environ.get(WEEKLY_HOOK_ENV),
            "register": os.environ.get("FOLDARIUM_WEEKLY_REGISTER") == "1",
            "submit": os.environ.get("FOLDARIUM_WEEKLY_SUBMIT") == "1",
            "max_targets": os.environ.get("FOLDARIUM_WEEKLY_MAX_TARGETS"),
            "gpu_class": os.environ.get("FOLDARIUM_WEEKLY_GPU_CLASS"),
            "public_quiz_bucket": os.environ.get(PUBLIC_QUIZ_BUCKET_ENV),
            "prediction_max_containers": PREDICTION_MAX_CONTAINERS,
        },
        "nextweekly": {
            "enabled": NEXTWEEKLY_CRON_ENABLED,
            "cron": NEXTWEEKLY_CRON_UTC,
            "environment": NEXTWEEKLY_ENVIRONMENT,
            "include_pose_metrics": NEXTWEEKLY_INCLUDE_POSE_METRICS,
            "round_version": NEXTWEEKLY_ROUND_VERSION,
            "retry_policy": "every-attempt-one-failure-once",
            "retry_batch_size": NEXTWEEKLY_RETRY_BATCH_SIZE,
            "gpu_command_budget_seconds": NEXTWEEKLY_GPU_COMMAND_BUDGET_SECONDS,
            "gpu_cost_budget_usd": NEXTWEEKLY_GPU_COST_BUDGET_USD,
        },
        "weekly_production": {
            "enabled": WEEKLY_PRODUCTION_ENABLED,
            "cron": WEEKLY_PRODUCTION_CRON_UTC,
            "open": os.environ.get(WEEKLY_PRODUCTION_OPEN_ENV) == "1",
            "round_suffix": WEEKLY_PRODUCTION_ROUND_SUFFIX,
            "register_selector_kit": (
                os.environ.get(WEEKLY_REGISTER_SELECTOR_KIT_ENV) == "1"
            ),
        },
        "wednesday_reveal": {
            "enabled": WEDNESDAY_REVEAL_ENABLED,
            "cron": WEDNESDAY_REVEAL_CRON_UTC,
            "publish": _wednesday_publish_enabled(None),
        },
        "weekly_retrospective": {
            "enabled": WEEKLY_RETROSPECTIVE_ENABLED,
            "cron": WEEKLY_RETROSPECTIVE_CRON_UTC,
        },
        "weekly_retrospective_publication": {
            "enabled": WEEKLY_RETROSPECTIVE_PUBLICATION_ENABLED,
            "cron": WEEKLY_RETROSPECTIVE_PUBLICATION_CRON_UTC,
        },
        "required_migrations_before_publication": [REQUIRED_RETROSPECTIVE_MIGRATION],
        "lifecycle_journal": {
            "volume": LIFECYCLE_JOURNAL_VOLUME_NAME,
            "mount": WEEKLY_LIFECYCLE_JOURNAL_MOUNT,
        },
    }


def _lifecycle_journal_commit() -> None:
    commit_journal_volume(_lifecycle_journal_hooks.get("commit"))


def _lifecycle_journal_reload() -> None:
    reload = _lifecycle_journal_hooks.get("reload")
    if reload is not None:
        reload()


def _lifecycle_journal_root() -> Path:
    """Resolve the active journal directory, falling back locally when unwritable."""

    configured = Path(
        os.environ.get("FOLDARIUM_LIFECYCLE_JOURNAL_MOUNT", WEEKLY_LIFECYCLE_JOURNAL_MOUNT)
    )
    try:
        configured.mkdir(parents=True, exist_ok=True)
        probe = configured / ".foldarium_journal_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return configured
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "foldarium-lifecycle-journal-local"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _weekly_lifecycle_journal(operation: str):
    """Wrap one lifecycle control function with durable started/succeeded/failed records."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            root = _lifecycle_journal_root()
            return run_with_lifecycle_journal(
                operation,
                root,
                lambda: fn(*args, **kwargs),
                args=args,
                kwargs=kwargs,
                commit=_lifecycle_journal_commit,
                on_journal_error=lambda message: print(message, flush=True),
            )

        return wrapper

    return decorator


@contextlib.contextmanager
def _temporary_environ(overrides: Mapping[str, str | None]) -> Iterator[None]:
    previous: dict[str, str | None] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _invoke_weekly_hook_pipeline(
    *,
    submit: bool | None = None,
    spawn_task: Callable[[str | Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Run the configured weekly hook and optionally submit registered GPU tasks."""

    reference = os.environ.get(WEEKLY_HOOK_ENV)
    if not reference:
        return {
            "status": "disabled",
            "reason": f"{WEEKLY_HOOK_ENV} is not configured",
        }
    produced = _load_weekly_hook(reference)()
    if isinstance(produced, Mapping):
        raw_tasks = produced.get("tasks")
        if not isinstance(raw_tasks, list):
            raise TypeError("weekly hook mapping must contain a tasks list")
        tasks = raw_tasks
        report = {key: value for key, value in produced.items() if key != "tasks"}
    else:
        tasks = list(produced)
        report = {}
    if not tasks:
        return {"status": report.pop("status", "no-work"), "count": 0, **report}
    should_submit = (
        os.environ.get("FOLDARIUM_WEEKLY_SUBMIT") == "1"
        if submit is None
        else submit
    )
    if not should_submit:
        return {
            "status": "planned-not-submitted",
            "count": len(tasks),
            **report,
        }
    registration = report.get("registration")
    if not isinstance(registration, Mapping) or registration.get("status") != "registered":
        raise RuntimeError(
            "weekly GPU submission requires an atomically registered Supabase plan"
        )
    if spawn_task is None:
        raise RuntimeError("weekly GPU submission requires a Modal task spawner")
    call_ids = [spawn_task(task) for task in tasks]
    return {
        "status": "submitted",
        "count": len(call_ids),
        "call_ids": call_ids,
        **report,
    }


def _nextweekly_window(
    release_date: str | date | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the immutable Preview identity/window for one Saturday intake."""

    if release_date is None:
        current = datetime.now(timezone.utc) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("now must be a timezone-aware datetime")
        current_date = current.astimezone(timezone.utc).date()
        selected = current_date - timedelta(days=(current_date.weekday() - 5) % 7)
    elif isinstance(release_date, date):
        selected = release_date
    elif isinstance(release_date, str):
        try:
            selected = date.fromisoformat(release_date)
        except ValueError as exc:
            raise ValueError("release_date must be an ISO date") from exc
    else:
        raise TypeError("release_date must be an ISO date, date, or null")
    if selected.weekday() != 5:
        raise ValueError("nextweekly release_date must be a Saturday")
    round_version = NEXTWEEKLY_ROUND_VERSION.strip()
    if (
        not round_version
        or len(round_version) > 32
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in round_version
        )
    ):
        raise ValueError("FOLDARIUM_NEXTWEEKLY_ROUND_VERSION is invalid")
    opens = datetime.combine(selected, time(hour=3), tzinfo=timezone.utc)
    closes = datetime.combine(
        selected + timedelta(days=4), time.min, tzinfo=timezone.utc
    )

    def utc(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")

    return {
        "release_date": selected.isoformat(),
        "campaign_id": f"wwpdb-{selected.isoformat()}",
        "round_id": (
            f"preview-weekly-{selected.isoformat()}-nextweekly-"
            f"{round_version}"
        ),
        "opens_at": utc(opens),
        "closes_at": utc(closes),
    }


def _nextweekly_run_report(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize run state and identify each attempt-1 failure for one retry."""

    counts = {
        status: 0
        for status in ("pending", "queued", "running", "succeeded", "failed", "cancelled")
    }
    method_counts: dict[str, dict[str, int]] = {}
    active_run_ids: list[str] = []
    retry_candidates: list[dict[str, Any]] = []
    authorized_retry_pending_run_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(f"campaign run statuses[{index}] must be an object")
        run_id = row.get("run_id")
        method = row.get("method")
        status = row.get("status")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("campaign run status has no run_id")
        if not isinstance(method, str) or not method:
            raise ValueError(f"campaign run {run_id} has no method")
        if status not in counts:
            raise ValueError(f"campaign run {run_id} has invalid status {status!r}")
        counts[status] += 1
        method_counts.setdefault(method, {}).setdefault(status, 0)
        method_counts[method][status] += 1
        if status in {"pending", "queued", "running"}:
            active_run_ids.append(run_id)
        if (
            status == "failed"
            and row.get("attempt_count") == 1
            and row.get("max_attempts") == 2
        ):
            # Authorization is durable before Modal spawn acknowledgement.  A
            # later tick must never assemble around an authorized-but-unclaimed
            # retry; break-glass resubmission remains an operator action.
            authorized_retry_pending_run_ids.append(run_id)
        if not (
            status == "failed"
            and row.get("attempt_count") == 1
            and row.get("max_attempts") == 1
        ):
            continue
        task_payload = row.get("task_payload")
        resources = (
            task_payload.get("resources")
            if isinstance(task_payload, Mapping)
            else None
        )
        config = (
            task_payload.get("config")
            if isinstance(task_payload, Mapping)
            else None
        )
        if (
            not isinstance(resources, Mapping)
            or resources.get("gpu_class") != "l4"
            or resources.get("timeout_seconds") != 1800
        ):
            continue
        retry_kind: str | None = None
        reviewed_legacy = False
        error_code = row.get("error_code")
        if error_code == "gpu_out_of_memory":
            retry_kind = "gpu_out_of_memory"
        elif (
            error_code == "msa_generation_timeout"
            and isinstance(row.get("result"), Mapping)
            and row["result"].get("failure_stage") == "msa_generation"
            and isinstance(config, Mapping)
            and config.get("msa_mode") == "server"
        ):
            retry_kind = "msa_generation_timeout"
        elif method == "boltz2" and error_code == "msa_preprocessing_failed":
            retry_kind = "msa_preprocessing_failed"
        else:
            reviewed = NEXTWEEKLY_REVIEWED_LEGACY_RETRIES.get(run_id)
            if (
                isinstance(reviewed, Mapping)
                and reviewed.get("target_id") == row.get("target_id")
                and reviewed.get("method") == method
                and reviewed.get("source_error_code") == error_code
            ):
                retry_kind = str(reviewed["retry_kind"])
                reviewed_legacy = True
            else:
                retry_kind = "repeat_once"
        if retry_kind == "gpu_out_of_memory":
            retry_gpu_class = "a100-40gb"
            retry_timeout_seconds = NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
        elif retry_kind == "msa_generation_timeout":
            retry_gpu_class = "l4"
            retry_timeout_seconds = NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
        else:
            retry_gpu_class = "l4"
            retry_timeout_seconds = NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
        retry_candidates.append(
            {
                "run_id": run_id,
                "target_id": row.get("target_id"),
                "method": method,
                "source_error_code": error_code,
                "retry_kind": retry_kind,
                "retry_gpu_class": retry_gpu_class,
                "retry_timeout_seconds": retry_timeout_seconds,
                "reviewed_legacy": reviewed_legacy,
            }
        )
    retry_candidates.sort(key=lambda item: item["run_id"])
    return {
        "run_count": len(rows),
        "status_counts": counts,
        "method_status_counts": {
            method: dict(sorted(statuses.items()))
            for method, statuses in sorted(method_counts.items())
        },
        "active_run_ids": sorted(active_run_ids),
        "retryable_run_ids": [item["run_id"] for item in retry_candidates],
        "retry_candidates": retry_candidates,
        "authorized_retry_pending_run_ids": sorted(
            authorized_retry_pending_run_ids
        ),
    }


def _nextweekly_retry_budget(
    rows: list[Mapping[str, Any]],
    retry_candidates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a fail-closed campaign-wide automatic-retry authorization budget.

    A terminal attempt-1 row still contains that original attempt's exact command
    duration. Once a retry is authorized, however, the row can later contain only
    the retry result and does not durably store the retry tier.  Therefore every
    ``max_attempts=2`` row reserves the maximum supported retry command duration
    and the larger of the supported retry costs.  This deliberately overcounts
    old L4 MSA retries but prevents a later tick from recovering spent authority.
    """

    invalid_run_ids: list[str] = []
    conservatively_accounted_run_ids: list[str] = []
    exact_original_seconds: list[float] = []
    exact_original_cost_usd: list[float] = []
    authorized_retry_count = 0
    for index, row in enumerate(rows):
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            run_id = f"invalid-row-{index}"
        status = row.get("status")
        attempt_count = row.get("attempt_count")
        max_attempts = row.get("max_attempts")
        if (
            status not in {"succeeded", "failed", "cancelled"}
            or isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or attempt_count < 1
            or max_attempts not in {1, 2}
            or attempt_count > max_attempts
        ):
            invalid_run_ids.append(run_id)
            continue
        if max_attempts == 2:
            task_payload = row.get("task_payload")
            resources = (
                task_payload.get("resources")
                if isinstance(task_payload, Mapping)
                else None
            )
            timeout_seconds = (
                resources.get("timeout_seconds")
                if isinstance(resources, Mapping)
                else None
            )
            gpu_class = (
                resources.get("gpu_class")
                if isinstance(resources, Mapping)
                else None
            )
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1
                <= timeout_seconds
                <= NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
                or gpu_class not in NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS
            ):
                invalid_run_ids.append(run_id)
                continue
            authorized_retry_count += 1
            # The prior attempt duration is no longer reliable after retry result
            # publication. Reserve the full original task command.
            exact_original_seconds.append(float(timeout_seconds))
            exact_original_cost_usd.append(
                timeout_seconds * NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS[gpu_class]
            )
            continue
        task_payload = row.get("task_payload")
        resources = (
            task_payload.get("resources")
            if isinstance(task_payload, Mapping)
            else None
        )
        gpu_class = (
            resources.get("gpu_class") if isinstance(resources, Mapping) else None
        )
        if gpu_class not in NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS:
            invalid_run_ids.append(run_id)
            continue
        result = row.get("result")
        duration_seconds = (
            result.get("duration_seconds") if isinstance(result, Mapping) else None
        )
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(float(duration_seconds))
            or duration_seconds < 0
        ):
            timeout_seconds = resources.get("timeout_seconds")
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
            ):
                invalid_run_ids.append(run_id)
                continue
            duration_seconds = timeout_seconds
            conservatively_accounted_run_ids.append(run_id)
        exact_original_seconds.append(float(duration_seconds))
        exact_original_cost_usd.append(
            float(duration_seconds) * NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS[gpu_class]
        )

    original_consumed_seconds = math.fsum(exact_original_seconds)
    retry_reserved_seconds = (
        authorized_retry_count * NEXTWEEKLY_MAX_RETRY_RESERVATION_SECONDS
    )
    original_consumed_cost_usd = math.fsum(exact_original_cost_usd)
    maximum_retry_cost_usd = max(
        NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
        * NEXTWEEKLY_A100_40GB_RATE_USD_PER_SECOND,
        NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
        * NEXTWEEKLY_L4_RATE_USD_PER_SECOND,
    )
    retry_reserved_cost_usd = authorized_retry_count * maximum_retry_cost_usd
    consumed_or_reserved_seconds = original_consumed_seconds + retry_reserved_seconds
    consumed_or_reserved_cost_usd = (
        original_consumed_cost_usd + retry_reserved_cost_usd
    )
    remaining_seconds = max(
        0.0,
        NEXTWEEKLY_GPU_COMMAND_BUDGET_SECONDS - consumed_or_reserved_seconds,
    )
    remaining_cost_usd = max(
        0.0, NEXTWEEKLY_GPU_COST_BUDGET_USD - consumed_or_reserved_cost_usd
    )
    authorized_candidate_run_ids: list[str] = []
    candidate_seconds = 0
    candidate_cost_usd = 0.0
    if not invalid_run_ids:
        for candidate in retry_candidates:
            run_id = candidate.get("run_id")
            retry_timeout = candidate.get("retry_timeout_seconds")
            retry_gpu_class = candidate.get("retry_gpu_class")
            if (
                not isinstance(run_id, str)
                or not run_id
                or isinstance(retry_timeout, bool)
                or not isinstance(retry_timeout, int)
                or retry_timeout not in {
                    NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS,
                    NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS,
                }
                or retry_gpu_class not in NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS
            ):
                invalid_run_ids.append(
                    run_id if isinstance(run_id, str) and run_id else "invalid-candidate"
                )
                authorized_candidate_run_ids = []
                break
            cost = retry_timeout * NEXTWEEKLY_RETRY_RATE_BY_GPU_CLASS[retry_gpu_class]
            if (
                candidate_seconds + retry_timeout > remaining_seconds
                or candidate_cost_usd + cost > remaining_cost_usd
                or len(authorized_candidate_run_ids) >= NEXTWEEKLY_RETRY_BATCH_SIZE
            ):
                continue
            authorized_candidate_run_ids.append(run_id)
            candidate_seconds += retry_timeout
            candidate_cost_usd += cost
    remaining_retry_slots = len(authorized_candidate_run_ids)
    if invalid_run_ids:
        status = "invalid-run-accounting"
        remaining_retry_slots = 0
        authorized_candidate_run_ids = []
    elif remaining_retry_slots < 1:
        status = "exhausted"
    else:
        status = "available"
    return {
        "status": status,
        "authorization_ready": status == "available",
        "command_budget_seconds": NEXTWEEKLY_GPU_COMMAND_BUDGET_SECONDS,
        "cost_budget_usd": NEXTWEEKLY_GPU_COST_BUDGET_USD,
        "maximum_retry_reservation_seconds": (
            NEXTWEEKLY_MAX_RETRY_RESERVATION_SECONDS
        ),
        "maximum_retry_reservation_cost_usd": maximum_retry_cost_usd,
        "original_consumed_seconds": original_consumed_seconds,
        "original_consumed_cost_usd": original_consumed_cost_usd,
        "authorized_retry_count": authorized_retry_count,
        "retry_reserved_seconds": retry_reserved_seconds,
        "retry_reserved_cost_usd": retry_reserved_cost_usd,
        "consumed_or_reserved_seconds": consumed_or_reserved_seconds,
        "consumed_or_reserved_cost_usd": consumed_or_reserved_cost_usd,
        "remaining_seconds": remaining_seconds,
        "remaining_cost_usd": remaining_cost_usd,
        "remaining_retry_slots": remaining_retry_slots,
        "authorized_candidate_run_ids": authorized_candidate_run_ids,
        "candidate_reserved_seconds": candidate_seconds,
        "candidate_reserved_cost_usd": candidate_cost_usd,
        "invalid_run_ids": sorted(invalid_run_ids),
        "conservatively_accounted_run_ids": sorted(
            conservatively_accounted_run_ids
        ),
    }


def _retry_execution_task(
    task_payload: Mapping[str, Any], retry_request: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a validated execution-only resource override for one retry."""

    from foldarium_pipeline.contracts import validate_prediction_task

    task = validate_prediction_task(task_payload)
    run_id = retry_request.get("run_id")
    retry_kind = retry_request.get("retry_kind")
    expected = {
        "gpu_out_of_memory": (
            "a100-40gb",
            NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS,
        ),
        "msa_generation_timeout": (
            "l4",
            NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS,
        ),
        "msa_preprocessing_failed": (
            "l4",
            NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS,
        ),
        "repeat_once": (
            "l4",
            NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS,
        ),
    }.get(retry_kind)
    if expected is None:
        raise ValueError("unsupported retry kind")
    if (
        task["task_id"] != run_id
        or task["target"]["target_id"] != retry_request.get("target_id")
        or task["method"] != retry_request.get("method")
        or task["resources"].get("gpu_class") != "l4"
        or task["resources"].get("timeout_seconds") != 1800
        or retry_request.get("retry_gpu_class") != expected[0]
        or retry_request.get("retry_timeout_seconds") != expected[1]
        or not isinstance(retry_request.get("reviewed_legacy"), bool)
    ):
        raise ValueError("retry request does not match its immutable task or policy")
    retry_task = deepcopy(task)
    retry_task["resources"] = {
        **task["resources"],
        "gpu_class": expected[0],
        "timeout_seconds": expected[1],
        "retry_policy": {
            "retry_kind": retry_kind,
            "source_error_code": retry_request.get("source_error_code"),
            "reviewed_legacy": retry_request["reviewed_legacy"],
            "original_gpu_class": "l4",
            "original_timeout_seconds": 1800,
        },
    }
    return validate_prediction_task(retry_task)


if modal is not None:
    # Modal re-imports this module inside every container to find the function
    # it should run. At that point the local checkout does not exist, so local
    # paths may only be touched while running locally.
    _IS_LOCAL = modal.is_local()
    if _IS_LOCAL and not _CORE_SOURCE.is_dir():
        raise RuntimeError(f"Foldarium core source directory not found: {_CORE_SOURCE}")

    # Source is added explicitly below rather than by automounting, so the image
    # never picks up the repository's large data directories.
    app = modal.App(APP_NAME, include_source=False)

    # These volumes are disposable acceleration caches. Prediction inputs,
    # outputs, run state, and publication state must live in object storage and
    # Supabase, never only in a Modal Volume.
    openfold_cache = modal.Volume.from_name(
        "foldarium-openfold3-cache", create_if_missing=True
    )
    boltz_cache = modal.Volume.from_name(
        "foldarium-boltz2-cache", create_if_missing=True
    )
    weekly_lifecycle_journal_volume = modal.Volume.from_name(
        LIFECYCLE_JOURNAL_VOLUME_NAME,
        create_if_missing=True,
    )
    _lifecycle_journal_hooks["commit"] = weekly_lifecycle_journal_volume.commit
    reload_hook = getattr(weekly_lifecycle_journal_volume, "reload", None)
    _lifecycle_journal_hooks["reload"] = reload_hook if callable(reload_hook) else None
    LIFECYCLE_JOURNAL_VOLUMES = {
        WEEKLY_LIFECYCLE_JOURNAL_MOUNT: weekly_lifecycle_journal_volume,
    }

    control_plane_secret = modal.Secret.from_name(
        "foldarium-control-plane",
        required_keys=[
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "FOLDARIUM_STORAGE_BUCKET",
        ],
    )

    def _add_core(image, *, clear_entrypoint: bool = True):
        """Attach the portable core to a prediction image.

        Images we build ourselves have no meaningful entrypoint, so clearing it
        keeps the container command explicit. An upstream image may instead use
        its entrypoint to activate the environment its tools live in; clearing
        that would hide both the interpreter and the method CLI.
        """

        if clear_entrypoint:
            image = image.entrypoint([])
        if _IS_LOCAL:
            image = image.add_local_dir(
                _CORE_SOURCE,
                remote_path=f"{_REMOTE_SOURCE_ROOT}/foldarium_pipeline",
                copy=True,
            ).add_local_file(
                # Modal imports this module by name inside the container, so the
                # file defining the functions must itself be importable there.
                Path(__file__).resolve(),
                remote_path=f"{_REMOTE_SOURCE_ROOT}/modal_app.py",
                copy=True,
            )
        return image.env({"PYTHONPATH": _REMOTE_SOURCE_ROOT})

    # The official OpenFold3 image ships a Pixi environment activated by its
    # entrypoint. Clear that entrypoint so Modal always starts under the injected
    # 3.12 interpreter, then explicitly source the activation script only in OF3
    # subprocesses. This isolates Modal's runtime from the upstream Python 3.14
    # environment while retaining its CUDA, Triton, libtorch, and CLI settings.
    # OpenFold3's Pixi environment currently contains Python 3.14. Its model CLI
    # is pinned to that environment, but Modal's container runtime must not be:
    # grpclib in the injected runtime is not compatible with this upstream 3.14
    # build. Inject a standalone 3.12 interpreter for Modal itself while keeping
    # the upstream entrypoint/PATH so method subprocesses still use the official
    # Pixi environment.
    openfold3_image = _add_core(
        modal.Image.from_registry(
            OPENFOLD3_IMAGE_REF,
            add_python=OPENFOLD3_CONTROL_PYTHON,
        ).env({"OPENFOLD_CACHE": OPENFOLD_CACHE_ROOT}),
    )

    # This bootstrap recipe is version-pinned and deliberately contains no
    # weights or credentials. For GCP, publish its equivalent as a
    # Foldarium-owned OCI image and pin the resulting Artifact Registry digest.
    boltz2_image = _add_core(
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("git")
        .uv_pip_install(BOLTZ2_PACKAGE)
        .env({"BOLTZ_CACHE": BOLTZ_CACHE_ROOT})
    )

    control_image = _add_core(modal.Image.debian_slim(python_version="3.12")).env(
        WEEKLY_RUNTIME_ENV
    )
    quiz_assembly_image = _add_core(
        modal.Image.debian_slim(python_version="3.12").uv_pip_install(
            *QUIZ_EVALUATION_PACKAGES
        )
    ).env(WEEKLY_RUNTIME_ENV)

    @app.function(
        image=control_image,
        cpu=0.25,
        memory=256,
        timeout=60,
        max_containers=1,
    )
    def deployment_config() -> dict[str, Any]:
        """Return only non-secret deployment gates for post-deploy verification."""

        report = _lifecycle_deployment_report()
        report["nextweekly"].update(
            {
                "automatic_retry_kinds": [
                    "gpu_out_of_memory",
                    "msa_generation_timeout",
                    "msa_preprocessing_failed",
                    "repeat_once",
                ],
                "retry_batch_size": NEXTWEEKLY_RETRY_BATCH_SIZE,
                "gpu_command_budget_seconds": NEXTWEEKLY_GPU_COMMAND_BUDGET_SECONDS,
                "gpu_cost_budget_usd": NEXTWEEKLY_GPU_COST_BUDGET_USD,
                "oom_retry": {
                    "from_gpu_class": "l4",
                    "to_gpu_class": "a100-40gb",
                    "command_timeout_seconds": (
                        NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
                    ),
                    "outer_timeout_seconds": (
                        NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
                        + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
                    ),
                },
                "msa_timeout_retry": {
                    "gpu_class": "l4",
                    "command_timeout_seconds": (
                        NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
                    ),
                    "outer_timeout_seconds": (
                        NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
                        + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
                    ),
                },
                "maximum_retry_reservation_seconds": (
                    NEXTWEEKLY_MAX_RETRY_RESERVATION_SECONDS
                ),
            }
        )
        return report

    @app.function(
        image=control_image,
        cpu=1.0,
        memory=1024,
        secrets=[control_plane_secret],
        timeout=2 * 60 * 60,
        max_containers=1,
    )
    def backfill_weekly_public_cache(
        round_id: str,
        apply: bool = False,
        public_quiz_bucket: str | None = None,
    ) -> dict[str, Any]:
        """Verify one exact round and optionally replace bytes to set cache metadata."""

        from foldarium_pipeline.cache_backfill import (
            backfill_immutable_cache,
            verified_public_object_inventory,
        )
        from foldarium_pipeline.supabase import (
            SupabaseConfigurationError,
            SupabaseCoordinator,
        )

        private = SupabaseCoordinator.from_env()
        try:
            public_bucket = _weekly_public_bucket(public_quiz_bucket)
        except ValueError as exc:
            raise SupabaseConfigurationError(
                f"missing or invalid {PUBLIC_QUIZ_BUCKET_ENV}"
            ) from exc
        public_environment = dict(os.environ)
        public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
        public = SupabaseCoordinator.from_env(public_environment)
        if public.storage_bucket == private.storage_bucket:
            raise SupabaseConfigurationError("public quiz bucket must differ from predictions")

        round_row = private.weekly_quiz_round(round_id)
        if round_row.get("environment") != "production":
            raise ValueError("cache backfill accepts production rounds only")
        if apply and round_row.get("status") != "revealed":
            raise ValueError("refusing to update a round that is not revealed")
        manifest = round_row.get("blind_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("weekly round blind manifest is unavailable")
        inventory = verified_public_object_inventory(
            manifest,
            round_id=round_id,
            expected_manifest_sha256=round_row["blind_manifest_sha256"],
            public_bucket=public.storage_bucket,
        )
        summary = backfill_immutable_cache(public, inventory, apply=apply)
        summary.update(
            {
                "round_id": round_id,
                "round_status": round_row.get("status"),
                "object_count": len(inventory),
            }
        )
        return summary

    @app.function(
        image=openfold3_image,
        cpu=2.0,
        memory=8192,
        timeout=60 * 60,
        max_containers=1,
        volumes={OPENFOLD_CACHE_ROOT: openfold_cache},
    )
    def bootstrap_openfold3_cache() -> dict[str, str]:
        """One-time, operator-invoked OpenFold3 setup/cache bootstrap."""

        config_path = Path("/tmp/openfold3-setup.json")
        config_path.write_text(
            json.dumps(
                {
                    "openfold_cache": OPENFOLD_CACHE_ROOT,
                    "param_directory": f"{OPENFOLD_CACHE_ROOT}/parameters",
                    "selected_parameters": "openfold3-p2-155k",
                    "force_download_parameters": False,
                    "run_integration_tests": False,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        subprocess.run(
            [
                "/bin/bash",
                "-lc",
                f"source {OPENFOLD3_ACTIVATE} && exec \"$@\"",
                "foldarium-openfold3",
                "setup_openfold",
                "--config",
                str(config_path),
            ],
            check=True,
            timeout=50 * 60,
        )
        openfold_cache.commit()
        return {
            "status": "ready",
            "cache": OPENFOLD_CACHE_ROOT,
            "parameters": f"{OPENFOLD_CACHE_ROOT}/parameters",
        }

    @app.function(
        image=openfold3_image,
        cpu=2.0,
        memory=8192,
        timeout=5 * 60,
        max_containers=1,
    )
    def validate_openfold3_cli() -> dict[str, Any]:
        """Validate our pinned command-line contract without reserving a GPU."""

        completed = subprocess.run(
            [
                "/bin/bash",
                "-lc",
                f"source {OPENFOLD3_ACTIVATE} && exec \"$@\"",
                "foldarium-openfold3",
                "run_openfold",
                "predict",
                "--help",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2 * 60,
        )
        output = completed.stdout + "\n" + completed.stderr
        expected = (
            "--query_json",
            "--output_dir",
            "--inference_ckpt_name",
            "--num_model_seeds",
            "--num_diffusion_samples",
            "--use_msa_server",
        )
        present = {option: option in output for option in expected}
        return {
            "status": "ready" if completed.returncode == 0 and all(present.values()) else "invalid",
            "returncode": completed.returncode,
            "expected_options": present,
            "diagnostic_tail": "" if completed.returncode == 0 else output[-4_000:],
        }

    @app.function(
        image=openfold3_image,
        cpu=8.0,
        memory=32768,
        gpu="A100-40GB",
        timeout=GPU_FUNCTION_TIMEOUT_SECONDS,
        max_containers=PREDICTION_MAX_CONTAINERS,
        volumes={OPENFOLD_CACHE_ROOT: openfold_cache},
        secrets=[control_plane_secret],
    )
    def run_openfold3(task_json: str | dict[str, Any]) -> dict[str, Any]:
        openfold_cache.reload()
        result = _execute(task_json)
        openfold_cache.commit()
        return result

    @app.function(
        image=boltz2_image,
        cpu=4.0,
        memory=16384,
        gpu="L40S",
        timeout=GPU_FUNCTION_TIMEOUT_SECONDS,
        max_containers=PREDICTION_MAX_CONTAINERS,
        volumes={BOLTZ_CACHE_ROOT: boltz_cache},
        secrets=[control_plane_secret],
    )
    def run_boltz2(task_json: str | dict[str, Any]) -> dict[str, Any]:
        boltz_cache.reload()
        result = _execute(task_json)
        boltz_cache.commit()
        return result

    @app.function(
        image=boltz2_image,
        cpu=4.0,
        memory=16384,
        gpu="L4",
        timeout=GPU_FUNCTION_TIMEOUT_SECONDS,
        max_containers=1,
        volumes={BOLTZ_CACHE_ROOT: boltz_cache},
        secrets=[control_plane_secret],
    )
    def run_transient_boltz_msa_retry(
        task_json: str | dict[str, Any],
    ) -> dict[str, Any]:
        """Run one explicitly authorized Boltz MSA retry on a serialized L4.

        This function deliberately bypasses dynamic GPU sizing and has a hard
        one-container ceiling. Even if several exact run IDs are authorized in
        one maintenance call, only one retry can contact ColabFold at a time.
        """

        canonical_json = _normalise_task_json(task_json)
        if _method_name(canonical_json) != "boltz2":
            raise ValueError("the transient MSA retry worker accepts only Boltz-2 tasks")
        boltz_cache.reload()
        result = _execute(canonical_json)
        boltz_cache.commit()
        return result

    def _sized_function(task_json: str):
        """Return the method's function, moved onto the task's requested class.

        The deployed decorator carries a default so the app is runnable without
        sizing, but a task that names a ``gpu_class`` overrides it per call rather
        than needing a separate deployed function for every accelerator.
        """

        payload = json.loads(task_json)
        method = _method_name(task_json)
        function = run_openfold3 if method == "openfold3" else run_boltz2

        resources = payload.get("resources") or {}
        gpu_class = resources.get("gpu_class")
        if gpu_class is None:
            return function
        if gpu_class not in MODAL_GPU_BY_CLASS:
            raise ValueError(
                f"unsupported gpu_class {gpu_class!r}; this backend maps "
                f"{sorted(MODAL_GPU_BY_CLASS)}"
            )
        cpu, memory = MODAL_HOST_BY_CLASS[gpu_class]
        return function.with_options(
            gpu=MODAL_GPU_BY_CLASS[gpu_class], cpu=cpu, memory=memory
        )

    def _spawn_task(task_json: str | Mapping[str, Any]) -> str:
        canonical_json = _normalise_task_json(task_json)
        return _sized_function(canonical_json).spawn(canonical_json).object_id

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        max_containers=1,
    )
    def submit_tasks(task_jsons: list[str | dict[str, Any]]) -> list[str]:
        """Fan out already-planned tasks; return Modal call IDs for observability."""

        return [_spawn_task(task_json) for task_json in task_jsons]

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=5 * 60,
        max_containers=1,
    )
    def retry_transient_boltz_msa_runs(
        run_ids: list[str],
        confirmed_oom_run_ids: list[str],
        resubmit_already_authorized: bool = False,
    ) -> dict[str, Any]:
        """Authorize and submit an exact bounded list of transient MSA retries.

        ``resubmit_already_authorized`` is a break-glass recovery for a prior
        control call that raised after the database PATCH but before Modal
        acknowledged the spawn. It remains off during ordinary idempotent use.
        """

        from foldarium_pipeline.supabase import SupabaseCoordinator

        authorization = (
            SupabaseCoordinator.from_env().authorize_transient_boltz_msa_retries(
                run_ids,
                confirmed_oom_run_ids=confirmed_oom_run_ids,
                resubmit_already_authorized=resubmit_already_authorized,
            )
        )
        task_payloads = authorization.pop("task_payloads")
        submissions: list[dict[str, str]] = []
        submission_errors: list[dict[str, str]] = []
        for run_id in authorization["approved_submission_run_ids"]:
            task_json = _normalise_task_json(task_payloads[run_id])
            try:
                call_id = run_transient_boltz_msa_retry.spawn(task_json).object_id
            except Exception as exc:  # Modal acknowledgement is the audit boundary.
                submission_errors.append(
                    {
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                        "error": "Modal did not acknowledge the retry spawn",
                    }
                )
                continue
            submissions.append({"run_id": run_id, "modal_call_id": call_id})
        submitted_ids = [row["run_id"] for row in submissions]
        unsubmitted_ids = [row["run_id"] for row in submission_errors]
        if submission_errors and submissions:
            submission_status = "partially-submitted"
        elif submission_errors:
            submission_status = "submission-failed"
        elif submissions:
            submission_status = "submitted"
        else:
            submission_status = "not-resubmitted"
        return {
            **authorization,
            "submission_status": submission_status,
            "submissions": submissions,
            "submission_errors": submission_errors,
            "submitted_run_ids": submitted_ids,
            "authorized_not_submitted_run_ids": unsubmitted_ids,
            "recovery": (
                "call again with only authorized_not_submitted_run_ids and "
                "resubmit_already_authorized=True after verifying no Modal call exists"
                if unsubmitted_ids
                else None
            ),
        }

    @app.function(
        image=openfold3_image,
        cpu=4.0,
        memory=16384,
        gpu="L4",
        timeout=(
            NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
            + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
        ),
        max_containers=1,
        volumes={OPENFOLD_CACHE_ROOT: openfold_cache},
        secrets=[control_plane_secret],
    )
    def run_openfold3_retry(task_json: str | dict[str, Any]) -> dict[str, Any]:
        canonical_json = _normalise_task_json(task_json)
        if _method_name(canonical_json) != "openfold3":
            raise ValueError("the OpenFold3 retry worker accepts only OpenFold3 tasks")
        openfold_cache.reload()
        result = _execute(canonical_json)
        openfold_cache.commit()
        return result

    @app.function(
        image=boltz2_image,
        cpu=4.0,
        memory=16384,
        gpu="L4",
        timeout=(
            NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
            + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
        ),
        max_containers=1,
        volumes={BOLTZ_CACHE_ROOT: boltz_cache},
        secrets=[control_plane_secret],
    )
    def run_boltz2_retry(task_json: str | dict[str, Any]) -> dict[str, Any]:
        canonical_json = _normalise_task_json(task_json)
        if _method_name(canonical_json) != "boltz2":
            raise ValueError("the Boltz-2 retry worker accepts only Boltz-2 tasks")
        boltz_cache.reload()
        result = _execute(canonical_json)
        boltz_cache.commit()
        return result

    def _retry_function(retry_request: Mapping[str, Any]):
        method = retry_request.get("method")
        retry_kind = retry_request.get("retry_kind")
        function = (
            run_openfold3_retry
            if method == "openfold3"
            else run_boltz2_retry if method == "boltz2" else None
        )
        if function is None:
            raise ValueError("unsupported retry method")
        if retry_kind == "gpu_out_of_memory":
            cpu, memory = MODAL_HOST_BY_CLASS["a100-40gb"]
            return function.with_options(
                gpu=MODAL_GPU_BY_CLASS["a100-40gb"],
                cpu=cpu,
                memory=memory,
                timeout=(
                    NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
                    + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
                ),
                max_containers=1,
            )
        if retry_kind == "msa_generation_timeout":
            return function.with_options(
                timeout=(
                    NEXTWEEKLY_MSA_RETRY_TIMEOUT_SECONDS
                    + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
                ),
                max_containers=1,
            )
        if retry_kind in {"msa_preprocessing_failed", "repeat_once"}:
            return function.with_options(
                timeout=(
                    NEXTWEEKLY_OOM_RETRY_TIMEOUT_SECONDS
                    + NEXTWEEKLY_RETRY_OUTER_GRACE_SECONDS
                ),
                max_containers=1,
            )
        raise ValueError("unsupported retry kind")

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=5 * 60,
        max_containers=1,
    )
    def retry_prediction_runs(
        retry_requests: list[dict[str, Any]],
        resubmit_already_authorized: bool = False,
    ) -> dict[str, Any]:
        """Authorize and spawn an exact, resource-bounded retry batch."""

        from foldarium_pipeline.supabase import SupabaseCoordinator

        authorization = SupabaseCoordinator.from_env().authorize_prediction_retries(
            retry_requests,
            resubmit_already_authorized=resubmit_already_authorized,
        )
        task_payloads = authorization.pop("task_payloads")
        request_by_id = {
            request["run_id"]: request
            for request in authorization["retry_requests"]
        }
        submissions: list[dict[str, Any]] = []
        submission_errors: list[dict[str, str]] = []
        for run_id in authorization["approved_submission_run_ids"]:
            request = request_by_id[run_id]
            try:
                retry_task = _retry_execution_task(task_payloads[run_id], request)
                canonical_json = _normalise_task_json(retry_task)
                call_id = _retry_function(request).spawn(canonical_json).object_id
            except Exception as exc:
                submission_errors.append(
                    {
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                        "error": "Modal did not acknowledge the retry spawn",
                    }
                )
                continue
            submissions.append(
                {
                    "run_id": run_id,
                    "modal_call_id": call_id,
                    "retry_kind": request["retry_kind"],
                    "gpu_class": request["retry_gpu_class"],
                    "timeout_seconds": request["retry_timeout_seconds"],
                }
            )
        if submission_errors and submissions:
            status = "partially-submitted"
        elif submission_errors:
            status = "submission-failed"
        elif submissions:
            status = "submitted"
        else:
            status = "not-resubmitted"
        return {
            **authorization,
            "submission_status": status,
            "submissions": submissions,
            "submission_errors": submission_errors,
            "submitted_run_ids": [item["run_id"] for item in submissions],
            "authorized_not_submitted_run_ids": [
                item["run_id"] for item in submission_errors
            ],
        }

    @app.function(
        image=control_image,
        cpu=1.0,
        memory=2048,
        secrets=[control_plane_secret],
        timeout=15 * 60,
        max_containers=1,
    )
    def register_weekly_expansion(plan_json: str | dict[str, Any]) -> dict[str, Any]:
        """Append reviewed tasks to an existing capped weekly campaign.

        This registration-only seam never spawns a prediction. The caller must
        separately submit the returned run IDs, which preserves a review point
        between durable control-plane creation and metered GPU fan-out.
        """

        from foldarium_pipeline.intake import ADAPTER_VERSION
        from foldarium_pipeline.supabase import SupabaseCoordinator

        if isinstance(plan_json, str):
            plan = json.loads(plan_json)
        elif isinstance(plan_json, Mapping):
            plan = dict(plan_json)
        else:
            raise TypeError("plan_json must be a JSON object or serialized object")
        return SupabaseCoordinator.from_env().append_weekly_plan(
            plan,
            adapter_version=ADAPTER_VERSION,
            max_attempts=1,
        )

    @app.function(
        image=quiz_assembly_image,
        cpu=8.0,
        memory=32768,
        secrets=[control_plane_secret],
        timeout=WEEKLY_ASSEMBLY_TIMEOUT_SECONDS,
        max_containers=1,
    )
    def assemble_weekly_quiz_round(
        campaign_id: str,
        round_id: str,
        opens_at: str,
        closes_at: str,
        open_round: bool = False,
        include_pose_metrics: bool = False,
        beta: bool = False,
        round_environment: str = "production",
        public_quiz_bucket: str | None = None,
        excluded_target_ids: str = "",
        reuse_pose_metrics_from_round_id: str | None = None,
    ) -> dict[str, Any]:
        """Assemble complete method pairs and optionally open the blind round."""

        import tempfile

        from foldarium_pipeline.supabase import SupabaseConfigurationError, SupabaseCoordinator
        from foldarium_pipeline.weekly_quiz import (
            REQUIRED_METHODS,
            publish_staged_weekly_quiz,
            regenerate_promoted_selector_kit,
            select_complete_method_pairs,
            stage_weekly_quiz,
        )

        private = SupabaseCoordinator.from_env()
        outputs = private.campaign_prediction_outputs(campaign_id)
        complete, omitted, replacements = select_complete_method_pairs(
            outputs, REQUIRED_METHODS
        )
        operator_excluded_target_ids = {
            value.strip()
            for value in excluded_target_ids.split(",")
            if value.strip()
        }
        available_target_ids = {
            row.get("target_id") for row in complete if isinstance(row.get("target_id"), str)
        }
        unknown_exclusions = operator_excluded_target_ids.difference(
            available_target_ids
        )
        if unknown_exclusions:
            raise RuntimeError(
                "excluded target IDs are absent from complete method pairs: "
                + ", ".join(sorted(unknown_exclusions))
            )
        if operator_excluded_target_ids:
            complete = [
                row
                for row in complete
                if row.get("target_id") not in operator_excluded_target_ids
            ]
        if not complete:
            raise RuntimeError("campaign has no complete two-method target pairs")

        try:
            public_bucket = _weekly_public_bucket(public_quiz_bucket)
        except ValueError as exc:
            raise SupabaseConfigurationError(
                f"missing or invalid {PUBLIC_QUIZ_BUCKET_ENV}"
            ) from exc
        public_environment = dict(os.environ)
        public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
        public = SupabaseCoordinator.from_env(public_environment)
        if public.storage_bucket == private.storage_bucket:
            raise SupabaseConfigurationError("public quiz bucket must differ from predictions")

        choice_scorer = None
        choice_batch_scorer = None
        remote_scorer = None
        remote_scorer_lock = threading.Lock()

        def score_choice_remotely(*, protein_path, ligand_path, ligand_smiles, pose_id):
            nonlocal remote_scorer
            if remote_scorer is None:
                with remote_scorer_lock:
                    if remote_scorer is None:
                        remote_scorer = modal.Function.from_name(
                            WEEKLY_SCORING_APP_NAME,
                            WEEKLY_SCORING_FUNCTION_NAME,
                        )
            protein = Path(protein_path).read_bytes()
            ligand = Path(ligand_path).read_bytes()
            return remote_scorer.remote(
                protein,
                ligand,
                ligand_smiles,
                pose_id,
                hashlib.sha256(protein).hexdigest(),
                hashlib.sha256(ligand).hexdigest(),
            )

        if reuse_pose_metrics_from_round_id:
            from foldarium_pipeline.clustering import choice_order_digest

            source_round, source_private_bytes = private.weekly_quiz_reveal_inputs(
                reuse_pose_metrics_from_round_id
            )
            if source_round.get("campaign_id") != campaign_id:
                raise RuntimeError("pose-metric source round belongs to another campaign")
            try:
                source_private = json.loads(source_private_bytes)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("pose-metric source private index is invalid") from exc
            reusable_scores: dict[str, Mapping[str, Any]] = {}
            for item in source_private.get("items", []):
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    raise RuntimeError("pose-metric source contains an invalid item")
                for choice in item.get("choices", []):
                    if not isinstance(choice, Mapping):
                        raise RuntimeError("pose-metric source contains an invalid choice")
                    identity = {
                        "run_id": choice.get("run_id"),
                        "sample_id": choice.get("sample_id"),
                        "artifact_sha256": choice.get("artifact_sha256"),
                    }
                    pose_id = choice_order_digest(
                        round_id, item["id"], identity
                    )
                    scoring = choice.get("scoring")
                    if not isinstance(scoring, Mapping) or pose_id in reusable_scores:
                        raise RuntimeError(
                            "pose-metric source is incomplete or contains duplicate choices"
                        )
                    reusable_scores[pose_id] = scoring

            def score_choice(*, protein_path, ligand_path, ligand_smiles, pose_id):
                source_scoring = reusable_scores.get(pose_id)
                if source_scoring is None:
                    if not include_pose_metrics:
                        raise RuntimeError(
                            "pose-metric source lacks an exact run/sample choice"
                        )
                    return score_choice_remotely(
                        protein_path=protein_path,
                        ligand_path=ligand_path,
                        ligand_smiles=ligand_smiles,
                        pose_id=pose_id,
                    )
                reused = deepcopy(dict(source_scoring))
                reused["pose_id"] = pose_id
                provenance = deepcopy(dict(reused.get("provenance") or {}))
                provenance["inputs"] = {
                    "protein_sha256": hashlib.sha256(
                        Path(protein_path).read_bytes()
                    ).hexdigest(),
                    "ligand_pose_sha256": hashlib.sha256(
                        Path(ligand_path).read_bytes()
                    ).hexdigest(),
                }
                provenance["metric_reuse"] = {
                    "source_round_id": reuse_pose_metrics_from_round_id,
                    "policy": "rigid-transform-invariant-fixed-pose-metrics/v1",
                }
                reused["provenance"] = provenance
                return reused

            choice_scorer = score_choice

        elif include_pose_metrics:
            choice_scorer = score_choice_remotely

        if choice_scorer is not None:
            def score_choice_batch(requests):
                return _score_weekly_choices_concurrently(choice_scorer, requests)

            choice_batch_scorer = score_choice_batch

        with tempfile.TemporaryDirectory(prefix="foldarium-weekly-quiz-") as temporary:
            stage = stage_weekly_quiz(
                complete,
                temporary,
                round_id=round_id,
                campaign_id=campaign_id,
                downloader=private.download_content_object,
                choice_batch_scorer=choice_batch_scorer,
                target_workers=WEEKLY_ASSEMBLY_TARGET_WORKERS,
                artifact_download_workers=WEEKLY_ASSEMBLY_TARGET_WORKERS,
                artifact_cache_directory="/tmp/foldarium-weekly-input-cache",
            )
            published = publish_staged_weekly_quiz(
                temporary,
                private_coordinator=private,
                public_coordinator=public,
                opens_at=opens_at,
                closes_at=closes_at,
                open_round=open_round,
                round_environment=round_environment,
                round_metadata={
                    "release_channel": "beta" if beta else "standard",
                    "assembly_excluded_target_ids": sorted(
                        operator_excluded_target_ids
                    ),
                    "assembly_exclusion_policy": (
                        "explicit-operator-reviewed-target-exclusion/v1"
                        if operator_excluded_target_ids
                        else None
                    ),
                    "pose_metrics_reused_from_round_id": (
                        reuse_pose_metrics_from_round_id
                    ),
                },
            )
        cluster_counts = [
            int(item["clustering"]["cluster_count"])
            for item in stage["items"]
        ]
        summary = {
            **published,
            "complete_target_count": len(stage["items"]),
            "cluster_count_total": sum(cluster_counts),
            "cluster_count_min": min(cluster_counts),
            "cluster_count_max": max(cluster_counts),
            "omitted_succeeded_partial_targets": omitted,
            "ignored_succeeded_replacement_runs": replacements,
            "pose_metrics_included": bool(
                include_pose_metrics or reuse_pose_metrics_from_round_id
            ),
            "pose_metrics_reused_from_round_id": reuse_pose_metrics_from_round_id,
            "release_channel": "beta" if beta else "standard",
            "environment": round_environment,
            "assembly_excluded_target_ids": sorted(operator_excluded_target_ids),
        }
        print(
            "foldarium.weekly_quiz_assembly "
            + json.dumps(
                {
                    key: summary[key]
                    for key in (
                        "status",
                        "round_id",
                        "complete_target_count",
                        "item_count",
                        "choice_count",
                        "cluster_count_total",
                        "cluster_count_min",
                        "cluster_count_max",
                        "blind_manifest_sha256",
                    )
                },
                sort_keys=True,
            )
        )
        return summary

    @app.function(
        image=quiz_assembly_image,
        cpu=1.0,
        memory=2048,
        secrets=[control_plane_secret],
        timeout=15 * 60,
        max_containers=1,
    )
    def promote_weekly_quiz_round(
        source_round_id: str,
        round_id: str,
        opens_at: str,
        closes_at: str,
        source_environment: str = "preview",
        round_environment: str = "production",
        beta: bool = True,
        open_round: bool = False,
        minimum_cluster_count: int = 1,
        additional_excluded_item_ids: str = "",
    ) -> dict[str, Any]:
        """Clone one exact reviewed round without recomputing its pose assets."""

        from foldarium_pipeline.contracts import canonical_json
        from foldarium_pipeline.quiz import manifest_sha256
        from foldarium_pipeline.supabase import SupabaseConfigurationError, SupabaseCoordinator
        from foldarium_pipeline.weekly_quiz import (
            clone_weekly_quiz_manifests,
            regenerate_promoted_selector_kit,
        )

        coordinator = SupabaseCoordinator.from_env()
        try:
            public_bucket = _weekly_public_bucket()
        except ValueError as exc:
            raise SupabaseConfigurationError(
                f"missing or invalid {PUBLIC_QUIZ_BUCKET_ENV}"
            ) from exc
        public_environment = dict(os.environ)
        public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
        public = SupabaseCoordinator.from_env(public_environment)
        if public.storage_bucket == coordinator.storage_bucket:
            raise SupabaseConfigurationError("public quiz bucket must differ from predictions")
        source, private_content = coordinator.weekly_quiz_reveal_inputs(source_round_id)
        if source.get("environment") != source_environment:
            raise RuntimeError("source weekly round environment does not match")
        if source.get("status") not in {"open", "closed"}:
            raise RuntimeError("only an unrevealed weekly round can be promoted")
        try:
            private_index = json.loads(private_content)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("source private index is not valid JSON") from exc
        if (
            isinstance(minimum_cluster_count, bool)
            or not isinstance(minimum_cluster_count, int)
            or minimum_cluster_count < 1
        ):
            raise ValueError("minimum_cluster_count must be a positive integer")
        private_items = private_index.get("items")
        if not isinstance(private_items, list) or not private_items:
            raise RuntimeError("source private index has no weekly items")
        included_item_ids: set[str] = set()
        excluded_item_ids: set[str] = set()
        for item in private_items:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise RuntimeError("source private index contains an invalid item")
            clustering = item.get("clustering")
            cluster_count = clustering.get("cluster_count") if isinstance(clustering, Mapping) else None
            if isinstance(cluster_count, bool) or not isinstance(cluster_count, int):
                raise RuntimeError("source private index item has no valid cluster count")
            if cluster_count >= minimum_cluster_count:
                included_item_ids.add(item["id"])
            else:
                excluded_item_ids.add(item["id"])
        operator_excluded_item_ids = {
            value.strip()
            for value in additional_excluded_item_ids.split(",")
            if value.strip()
        }
        source_item_ids = {item["id"] for item in private_items}
        unknown_exclusions = operator_excluded_item_ids.difference(source_item_ids)
        if unknown_exclusions:
            raise RuntimeError(
                "additional excluded item IDs are absent from the source: "
                + ", ".join(sorted(unknown_exclusions))
            )
        included_item_ids.difference_update(operator_excluded_item_ids)
        excluded_item_ids.update(operator_excluded_item_ids)
        blind, promoted_private = clone_weekly_quiz_manifests(
            source["blind_manifest"],
            private_index,
            round_id=round_id,
            include_item_ids=included_item_ids,
        )
        private_object = coordinator.store_bytes(
            canonical_json(promoted_private).encode("utf-8"), "application/json"
        )
        source_metadata = dict(source.get("metadata") or {})
        source_metadata.pop("private_index", None)
        metadata = {
            **source_metadata,
            "private_index": private_object,
            "promoted_from_round_id": source_round_id,
            "promoted_from_blind_manifest_sha256": source["blind_manifest_sha256"],
            "release_channel": "beta" if beta else "standard",
            "quiz_item_filter": {
                "policy": "minimum-pose-cluster-count/v1",
                "minimum_cluster_count": minimum_cluster_count,
                "source_item_count": len(private_items),
                "included_item_count": len(included_item_ids),
                "excluded_item_ids": sorted(excluded_item_ids),
                "additional_exclusion_policy": (
                    "published-coordinate-frame-audit/v1"
                    if operator_excluded_item_ids
                    else None
                ),
            },
        }
        selector_kit = regenerate_promoted_selector_kit(
            source_round=source,
            source_metadata=source_metadata,
            promoted_blind_manifest=blind,
            public_coordinator=public,
            private_coordinator=coordinator,
            register_catalog=False,
        )
        metadata["selector_targets"] = selector_kit["selector_targets"]
        metadata["selector_kit"] = {
            "kit_sha256": selector_kit["kit_sha256"],
            "item_count": selector_kit["item_count"],
            "byte_size": selector_kit["byte_size"],
            "storage_path": selector_kit["storage_path"],
            "object_uri": selector_kit["object_uri"],
            "registered": False,
        }
        response: Any = {"status": "uploaded-not-opened"}
        if open_round:
            opened = coordinator.open_weekly_quiz_round(
                round_id=round_id,
                campaign_id=source["campaign_id"],
                opens_at=opens_at,
                closes_at=closes_at,
                blind_manifest=blind,
                metadata=metadata,
                environment=round_environment,
            )
            selector_kit["registration"] = coordinator.register_weekly_selector_kit(
                round_id=round_id,
                kit_sha256=selector_kit["kit_sha256"],
                item_count=int(selector_kit["item_count"]),
                byte_size=int(selector_kit["byte_size"]),
                storage_path=selector_kit["storage_path"],
                descriptor=selector_kit["descriptor"],
                blind_manifest_sha256=manifest_sha256(blind),
            )
            selector_kit["registered"] = True
            metadata["selector_kit"]["registered"] = True
            response = {
                "status": opened.get("status"),
                "round_id": opened.get("round_id"),
            }
        result = {
            "status": "opened" if open_round else "uploaded-not-opened",
            "round_id": round_id,
            "campaign_id": source["campaign_id"],
            "environment": round_environment,
            "release_channel": metadata["release_channel"],
            "source_round_id": source_round_id,
            "source_item_count": len(private_items),
            "minimum_cluster_count": minimum_cluster_count,
            "excluded_item_ids": sorted(excluded_item_ids),
            "item_count": len(blind["items"]),
            "choice_count": sum(len(item["choices"]) for item in blind["items"]),
            "blind_manifest_sha256": manifest_sha256(blind),
            "private_index": private_object,
            "selector_kit": selector_kit,
            "open_response": response,
        }
        print("foldarium.weekly_quiz_promotion " + json.dumps(result, sort_keys=True))
        return result

    @app.function(
        image=quiz_assembly_image,
        cpu=2.0,
        memory=8192,
        secrets=[control_plane_secret],
        timeout=15 * 60,
        max_containers=1,
    )
    def backfill_weekly_selector_kit(
        round_id: str | None = None,
        campaign_id: str | None = None,
        round_environment: str = "production",
    ) -> dict[str, Any]:
        """Publish a selector kit catalog row for the current or named round."""

        from foldarium_pipeline.supabase import SupabaseConfigurationError, SupabaseCoordinator
        from foldarium_pipeline.weekly_quiz import backfill_selector_kit_for_round

        private = SupabaseCoordinator.from_env()
        try:
            public_bucket = _weekly_public_bucket()
        except ValueError as exc:
            raise SupabaseConfigurationError(
                f"missing or invalid {PUBLIC_QUIZ_BUCKET_ENV}"
            ) from exc
        public_environment = dict(os.environ)
        public_environment["FOLDARIUM_STORAGE_BUCKET"] = public_bucket
        public = SupabaseCoordinator.from_env(public_environment)
        if public.storage_bucket == private.storage_bucket:
            raise SupabaseConfigurationError("public quiz bucket must differ from predictions")

        if round_id:
            round_row = private.weekly_quiz_round(round_id)
        else:
            if not campaign_id:
                raise ValueError("campaign_id is required when round_id is omitted")
            round_row = private.current_weekly_quiz_round(
                campaign_id,
                environment=round_environment,
            )
        selector_kit = backfill_selector_kit_for_round(
            round_row,
            public_coordinator=public,
            private_coordinator=private,
            register_catalog=True,
        )
        result = {
            "status": "selector-kit-registered",
            "round_id": round_row["round_id"],
            "environment": round_row.get("environment", round_environment),
            "kit_sha256": selector_kit["kit_sha256"],
            "item_count": selector_kit["item_count"],
            "byte_size": selector_kit["byte_size"],
            "storage_path": selector_kit["storage_path"],
        }
        print("foldarium.weekly_selector_backfill " + json.dumps(result, sort_keys=True))
        return result

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=5 * 60,
        max_containers=1,
    )
    def configure_delayed_weekly_retrospective(
        round_id: str,
        expected_closes_at: str,
        safety_closes_at: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Opt one exact open production round into next-round release timing."""

        from foldarium_pipeline.supabase import SupabaseCoordinator

        if not isinstance(apply, bool):
            raise TypeError("apply must be a boolean")
        coordinator = SupabaseCoordinator.from_env()
        before = coordinator.weekly_quiz_round(round_id)
        updated = (
            coordinator.configure_delayed_weekly_retrospective(
                round_id,
                expected_closes_at=expected_closes_at,
                safety_closes_at=safety_closes_at,
            )
            if apply
            else before
        )
        result = {
            "status": "configured" if apply else "planned",
            "round_id": round_id,
            "apply": apply,
            "previous_closes_at": before.get("closes_at"),
            "closes_at": (
                updated.get("closes_at") if apply else safety_closes_at
            ),
            "policy": "next-weekly-activation",
        }
        print(
            "foldarium.delayed_weekly_retrospective "
            + json.dumps(result, sort_keys=True),
            flush=True,
        )
        return result

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=5 * 60,
        max_containers=1,
    )
    def extend_delayed_weekly_voting_window(
        round_id: str,
        expected_safety_closes_at: str,
        new_safety_closes_at: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Extend one exact delayed round while keeping its reveal private."""

        from foldarium_pipeline.supabase import SupabaseCoordinator
        from foldarium_pipeline.weekly_lifecycle import delayed_retrospective_release

        if not isinstance(apply, bool):
            raise TypeError("apply must be a boolean")
        coordinator = SupabaseCoordinator.from_env()
        before = coordinator.weekly_quiz_round(round_id)
        release = delayed_retrospective_release(before)
        if release is None:
            raise RuntimeError("weekly round has no delayed retrospective policy")
        updated = (
            coordinator.extend_delayed_weekly_voting_window(
                round_id,
                expected_safety_closes_at=expected_safety_closes_at,
                new_safety_closes_at=new_safety_closes_at,
            )
            if apply
            else before
        )
        result = {
            "status": "extended" if apply else "planned",
            "round_id": round_id,
            "apply": apply,
            "previous_closes_at": before.get("closes_at"),
            "closes_at": (
                updated.get("closes_at")
                if apply
                else new_safety_closes_at
            ),
            "prepared_evaluation_will_be_superseded": isinstance(
                release.get("prepared_evaluation"),
                Mapping,
            ),
            "reveal_mutation_enabled": False,
        }
        print(
            "foldarium.delayed_weekly_voting_extension "
            + json.dumps(result, sort_keys=True),
            flush=True,
        )
        return result

    @app.function(
        image=quiz_assembly_image,
        cpu=8.0,
        memory=32768,
        secrets=[control_plane_secret],
        timeout=2 * 60 * 60,
        max_containers=1,
    )
    def materialize_private_weekly_evaluation(
        round_id: str,
        publish: bool | None = None,
    ) -> dict[str, Any]:
        """Materialize one allow-listed pre-close evaluation in private storage."""

        if publish is not False:
            raise RuntimeError(
                "private weekly evaluation requires explicit --no-publish"
            )

        import tempfile

        from foldarium_pipeline.private_evaluation import (
            materialize_private_preclose_evaluation,
        )
        from foldarium_pipeline.supabase import SupabaseCoordinator

        coordinator = SupabaseCoordinator.from_env()
        with tempfile.TemporaryDirectory(
            prefix="foldarium-private-weekly-evaluation-"
        ) as temporary:
            result = materialize_private_preclose_evaluation(
                round_id,
                temporary,
                coordinator=coordinator,
            )
        print(
            "foldarium.private_weekly_evaluation "
            + json.dumps(
                {
                    "round_id": result.get("round_id"),
                    "evaluation_id": result.get("evaluation_id"),
                    "status": result.get("status"),
                    "item_count": result.get("item_count"),
                    "choice_count": result.get("choice_count"),
                    "artifact_sha256": result.get("artifact", {}).get("sha256"),
                    "mode": "private-no-publish",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return {**result, "mode": "private-no-publish", "mutation_enabled": False}

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=1024,
        schedule=(
            modal.Cron(WEEKLY_PRODUCTION_CRON_UTC)
            if WEEKLY_PRODUCTION_ENABLED
            else None
        ),
        secrets=[control_plane_secret],
        timeout=15 * 60,
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("weekly_production_promotion_tick")
    def weekly_production_promotion_tick(
        release_date: str | None = None,
        open_round_override: bool | None = None,
        register_selector_kit_override: bool | None = None,
    ) -> dict[str, Any]:
        """Promote one immutable Preview round into production without fabricating votes.

        Repeated ticks are idempotent: an existing production round short-circuits
        before any manifest mutation. Opening and selector-kit registration remain
        separately gated so a deployment can stage manifests first.
        """

        from foldarium_pipeline.supabase import SupabaseCoordinator

        for field, value in (
            ("open_round_override", open_round_override),
            ("register_selector_kit_override", register_selector_kit_override),
        ):
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{field} must be a boolean or null")
        window = _weekly_production_window(release_date)
        private, public = _weekly_quiz_public_private_coordinators()
        open_round = (
            os.environ.get(WEEKLY_PRODUCTION_OPEN_ENV) == "1"
            if open_round_override is None
            else open_round_override
        )
        register_selector_kit = (
            os.environ.get(WEEKLY_REGISTER_SELECTOR_KIT_ENV) == "1"
            if register_selector_kit_override is None
            else register_selector_kit_override
        )
        production_exists = private.weekly_quiz_round_exists(window["round_id"])
        preview_exists = private.weekly_quiz_round_exists(window["preview_round_id"])
        previous_round_id = None
        if open_round and not production_exists and preview_exists:
            try:
                candidate = private.current_weekly_quiz_round().get("round_id")
                if isinstance(candidate, str) and candidate != window["round_id"]:
                    previous_round_id = candidate
            except Exception:
                previous_round_id = None
        selector_result = None
        promoted = None
        just_promoted = False
        if production_exists:
            if open_round and register_selector_kit:
                selector_result = _attempt_production_selector_kit_registration(
                    window["round_id"],
                    private_coordinator=private,
                    public_coordinator=public,
                )
        elif preview_exists:
            promoted = promote_weekly_quiz_round.remote(
                window["preview_round_id"],
                window["round_id"],
                window["opens_at"],
                window["closes_at"],
                "preview",
                window["environment"],
                True,
                open_round,
            )
            just_promoted = True
            if open_round and register_selector_kit:
                selector_result = _attempt_production_selector_kit_registration(
                    window["round_id"],
                    private_coordinator=private,
                    public_coordinator=public,
                )
        handoff_spawned = False
        if just_promoted and open_round and previous_round_id is not None:
            from foldarium_pipeline.weekly_lifecycle import (
                delayed_retrospective_release,
            )

            previous = private.weekly_quiz_round(previous_round_id)
            if delayed_retrospective_release(previous) is not None:
                delayed_weekly_retrospective_handoff.spawn(
                    previous_round_id,
                    window["round_id"],
                    True,
                )
                handoff_spawned = True
        selector_fields = _production_selector_kit_status(
            open_round=open_round,
            register_selector_kit=register_selector_kit,
            selector_result=selector_result,
        )
        outcome = {
            **window,
            "status": _production_tick_status(
                production_exists=production_exists or just_promoted,
                preview_exists=preview_exists,
                open_round=open_round,
                selector_kit_retryable=selector_fields["selector_kit_retryable"],
                selector_kit_status=selector_fields["selector_kit_status"],
                just_promoted=just_promoted,
            ),
            "open_round": open_round,
            "register_selector_kit": register_selector_kit,
            "previous_round_id": previous_round_id,
            "delayed_retrospective_handoff_spawned": handoff_spawned,
            **selector_fields,
        }
        if promoted is not None:
            outcome["promotion"] = promoted
        print(
            "foldarium.weekly_production "
            + json.dumps(
                {key: value for key, value in outcome.items() if key != "promotion"},
                sort_keys=True,
            ),
            flush=True,
        )
        return outcome

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=2 * 60,
        max_containers=1,
    )
    def weekly_lifecycle_preflight(
        release_date: str | None = None,
    ) -> dict[str, Any]:
        """Return a read-only lifecycle status report for operator review."""

        from foldarium_pipeline.supabase import SupabaseCoordinator

        coordinator = SupabaseCoordinator.from_env()
        preview = _nextweekly_window(release_date)
        production = _weekly_production_window(release_date)
        current_round = None
        current_round_error = None
        try:
            current_round = coordinator.current_weekly_quiz_round()["round_id"]
        except Exception as exc:
            current_round_error = type(exc).__name__
        report = {
            "deployment": _lifecycle_deployment_report(),
            "preview": {
                **preview,
                "exists": coordinator.weekly_quiz_round_exists(preview["round_id"]),
            },
            "production": {
                **production,
                "exists": coordinator.weekly_quiz_round_exists(production["round_id"]),
            },
            "campaign_registered": coordinator.weekly_campaign_exists(
                preview["campaign_id"]
            ),
            "current_production_round_id": current_round,
            "current_production_round_error": current_round_error,
            "required_migrations_before_publication": [
                REQUIRED_RETROSPECTIVE_MIGRATION
            ],
            "selector_kit_module_available": importlib.util.find_spec(
                "foldarium_pipeline.weekly_selector"
            )
            is not None,
        }
        print(
            "foldarium.weekly_lifecycle_preflight "
            + json.dumps(report, sort_keys=True),
            flush=True,
        )
        return report

    @app.function(
        image=quiz_assembly_image,
        cpu=8.0,
        memory=32768,
        schedule=(
            modal.Cron(WEEKLY_RETROSPECTIVE_CRON_UTC)
            if WEEKLY_RETROSPECTIVE_ENABLED
            else None
        ),
        secrets=[control_plane_secret],
        timeout=2 * 60 * 60,
        retries=modal.Retries(
            max_retries=WEDNESDAY_REVEAL_MODAL_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=60.0,
            max_delay=60.0,
        ),
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("weekly_retrospective_tick")
    def weekly_retrospective_tick(
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """Prepare opted-in rounds pre-close or catalog closed rounds privately."""

        import tempfile

        from foldarium_pipeline.private_evaluation import (
            materialize_delayed_preclose_weekly_evaluation,
            materialize_postclose_weekly_evaluation,
        )
        from foldarium_pipeline.supabase import SupabaseCoordinator
        from foldarium_pipeline.weekly_lifecycle import delayed_retrospective_release

        coordinator = SupabaseCoordinator.from_env()
        selected_round_id = round_id
        if selected_round_id is None:
            selected_round_id = coordinator.current_weekly_quiz_round(
                _default_weekly_campaign_id()
            )["round_id"]
        prepare_preclose = False
        if hasattr(coordinator, "weekly_quiz_round"):
            round_record = coordinator.weekly_quiz_round(selected_round_id)
            delayed_release = delayed_retrospective_release(round_record)
            closes_at = datetime.fromisoformat(
                str(round_record.get("closes_at")).replace("Z", "+00:00")
            )
            if closes_at.tzinfo is None:
                raise RuntimeError("weekly round closes_at must include a timezone")
            prepare_preclose = (
                delayed_release is not None
                and datetime.now(timezone.utc) < closes_at
            )
        with tempfile.TemporaryDirectory(
            prefix="foldarium-weekly-retrospective-"
        ) as temporary:
            result = (
                materialize_delayed_preclose_weekly_evaluation(
                    selected_round_id,
                    temporary,
                    coordinator=coordinator,
                )
                if prepare_preclose
                else materialize_postclose_weekly_evaluation(
                    selected_round_id,
                    temporary,
                    coordinator=coordinator,
                )
            )
        outcome = {
            **result,
            "mode": (
                "private-delayed-preclose"
                if prepare_preclose
                else "private-postclose"
            ),
            "private_catalog_mutation_enabled": True,
            "public_mutation_enabled": False,
        }
        print(
            "foldarium.weekly_retrospective "
            + json.dumps(
                {
                    "round_id": outcome.get("round_id"),
                    "evaluation_id": outcome.get("evaluation_id"),
                    "status": outcome.get("status"),
                    "item_count": outcome.get("item_count"),
                    "choice_count": outcome.get("choice_count"),
                    "artifact_sha256": outcome.get("artifact", {}).get("sha256"),
                    "mode": outcome["mode"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return outcome

    @app.function(
        image=control_image,
        cpu=1.0,
        memory=2048,
        schedule=(
            modal.Cron(WEEKLY_RETROSPECTIVE_PUBLICATION_CRON_UTC)
            if WEEKLY_RETROSPECTIVE_PUBLICATION_ENABLED
            else None
        ),
        secrets=[control_plane_secret],
        timeout=30 * 60,
        retries=modal.Retries(
            max_retries=WEDNESDAY_REVEAL_MODAL_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=60.0,
            max_delay=60.0,
        ),
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("weekly_retrospective_publication_tick")
    def weekly_retrospective_publication_tick(
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """Publish one exact round or backfill all missing revealed rounds."""

        from foldarium_pipeline.retrospective_archive import (
            publish_missing_retrospectives,
        )
        from foldarium_pipeline.supabase import SupabaseCoordinator

        result = publish_missing_retrospectives(
            coordinator=SupabaseCoordinator.from_env(),
            round_id=round_id,
        )
        print(
            "foldarium.weekly_retrospective_publication "
            + json.dumps(
                {
                    "status": result["status"],
                    "requested_round_id": result["requested_round_id"],
                    "round_count": result["round_count"],
                    "round_ids": result["round_ids"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return {
            **result,
            "mode": "post-reveal-publication",
            "schedule_enabled": WEEKLY_RETROSPECTIVE_PUBLICATION_ENABLED,
            "admin_artifacts_private": True,
        }

    @app.function(
        image=quiz_assembly_image,
        cpu=8.0,
        memory=32768,
        secrets=[control_plane_secret],
        timeout=2 * 60 * 60,
        retries=modal.Retries(
            max_retries=WEDNESDAY_REVEAL_MODAL_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=60.0,
            max_delay=60.0,
        ),
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("delayed_weekly_retrospective_handoff")
    def delayed_weekly_retrospective_handoff(
        previous_round_id: str,
        successor_round_id: str,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Close, reveal, and retrospectivize an opted-in predecessor."""

        import tempfile

        from foldarium_pipeline.private_evaluation import (
            describe_private_evaluation_artifact,
            materialize_postclose_weekly_evaluation,
        )
        from foldarium_pipeline.quiz import manifest_sha256
        from foldarium_pipeline.retrospective_archive import (
            publish_missing_retrospectives,
        )
        from foldarium_pipeline.supabase import SupabaseCoordinator
        from foldarium_pipeline.weekly_lifecycle import delayed_retrospective_release

        if not isinstance(apply, bool):
            raise TypeError("apply must be a boolean")
        coordinator = SupabaseCoordinator.from_env()
        previous = coordinator.weekly_quiz_round(previous_round_id)
        successor = coordinator.weekly_quiz_round(successor_round_id)
        release = delayed_retrospective_release(previous)
        if release is None:
            raise RuntimeError("previous round has no delayed retrospective policy")
        if successor.get("environment") != "production":
            raise RuntimeError("successor round must be production")
        if not apply:
            return {
                "status": "planned",
                "previous_round_id": previous_round_id,
                "successor_round_id": successor_round_id,
                "previous_closes_at": previous.get("closes_at"),
                "apply": False,
            }

        closed = coordinator.close_delayed_weekly_round_for_successor(
            previous_round_id,
            successor_round_id,
        )
        with tempfile.TemporaryDirectory(
            prefix="foldarium-delayed-weekly-handoff-"
        ) as temporary:
            evaluation = materialize_postclose_weekly_evaluation(
                previous_round_id,
                temporary,
                coordinator=coordinator,
            )
        catalog = coordinator.private_weekly_evaluation(previous_round_id)
        if not isinstance(catalog, Mapping):
            raise RuntimeError("delayed weekly handoff has no private evaluation")
        content = coordinator.download_content_object(
            catalog.get("artifact_object_uri"),
            expected_sha256=catalog.get("artifact_sha256"),
        )
        described = describe_private_evaluation_artifact(
            content,
            expected_artifact_sha256=catalog.get("artifact_sha256"),
        )
        try:
            artifact = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("private evaluation artifact is invalid") from exc
        reveal_manifest = (
            artifact.get("reveal_manifest")
            if isinstance(artifact, Mapping)
            else None
        )
        if (
            not isinstance(reveal_manifest, Mapping)
            or manifest_sha256(reveal_manifest)
            != described.get("reveal_manifest_sha256")
        ):
            raise RuntimeError("private evaluation reveal manifest is invalid")
        reveal_response = coordinator.reveal_weekly_quiz_round(
            round_id=previous_round_id,
            reveal_manifest=reveal_manifest,
        )
        publication = publish_missing_retrospectives(
            coordinator=coordinator,
            round_id=previous_round_id,
        )
        outcome = {
            "status": "complete",
            "previous_round_id": previous_round_id,
            "successor_round_id": successor_round_id,
            "previous_closes_at": closed.get("closes_at"),
            "evaluation_status": evaluation.get("status"),
            "reveal_status": (
                reveal_response.get("status")
                if isinstance(reveal_response, Mapping)
                else None
            ),
            "retrospective_status": publication.get("status"),
            "apply": True,
        }
        print(
            "foldarium.delayed_weekly_handoff "
            + json.dumps(outcome, sort_keys=True),
            flush=True,
        )
        return outcome

    @app.function(
        image=quiz_assembly_image,
        cpu=8.0,
        memory=32768,
        schedule=(
            modal.Cron(WEDNESDAY_REVEAL_CRON_UTC)
            if WEDNESDAY_REVEAL_ENABLED
            else None
        ),
        secrets=[control_plane_secret],
        timeout=2 * 60 * 60,
        retries=modal.Retries(
            max_retries=WEDNESDAY_REVEAL_MODAL_RETRIES,
            backoff_coefficient=1.0,
            initial_delay=60.0,
            max_delay=60.0,
        ),
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("wednesday_reveal_tick")
    def wednesday_reveal_tick(
        round_id: str | None = None,
        publish: bool | None = None,
    ) -> dict[str, Any]:
        """Evaluate one exact blind round and optionally publish it atomically.

        Scheduled calls follow the environment's actual current round and use
        the classic PDB target IDs stored in that round's private index.
        ``publish`` is an explicit per-call override; when omitted, the
        deployment must set the mutation gate to ``1``.
        """

        import tempfile

        from foldarium_pipeline.supabase import (
            SupabaseCoordinator,
            SupabasePublicationError,
        )
        from foldarium_pipeline.private_evaluation import (
            recover_legacy_ligand_eligibility,
        )
        from foldarium_pipeline.wednesday_reveal import (
            WednesdayRevealNotReady,
            run_wednesday_reveal,
        )
        from foldarium_pipeline.weekly_lifecycle import delayed_retrospective_release

        mutation_enabled = _wednesday_publish_enabled(publish)
        coordinator = SupabaseCoordinator.from_env()
        selected_round_id = round_id
        if selected_round_id is None:
            selected_round_id = coordinator.current_weekly_quiz_round()["round_id"]
        try:
            round_record, private_index_content = (
                coordinator.weekly_quiz_reveal_inputs(selected_round_id)
            )
        except SupabasePublicationError as exc:
            # A round or its private index can arrive just after the first
            # scheduled tick. Surface it as retryable while retaining the
            # service's fail-closed publication boundary.
            raise WednesdayRevealNotReady(
                f"weekly reveal inputs are not ready for {selected_round_id}"
            ) from exc

        delayed_release = delayed_retrospective_release(round_record)
        if (
            delayed_release is not None
            and delayed_release.get("activated_by_round_id") is None
        ):
            outcome = {
                "status": "awaiting-next-weekly-activation",
                "round_id": selected_round_id,
                "closes_at": round_record.get("closes_at"),
                "mode": "publish" if mutation_enabled else "dry-run",
                "mutation_enabled": mutation_enabled,
            }
            print(
                "foldarium.wednesday_reveal "
                + json.dumps(outcome, sort_keys=True),
                flush=True,
            )
            return outcome

        closes_at = round_record.get("closes_at")
        if isinstance(closes_at, str):
            try:
                parsed_close = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
            except ValueError:
                parsed_close = None
            if (
                parsed_close is not None
                and parsed_close.tzinfo is not None
                and datetime.now(timezone.utc) < parsed_close
            ):
                outcome = {
                    "status": "voting-open",
                    "round_id": selected_round_id,
                    "closes_at": closes_at,
                    "mode": "publish" if mutation_enabled else "dry-run",
                    "mutation_enabled": mutation_enabled,
                }
                print(
                    "foldarium.wednesday_reveal "
                    + json.dumps(outcome, sort_keys=True),
                    flush=True,
                )
                return outcome

        def prediction_resolver(choice: Mapping[str, Any]) -> dict[str, Any]:
            return coordinator.download_predicted_complex(
                choice.get("run_id"), choice.get("sample_id")
            )

        recovered_ligand_eligibility = recover_legacy_ligand_eligibility(
            coordinator,
            round_record,
            private_index_content,
        )
        reveal_publisher = (
            coordinator.reveal_weekly_quiz_round if mutation_enabled else None
        )
        with tempfile.TemporaryDirectory(prefix="foldarium-wednesday-reveal-") as temporary:
            result = run_wednesday_reveal(
                round_record,
                private_index_content,
                temporary,
                prediction_resolver=prediction_resolver,
                reveal_publisher=reveal_publisher,
                recovered_ligand_eligibility=recovered_ligand_eligibility,
            )
        outcome = {
            **result,
            "mode": "publish" if mutation_enabled else "dry-run",
            "mutation_enabled": mutation_enabled,
        }
        print(
            "foldarium.wednesday_reveal "
            + json.dumps(
                {
                    "round_id": outcome.get("round_id"),
                    "status": outcome.get("status"),
                    "mode": outcome["mode"],
                    "item_count": outcome.get("item_count"),
                    "choice_count": outcome.get("choice_count"),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return outcome

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        schedule=modal.Cron(WEEKLY_CRON_UTC) if WEEKLY_CRON_ENABLED else None,
        secrets=[control_plane_secret],
        timeout=30 * 60,
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("weekly_tick")
    def weekly_tick() -> dict[str, Any]:
        """Deployment-owned cron seam for a provider-neutral campaign producer.

        The configured hook receives no Modal objects. It must return an iterable
        of PredictionTask JSON strings/mappings and may use Supabase for
        idempotent planning. Leaving the hook unset makes the cron a safe no-op.
        """

        outcome = _invoke_weekly_hook_pipeline(spawn_task=_spawn_task)
        print("foldarium.weekly " + json.dumps(outcome, sort_keys=True), flush=True)
        return outcome

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=512,
        secrets=[control_plane_secret],
        timeout=30 * 60,
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("weekly_intake_replay")
    def weekly_intake_replay(release_date: str, apply: bool = False) -> dict[str, Any]:
        """Operator recovery: replay one exact Saturday intake without opening Preview."""

        if not isinstance(apply, bool):
            raise TypeError("apply must be a boolean")
        validated = validate_intake_replay_release_date(
            release_date,
            max_age_days=WEEKLY_INTAKE_REPLAY_MAX_AGE_DAYS,
        )
        if not os.environ.get(WEEKLY_HOOK_ENV):
            raise RuntimeError(f"{WEEKLY_HOOK_ENV} is not configured")
        campaign_id = f"wwpdb-{validated}"
        overrides = {
            "FOLDARIUM_RELEASE_DATE": validated,
            "FOLDARIUM_WEEKLY_REGISTER": "1" if apply else "0",
            "FOLDARIUM_WEEKLY_SUBMIT": "1" if apply else "0",
        }
        with _temporary_environ(overrides):
            outcome = _invoke_weekly_hook_pipeline(
                submit=apply,
                spawn_task=_spawn_task if apply else None,
            )
        planned_task_count = int(outcome.get("count", 0))
        response = {
            **outcome,
            "mode": "apply" if apply else "dry-run",
            "release_date": validated,
            "campaign_id": campaign_id,
            "planned_task_count": planned_task_count,
            "apply": apply,
            "mutation_enabled": apply,
            "preview_or_production_round_created": False,
        }
        if outcome.get("status") == "already-registered":
            response["idempotent"] = True
            response["note"] = (
                "campaign already registered; no GPU tasks respawned by the hook"
            )
        print(
            "foldarium.weekly_intake_replay "
            + json.dumps(
                {
                    key: response[key]
                    for key in (
                        "mode",
                        "release_date",
                        "campaign_id",
                        "status",
                        "planned_task_count",
                        "apply",
                        "idempotent",
                    )
                    if key in response
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return response

    @app.function(
        image=control_image,
        cpu=0.25,
        memory=256,
        timeout=2 * 60,
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    def weekly_lifecycle_journal_tail(
        operation: str | None = None,
        correlation_id: str | None = None,
        phase: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read-only tail of durable Weekly lifecycle journal events."""

        if phase is not None and phase not in {"started", "succeeded", "failed"}:
            raise ValueError("phase must be started, succeeded, failed, or null")
        events = read_lifecycle_journal_events(
            _lifecycle_journal_root(),
            operation=operation,
            correlation_id=correlation_id,
            phase=phase,  # type: ignore[arg-type]
            since=since,
            limit=limit,
            reload=_lifecycle_journal_reload,
        )
        report = {
            "mount": WEEKLY_LIFECYCLE_JOURNAL_MOUNT,
            "volume": LIFECYCLE_JOURNAL_VOLUME_NAME,
            "count": len(events),
            "events": events,
        }
        print(
            "foldarium.lifecycle_journal_tail "
            + json.dumps({"count": report["count"]}, sort_keys=True),
            flush=True,
        )
        return report

    @app.function(
        image=control_image,
        cpu=0.5,
        memory=1024,
        schedule=(
            modal.Cron(NEXTWEEKLY_CRON_UTC)
            if NEXTWEEKLY_CRON_ENABLED
            else None
        ),
        secrets=[control_plane_secret],
        timeout=75 * 60,
        max_containers=1,
        volumes=LIFECYCLE_JOURNAL_VOLUMES,
    )
    @_weekly_lifecycle_journal("nextweekly_tick")
    def nextweekly_tick(release_date: str | None = None) -> dict[str, Any]:
        """Advance one Saturday campaign into an immutable Preview round.

        This scheduler never writes a production round. It waits for every GPU
        run to become terminal, permits one explicitly classified and dual-budgeted
        OOM or MSA retry, and then invokes the existing fail-closed CPU assembler.
        Repeated ticks are idempotent because the exact Preview round identity is
        checked before any retry or assembly.
        """

        from foldarium_pipeline.supabase import SupabaseCoordinator

        identity = _nextweekly_window(release_date)
        if NEXTWEEKLY_ENVIRONMENT != "preview":
            raise RuntimeError(
                "automatic nextweekly publication is restricted to preview"
            )
        coordinator = SupabaseCoordinator.from_env()
        if coordinator.weekly_quiz_round_exists(identity["round_id"]):
            outcome = {
                **identity,
                "status": "preview-ready",
                "environment": NEXTWEEKLY_ENVIRONMENT,
            }
        elif not coordinator.weekly_campaign_exists(identity["campaign_id"]):
            outcome = {
                **identity,
                "status": "waiting-for-campaign",
                "environment": NEXTWEEKLY_ENVIRONMENT,
            }
        else:
            rows = coordinator.campaign_prediction_run_statuses(
                identity["campaign_id"]
            )
            report = _nextweekly_run_report(rows)
            if not rows:
                outcome = {
                    **identity,
                    **report,
                    "status": "waiting-for-runs",
                    "environment": NEXTWEEKLY_ENVIRONMENT,
                }
            elif report["active_run_ids"]:
                outcome = {
                    **identity,
                    **report,
                    "status": "waiting-for-predictions",
                    "environment": NEXTWEEKLY_ENVIRONMENT,
                }
            elif report["authorized_retry_pending_run_ids"]:
                outcome = {
                    **identity,
                    **report,
                    "status": "waiting-for-authorized-retries",
                    "environment": NEXTWEEKLY_ENVIRONMENT,
                }
            else:
                retry_budget = (
                    _nextweekly_retry_budget(rows, report["retry_candidates"])
                    if report["retry_candidates"]
                    else None
                )
                retry_ids = (
                    retry_budget["authorized_candidate_run_ids"]
                    if retry_budget is not None
                    and retry_budget["authorization_ready"]
                    else []
                )
                if retry_ids:
                    retry_id_set = set(retry_ids)
                    retry_requests = [
                        candidate
                        for candidate in report["retry_candidates"]
                        if candidate["run_id"] in retry_id_set
                    ]
                    retry = retry_prediction_runs.remote(
                        retry_requests, False
                    )
                    outcome = {
                        **identity,
                        **report,
                        "status": "prediction-retries-submitted",
                        "environment": NEXTWEEKLY_ENVIRONMENT,
                        "automatic_retry_budget": retry_budget,
                        "retry_run_ids": retry_ids,
                        "retry_requests": retry_requests,
                        "retry_submission_status": retry.get("submission_status"),
                        "retry_modal_call_ids": [
                            row.get("modal_call_id")
                            for row in retry.get("submissions", [])
                            if isinstance(row, Mapping)
                        ],
                    }
                elif report["retry_candidates"]:
                    outcome = {
                        **identity,
                        **report,
                        "status": "waiting-for-retry-authorization",
                        "environment": NEXTWEEKLY_ENVIRONMENT,
                        "automatic_retry_budget": retry_budget,
                        "automatic_retry_status": "blocked",
                    }
                else:
                    assembled = assemble_weekly_quiz_round.remote(
                        identity["campaign_id"],
                        identity["round_id"],
                        identity["opens_at"],
                        identity["closes_at"],
                        True,
                        NEXTWEEKLY_INCLUDE_POSE_METRICS,
                        True,
                        NEXTWEEKLY_ENVIRONMENT,
                        _weekly_public_bucket(),
                        "",
                        None,
                    )
                    outcome = {
                        **identity,
                        **report,
                        "status": "preview-opened",
                        "environment": NEXTWEEKLY_ENVIRONMENT,
                        "assembly": assembled,
                    }
        log_outcome = {
            key: value
            for key, value in outcome.items()
            if key not in {"active_run_ids", "retryable_run_ids", "assembly"}
        }
        print(
            "foldarium.nextweekly "
            + json.dumps(log_outcome, sort_keys=True),
            flush=True,
        )
        return outcome

    @app.local_entrypoint()
    def submit(task_json: str) -> None:
        """Submit one serialized task with ``modal run ... --task-json ...``."""

        canonical_json = _normalise_task_json(task_json)
        print(_spawn_task(canonical_json))

    @app.local_entrypoint()
    def run_task(task_path: str) -> None:
        """Run exactly one planned task synchronously and print its result.

        Unlike ``submit``, this blocks until the run reaches a terminal state, so
        an operator spending metered credits sees the outcome instead of a call
        ID. It reads the task from a file to keep large payloads out of shell
        history, and runs a single task by design: batching belongs to
        ``submit_tasks`` once cost behavior is known.
        """

        payload = json.loads(Path(task_path).read_text(encoding="utf-8"))
        canonical_json = _normalise_task_json(payload)
        result = _sized_function(canonical_json).remote(canonical_json)
        print(json.dumps(result, indent=2, sort_keys=True))

else:
    # Gives tooling a predictable symbol while keeping Modal out of core/test
    # dependencies. Deployment commands will naturally require `pip install modal`.
    app = None
