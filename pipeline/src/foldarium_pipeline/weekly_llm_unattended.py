"""Isolated blind inference entrypoint and credential-separated publication adapter.

This module has no schedule and enables no spend by default. The outer caller
must hold the execution lease through inference and use execution-only storage.
"""
from __future__ import annotations

from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import uuid
from typing import Any, Callable, Mapping

from .weekly_llm_contract import sha256_hex, validate_post_close_benchmark
from .weekly_llm_providers.anthropic_api import (
    ALLOWLIST, ApiConfig, ApiBudgetError, AnthropicApiProvider, MessagesTransport,
    atomic_json, config_sha256, initialize_budget_ledger, validate_budget_ledger,
)
from .weekly_selector import verify_selector_kit_zip


def validate_job(job: Mapping[str, Any], config: ApiConfig) -> None:
    required = {"round_id", "environment", "blind_manifest_sha256", "execution_id", "driver", "model_id", "config_sha256", "max_cost_usd"}
    if required != set(job) or job["driver"] != "anthropic-api" or job["model_id"] != config.model_id:
        raise ApiBudgetError("unsupported or incomplete frozen benchmark job")
    if job["config_sha256"] != config_sha256(config) or Decimal(str(job["max_cost_usd"])) != Decimal(config.max_cost_usd):
        raise ApiBudgetError("API configuration differs from frozen benchmark budget/provenance")
    if str(uuid.UUID(job["execution_id"])) != job["execution_id"]:
        raise ApiBudgetError("execution_id must be a canonical UUID")
    if job["environment"] not in {"production", "preview"}:
        raise ApiBudgetError("invalid benchmark environment")


def _validate_kit(content: bytes, job: Mapping[str, Any]) -> dict[str, Any]:
    kit = verify_selector_kit_zip(content)
    for key in ("round_id", "environment", "blind_manifest_sha256"):
        if kit.get(key) != job[key]:
            raise ApiBudgetError("kit does not match exact frozen round")
    return kit


def _validate_execution(content: bytes, kit: dict[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    execution = validate_post_close_benchmark(json.loads(content), kit=kit,
        context_environment=job["environment"], context_round_id=job["round_id"])
    if (execution["execution_id"] != job["execution_id"] or execution["provider"] != job["driver"]
        or execution["model"]["requested_id"] != job["model_id"]
        or execution["model"]["observed_ids"] != [job["model_id"]]
        or execution["provenance"]["config_sha256"] != job["config_sha256"]):
        raise ApiBudgetError("inference artifact differs from frozen benchmark expectation")
    cost = execution.get("usage", {}).get("cost_usd")
    if cost is None or not Decimal(str(cost)).is_finite() or not 0 <= Decimal(str(cost)) <= Decimal(str(job["max_cost_usd"])):
        raise ApiBudgetError("inference artifact cost is absent or exceeds frozen budget")
    return execution


def execute_benchmark_job(private_coordinator: Any, public_coordinator: Any,
        parameters: Mapping[str, Any], artifact_root: Path, *, config: Mapping[str, Any],
        launch_inference: Callable[..., Path]) -> dict[str, Any]:
    """Outer service-only adapter. Only verified kit/job/config enter inference.

    The launcher must enforce provider-only egress, serialize this execution and
    return a local artifact path. No answer data or Supabase credentials are
    passed to it. This function does not publish benchmark receipts or reveal.
    """
    api_config = ApiConfig.from_mapping(config)
    validate_job(parameters, api_config)
    snapshot = private_coordinator._rpc("weekly_automation_snapshot_v1", {})
    rows = [row for row in snapshot["rounds"] if row["round_id"] == parameters["round_id"]]
    if len(rows) != 1:
        raise ApiBudgetError("exact automation round is absent")
    row = rows[0]
    for key in ("environment", "blind_manifest_sha256"):
        if row.get(key) != parameters[key]:
            raise ApiBudgetError("automation round binding changed")
    jobs = [job for job in row["benchmark_jobs"] if job["execution_id"] == parameters["execution_id"]]
    if len(jobs) != 1:
        raise ApiBudgetError("frozen benchmark expectation is absent")
    job = jobs[0]
    for key in ("driver", "model_id", "config_sha256"):
        if job.get(key) != parameters[key]:
            raise ApiBudgetError("frozen benchmark expectation changed")
    if Decimal(str(job["max_cost_usd"])) != Decimal(api_config.max_cost_usd):
        raise ApiBudgetError("frozen benchmark budget changed")
    kit_row = row.get("kit")
    if not kit_row:
        raise ApiBudgetError("registered blind kit is absent")
    content = public_coordinator.download_content_object("supabase://" + kit_row["storage_path"])
    kit = _validate_kit(content, parameters)
    if kit["kit_sha256"] != kit_row["kit_sha256"]:
        raise ApiBudgetError("registered kit digest changed")
    if job.get("artifact_uri"):
        artifact = private_coordinator.download_content_object(job["artifact_uri"], expected_sha256=job["artifact_sha256"])
        _validate_execution(artifact, kit, parameters)
        return {"status": "already_registered", "execution_id": parameters["execution_id"], "artifact_sha256": job["artifact_sha256"]}
    # The one-time initialization grant is durable outside the inference
    # Volume. Missing/corrupt/all-files-lost state on retry must never reset the
    # budget. A lost RPC acknowledgement consumes the grant and fails closed.
    grant = private_coordinator._rpc("claim_weekly_automation_inference_start_v1", {
        "p_execution_id": parameters["execution_id"],
        "p_config_sha256": parameters["config_sha256"], "p_kit_sha256": kit["kit_sha256"],
    })
    if not isinstance(grant, dict) or type(grant.get("first_claim")) is not bool:
        raise ApiBudgetError("inference initialization grant is invalid")
    for key in ("execution_id", "round_id", "environment", "blind_manifest_sha256", "driver", "model_id", "config_sha256"):
        if grant.get(key) != parameters[key]:
            raise ApiBudgetError("inference initialization grant differs from frozen job")
    if grant.get("kit_sha256") != kit["kit_sha256"] or Decimal(str(grant.get("max_cost_usd"))) != Decimal(api_config.max_cost_usd):
        raise ApiBudgetError("inference initialization grant differs from kit or budget")
    state_dir = artifact_root / parameters["execution_id"]
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ledger_path = state_dir / "budget.json"
    if grant["first_claim"]:
        initialize_budget_ledger(ledger_path, api_config, parameters["execution_id"])
    validate_budget_ledger(ledger_path, api_config, parameters["execution_id"])
    kit_path = state_dir / "kit.zip"
    kit_path.write_bytes(content)
    os.chmod(kit_path, 0o600)
    path = launch_inference(kit_path=kit_path, job=dict(parameters), config=dict(config), state_dir=state_dir)
    artifact = Path(path).read_bytes()
    _validate_execution(artifact, kit, parameters)
    stored = private_coordinator.store_bytes(artifact, "application/json")
    if stored["sha256"] != sha256_hex(artifact):
        raise ApiBudgetError("stored artifact digest mismatch")
    private_coordinator._rpc("register_weekly_automation_artifact_v1", {
        "p_execution_id": parameters["execution_id"], "p_artifact_uri": stored["object_uri"], "p_artifact_sha256": stored["sha256"],
    })
    return {"status": "registered", "execution_id": parameters["execution_id"], "artifact_sha256": stored["sha256"]}


def run_isolated_inference(*, kit_path: Path, job: Mapping[str, Any], config: Mapping[str, Any],
        state_dir: Path, egress_enforced: bool, durable_commit: Callable[[], None],
        transport: Callable[..., dict[str, Any]] | None = None) -> Path:
    """Run only in the isolated sandbox. Replays successful exact-input items."""
    if not egress_enforced:
        raise ApiBudgetError("provider-only network isolation is required")
    api_config = ApiConfig.from_mapping(config)
    validate_job(job, api_config)
    kit = _validate_kit(kit_path.read_bytes(), job)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Local lock plus mandatory launcher execution exclusivity; distributed
    # Volume mounts do not turn this into a distributed lease.
    with (state_dir / "execution.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ApiBudgetError("execution is already running") from None
        complete = state_dir / "benchmark.execution.json"
        if complete.exists():
            _validate_execution(complete.read_bytes(), kit, job)
            return complete
        transport = transport or MessagesTransport(os.environ.get("ANTHROPIC_API_KEY", ""), api_config.timeout_seconds)
        provider = AnthropicApiProvider(config=api_config, execution_id=job["execution_id"],
            ledger_path=state_dir / "budget.json", transport=transport, durable_commit=durable_commit)
        allowlist_path = state_dir / "network-allowlist.json"
        atomic_json(allowlist_path, ALLOWLIST)
        from .weekly_llm_runner import RunnerOptions, run_weekly_llm_score
        result = run_weekly_llm_score(RunnerOptions(kit_path=kit_path,
            output_dir=state_dir / "attempts" / str(uuid.uuid4()), provider=provider,
            display_name=api_config.model_id, provider_name="anthropic-api",
            network_allowlist_path=allowlist_path, egress_enforcement_asserted=True,
            execution_id=job["execution_id"]))
        _validate_execution(result.benchmark_path.read_bytes(), kit, job)
        atomic_json(complete, result.execution)
        durable_commit()
        return complete
