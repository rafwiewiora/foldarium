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
# so this ceiling is deliberately tight. Raising it should be an explicit,
# reviewed change alongside a revised cost estimate.
GPU_FUNCTION_TIMEOUT_SECONDS = 20 * 60
LEASE_GRACE_SECONDS = 15 * 60
OPENFOLD_CACHE_ROOT = "/cache/openfold"
BOLTZ_CACHE_ROOT = "/cache/boltz"

# Saturday intake follows the lifecycle documented at the repository root. The
# cron belongs to this adapter, not to the provider-neutral pipeline core. CAMEO
# publication can lag the nominal 03:00 UTC boundary, so the deployed poller
# checks every 15 minutes through 06:45. Once a campaign exists in Supabase the
# hook exits before touching the public feeds or spawning any work.
WEEKLY_CRON_UTC = os.environ.get("FOLDARIUM_WEEKLY_CRON", "*/15 3-6 * * 6")
WEEKLY_HOOK_ENV = "FOLDARIUM_WEEKLY_HOOK"
WEEKLY_CRON_ENABLED = os.environ.get("FOLDARIUM_ENABLE_WEEKLY_CRON") == "1"
WEEKLY_RUNTIME_ENV = {
    key: os.environ[key]
    for key in (
        WEEKLY_HOOK_ENV,
        "FOLDARIUM_WEEKLY_REGISTER",
        "FOLDARIUM_WEEKLY_SUBMIT",
        "FOLDARIUM_WEEKLY_MAX_TARGETS",
        "FOLDARIUM_WEEKLY_GPU_CLASS",
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
    lease_seconds = (
        min(requested_timeout, GPU_FUNCTION_TIMEOUT_SECONDS) + LEASE_GRACE_SECONDS
    )
    if not publisher.claim_run(task["task_id"], worker_id, lease_seconds):
        raise RuntimeError(
            f"prediction run {task['task_id']} is already claimed by another worker"
        )

    result = execute_task_json(task, work_root=WORK_ROOT, dry_run=False)
    if not isinstance(result, dict):
        raise TypeError("execute_task_json must return a dict")
    if result.get("status") == "failed":
        # The core result remains deliberately terse. Preserve a bounded tail in
        # private Modal logs so an operator can distinguish a CLI/configuration
        # error from a model/runtime failure without launching a diagnostic GPU
        # retry. Method stderr must never be copied into a public quiz payload.
        stderr_path = Path(WORK_ROOT) / task["task_id"] / "logs" / "stderr.log"
        if stderr_path.is_file():
            stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-8_000:]
            print(
                "foldarium.worker.stderr "
                + json.dumps(
                    {
                        "task_id": task["task_id"],
                        "method": task["method"],
                        "stderr_tail": stderr_tail,
                    },
                    sort_keys=True,
                )
            )
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
        .uv_pip_install(BOLTZ2_PACKAGE)
        .env({"BOLTZ_CACHE": BOLTZ_CACHE_ROOT})
    )

    control_image = _add_core(modal.Image.debian_slim(python_version="3.12")).env(
        WEEKLY_RUNTIME_ENV
    )

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
        timeout=GPU_FUNCTION_TIMEOUT_SECONDS,
        max_containers=1,
        volumes={BOLTZ_CACHE_ROOT: boltz_cache},
        secrets=[control_plane_secret],
    )
    def run_boltz2(task_json: str | dict[str, Any]) -> dict[str, Any]:
        boltz_cache.reload()
        result = _execute(task_json)
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
        schedule=modal.Cron(WEEKLY_CRON_UTC) if WEEKLY_CRON_ENABLED else None,
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
            outcome = {
                "status": "disabled",
                "reason": f"{WEEKLY_HOOK_ENV} is not configured",
            }
            print("foldarium.weekly " + json.dumps(outcome, sort_keys=True), flush=True)
            return outcome
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
            outcome = {"status": report.pop("status", "no-work"), "count": 0, **report}
            print("foldarium.weekly " + json.dumps(outcome, sort_keys=True), flush=True)
            return outcome
        # Scheduling, registration, and GPU submission are deliberately three
        # independent switches. A newly deployed cron can prove tomorrow's
        # intake and cost plan without spending a single GPU second.
        if os.environ.get("FOLDARIUM_WEEKLY_SUBMIT") != "1":
            outcome = {
                "status": "planned-not-submitted",
                "count": len(tasks),
                **report,
            }
            print("foldarium.weekly " + json.dumps(outcome, sort_keys=True), flush=True)
            return outcome
        registration = report.get("registration")
        if not isinstance(registration, Mapping) or registration.get("status") != "registered":
            raise RuntimeError(
                "weekly GPU submission requires an atomically registered Supabase plan"
            )
        call_ids = [_spawn_task(task) for task in tasks]
        outcome = {
            "status": "submitted",
            "count": len(call_ids),
            "call_ids": call_ids,
            **report,
        }
        print("foldarium.weekly " + json.dumps(outcome, sort_keys=True), flush=True)
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
