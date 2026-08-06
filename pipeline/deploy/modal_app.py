"""Thin Modal deployment adapter for Foldarium prediction tasks.

This module intentionally contains no campaign logic, database schema, or model
input translation.  Those live in ``foldarium_pipeline`` so the same task can be
executed locally, on Modal, or in a GCP job.

Install Modal only in the deployment environment, then run::

    modal deploy pipeline/deploy/modal_app.py

The module is importable without Modal installed so local tests do not need the
deployment SDK.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:  # Modal is an optional deployment dependency, not a core dependency.
    import modal
except ModuleNotFoundError:  # pragma: no cover - exercised without deployment extras
    modal = None  # type: ignore[assignment]


APP_NAME = "foldarium-predictions"

# Official OpenFold3 0.4-pixi (OpenFold3 0.4.4) OCI index. Keep this immutable;
# upgrading the model runtime should be an explicit, reviewed change with a new
# digest and matching task provenance.
OPENFOLD3_IMAGE_REF = (
    "docker.io/openfoldconsortium/openfold3:0.4-pixi@"
    "sha256:9bc891b799285f0edae94f9f3f05ffcb88f29dc8e758248ce384c64f80e16eec"
)

BOLTZ2_VERSION = "2.2.1"
BOLTZ2_PACKAGE = f"boltz[cuda]=={BOLTZ2_VERSION}"

WORK_ROOT = "/tmp/foldarium"
FUNCTION_TIMEOUT_SECONDS = 6 * 60 * 60
LEASE_GRACE_SECONDS = 15 * 60
OPENFOLD_CACHE_ROOT = "/cache/openfold"
BOLTZ_CACHE_ROOT = "/cache/boltz"

# Saturday intake follows the lifecycle documented at the repository root. The
# cron belongs to this adapter, not to the provider-neutral pipeline core.
WEEKLY_CRON_UTC = os.environ.get("FOLDARIUM_WEEKLY_CRON", "0 6 * * 6")
WEEKLY_HOOK_ENV = "FOLDARIUM_WEEKLY_HOOK"

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
        "timeout_seconds", FUNCTION_TIMEOUT_SECONDS
    )
    if (
        isinstance(requested_timeout, bool)
        or not isinstance(requested_timeout, int)
        or requested_timeout < 1
    ):
        raise ValueError("resources.timeout_seconds must be a positive integer")
    lease_seconds = (
        min(requested_timeout, FUNCTION_TIMEOUT_SECONDS) + LEASE_GRACE_SECONDS
    )
    if not publisher.claim_run(task["task_id"], worker_id, lease_seconds):
        raise RuntimeError(
            f"prediction run {task['task_id']} is already claimed by another worker"
        )

    result = execute_task_json(task, work_root=WORK_ROOT, dry_run=False)
    if not isinstance(result, dict):
        raise TypeError("execute_task_json must return a dict")
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


if modal is not None:
    if not _CORE_SOURCE.is_dir():
        raise RuntimeError(f"Foldarium core source directory not found: {_CORE_SOURCE}")

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

    control_plane_secret = modal.Secret.from_name(
        "foldarium-control-plane",
        required_keys=[
            "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY",
            "FOLDARIUM_STORAGE_BUCKET",
        ],
    )

    def _add_core(image):
        return (
            image.entrypoint([])
            .add_local_dir(
                _CORE_SOURCE,
                remote_path=f"{_REMOTE_SOURCE_ROOT}/foldarium_pipeline",
                copy=True,
            )
            .env({"PYTHONPATH": _REMOTE_SOURCE_ROOT})
        )

    openfold3_image = _add_core(
        modal.Image.from_registry(OPENFOLD3_IMAGE_REF).env(
            {"OPENFOLD_CACHE": OPENFOLD_CACHE_ROOT}
        )
    )

    # This bootstrap recipe is version-pinned and deliberately contains no
    # weights or credentials. For GCP, publish its equivalent as a
    # Foldarium-owned OCI image and pin the resulting Artifact Registry digest.
    boltz2_image = _add_core(
        modal.Image.debian_slim(python_version="3.12")
        .uv_pip_install(BOLTZ2_PACKAGE)
        .env({"BOLTZ_CACHE": BOLTZ_CACHE_ROOT})
    )

    control_image = _add_core(modal.Image.debian_slim(python_version="3.12"))

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
            ["setup_openfold", "--config", str(config_path)],
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
        cpu=8.0,
        memory=32768,
        gpu="A100-40GB",
        timeout=FUNCTION_TIMEOUT_SECONDS,
        max_containers=1,
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
        timeout=FUNCTION_TIMEOUT_SECONDS,
        max_containers=1,
        volumes={BOLTZ_CACHE_ROOT: boltz_cache},
        secrets=[control_plane_secret],
    )
    def run_boltz2(task_json: str | dict[str, Any]) -> dict[str, Any]:
        boltz_cache.reload()
        result = _execute(task_json)
        boltz_cache.commit()
        return result

    def _spawn_task(task_json: str | Mapping[str, Any]) -> str:
        canonical_json = _normalise_task_json(task_json)
        method = _method_name(canonical_json)
        call = (
            run_openfold3.spawn(canonical_json)
            if method == "openfold3"
            else run_boltz2.spawn(canonical_json)
        )
        return call.object_id

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
        schedule=modal.Cron(WEEKLY_CRON_UTC),
        secrets=[control_plane_secret],
        timeout=30 * 60,
        max_containers=1,
    )
    def weekly_tick() -> dict[str, Any]:
        """Deployment-owned cron seam for a provider-neutral campaign producer.

        The configured hook receives no Modal objects. It must return an iterable
        of PredictionTask JSON strings/mappings and may use Supabase for
        idempotent planning. Leaving the hook unset makes the cron a safe no-op.
        """

        reference = os.environ.get(WEEKLY_HOOK_ENV)
        if not reference:
            return {
                "status": "disabled",
                "reason": f"{WEEKLY_HOOK_ENV} is not configured",
            }
        tasks = list(_load_weekly_hook(reference)())
        call_ids = [_spawn_task(task) for task in tasks]
        return {"status": "submitted", "count": len(call_ids), "call_ids": call_ids}

    @app.local_entrypoint()
    def submit(task_json: str) -> None:
        """Submit one serialized task with ``modal run ... --task-json ...``."""

        canonical_json = _normalise_task_json(task_json)
        print(_spawn_task(canonical_json))

else:
    # Gives tooling a predictable symbol while keeping Modal out of core/test
    # dependencies. Deployment commands will naturally require `pip install modal`.
    app = None
