"""Backend-independent worker entry point."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .contracts import SCHEMA_VERSION, validate_prediction_task
from .methods import ADAPTERS

GPU_SAMPLE_INTERVAL_SECONDS = 2.0


def _prediction_failure_code(stderr: str) -> str:
    """Classify only actionable accelerator exhaustion without exposing logs."""

    lowered = stderr.casefold()
    markers = (
        "cuda out of memory",
        "cuda_error_out_of_memory",
        "outofmemoryerror",
        "failed to allocate memory on device",
    )
    return "gpu_out_of_memory" if any(marker in lowered for marker in markers) else "prediction_failed"


class _GpuMemorySampler:
    """Record peak device memory while a prediction subprocess runs.

    Accelerator sizing is only as good as the measurements behind it, and the
    method CLIs do not report peak memory. Sampling ``nvidia-smi`` is method- and
    backend-neutral, and silently does nothing where it is unavailable, so a CPU
    or non-NVIDIA runtime is unaffected.

    The figure is whole-device rather than per-process. That is accurate here
    because a prediction container owns its GPU, but it would overcount if
    anything else shared the device.
    """

    def __init__(self) -> None:
        self.peak_mib = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample_once(self) -> bool:
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
        values = [
            int(line.strip())
            for line in completed.stdout.splitlines()
            if line.strip().isdigit()
        ]
        if values:
            self.peak_mib = max(self.peak_mib, max(values))
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._sample_once():
                return
            self._stop.wait(GPU_SAMPLE_INTERVAL_SECONDS)

    def __enter__(self) -> "_GpuMemorySampler":
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)


def execute_task_json(
    task_json: str | Mapping[str, Any],
    work_root: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Plan or run exactly one task using no backend-specific services.

    Artifact upload and database state transitions intentionally sit outside this
    function so an execution wrapper can use Supabase Storage, GCS, S3, or signed
    URLs without changing method code.
    """

    raw = json.loads(task_json) if isinstance(task_json, str) else task_json
    task = validate_prediction_task(raw)
    root = Path(work_root).resolve()
    work_dir = root / task["task_id"]
    work_dir.mkdir(parents=True, exist_ok=True)
    adapter = ADAPTERS[task["method"]]
    base_result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["task_id"],
        "campaign_id": task["campaign_id"],
        "target_id": task["target"]["target_id"],
        "method": task["method"],
        "method_version": task["method_version"],
        "container_image": task["container_image"],
        "output_uri_prefix": task["output_uri_prefix"],
    }
    try:
        plan = adapter.plan(task, work_dir)
    except (OSError, ValueError) as exc:
        if dry_run:
            raise
        return {
            **base_result,
            "status": "failed",
            "error_code": "planning_failed",
            "error": f"prediction planning failed: {type(exc).__name__}",
        }
    if dry_run:
        return {**base_result, "status": "planned", "plan": plan.public_dict(work_dir)}

    started = time.time()
    environment = os.environ.copy()
    environment.update(plan.environment)
    timeout = task["resources"].get("timeout_seconds")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1):
        raise ValueError("resources.timeout_seconds must be a positive integer")
    logs = work_dir / "logs"
    logs.mkdir(exist_ok=True)
    sampler = _GpuMemorySampler()
    try:
        with sampler:
            completed = subprocess.run(
                list(plan.argv),
                cwd=work_dir,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        (logs / "stdout.log").write_text(stdout, encoding="utf-8")
        (logs / "stderr.log").write_text(stderr, encoding="utf-8")
        return {
            **base_result,
            "status": "failed",
            "duration_seconds": round(time.time() - started, 3),
            "error_code": "timeout",
            "error": "prediction command exceeded its task timeout",
        }
    except OSError:
        return {
            **base_result,
            "status": "failed",
            "duration_seconds": round(time.time() - started, 3),
            "error_code": "launch_failed",
            "error": "prediction command could not be started in this runtime",
        }
    (logs / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    (logs / "stderr.log").write_text(completed.stderr, encoding="utf-8")
    duration = round(time.time() - started, 3)
    if completed.returncode != 0:
        return {
            **base_result,
            "status": "failed",
            "duration_seconds": duration,
            "exit_code": completed.returncode,
            "error_code": _prediction_failure_code(completed.stderr),
            "error": "prediction command failed; inspect worker logs",
        }
    try:
        samples = adapter.collect(task, plan.output_dir)
    except (OSError, ValueError):
        return {
            **base_result,
            "status": "failed",
            "duration_seconds": duration,
            "error_code": "output_validation_failed",
            "error": "prediction outputs did not satisfy the method adapter contract",
        }
    result: dict[str, Any] = {
        **base_result,
        "status": "succeeded",
        "duration_seconds": duration,
        "samples": samples,
        "provenance": {
            "config": task["config"],
            "command": plan.public_dict(work_dir)["argv"],
            "resources": dict(task["resources"]),
        },
    }
    # Absent rather than zero where no accelerator was observed, so calibration
    # never mistakes "not measured" for "used no memory".
    if sampler.peak_mib > 0:
        result["peak_gpu_memory_mib"] = sampler.peak_mib
    return result
