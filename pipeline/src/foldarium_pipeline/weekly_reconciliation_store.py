"""Service-role adapter and durable execution loop for Weekly reconciliation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Mapping

from .weekly_reconciliation import Gates, plan_reconciliation


class ReconciliationStore:
    def __init__(self, coordinator: Any):
        self.coordinator = coordinator

    def snapshot(self) -> dict[str, Any]:
        value = self.coordinator._rpc("weekly_automation_snapshot_v1", {})
        if not isinstance(value, dict):
            raise ValueError("automation snapshot must be an object")
        return value

    def enqueue(self, action: Mapping[str, Any]) -> Any:
        return self.coordinator._rpc("enqueue_weekly_automation_action_v1", {"p_action": dict(action)})

    def claim(self, key: str) -> dict[str, Any] | None:
        return self.coordinator._rpc("claim_weekly_automation_action_v1", {"p_action_key": key})

    def record_dispatch(self, claim: Mapping[str, Any], receipt: Mapping[str, Any]) -> Any:
        return self.coordinator._rpc("record_weekly_automation_dispatch_v1", {
            "p_action_key": claim["action_key"], "p_lease_token": claim["lease_token"],
            "p_receipt": dict(receipt),
        })

    def finish(self, claim: Mapping[str, Any], outcome: str, error: str | None = None) -> Any:
        return self.coordinator._rpc("finish_weekly_automation_action_v1", {
            "p_action_key": claim["action_key"], "p_lease_token": claim["lease_token"],
            "p_outcome": outcome, "p_error": error,
        })


def reconcile_weekly(
    store: Any, execute: Callable[[Mapping[str, Any]], Any], *, gates: Gates,
    preview_version: str, production_suffix: str, apply: bool = False,
    max_actions: int = 4, available_drivers: tuple[str, ...] = (),
    benchmark_policy: Mapping[str, Any] | None = None,
    lifecycle_scope: Mapping[str, Any] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """One bounded pass; dry-run never enqueues, claims, or calls an executor.

    Execute at least once. Exact existing RPCs must remain idempotent: no lease
    can provide exactly-once semantics across a database/compute boundary.
    Successful completion requires fresh desired-state observation, never just
    a successful transport response. Normal dependency waits do not use the
    five-failure retry budget; exceptions and worker loss do.
    """
    if not isinstance(apply, bool) or isinstance(max_actions, bool) or not 1 <= max_actions <= 80:
        raise ValueError("apply must be boolean and max_actions must be 1..80")

    def plan() -> tuple[dict[str, Any], Mapping[str, Any]]:
        snapshot = store.snapshot()
        return plan_reconciliation(snapshot, now=clock(), gates=gates,
            preview_version=preview_version, production_suffix=production_suffix,
            available_drivers=available_drivers, benchmark_policy=benchmark_policy, lifecycle_scope=lifecycle_scope), snapshot

    initial, snapshot = plan()
    result = {**initial, "apply": apply, "executed": [], "failed_actions": snapshot.get("failed_actions", [])}
    if not apply:
        return result
    history = snapshot.get("action_history", {})
    ordered = sorted(initial["actions"], key=lambda action: history.get(action["action_key"], {}).get("updated_at", ""))
    deadline = time.monotonic() + 25 * 60
    for action in ordered:
        if len(result["executed"]) >= max_actions or time.monotonic() >= deadline:
            break
        fresh, _ = plan()
        if action["action_key"] not in {a["action_key"] for a in fresh["actions"]}:
            continue
        store.enqueue(action)
        claim = store.claim(action["action_key"])
        if claim is None:
            continue
        try:
            response = execute(action)
            if isinstance(response, Mapping) and response.get("dispatch_receipt"):
                store.record_dispatch(claim, response["dispatch_receipt"])
            after, _ = plan()
            outcome = "waiting" if action["action_key"] in {a["action_key"] for a in after["actions"]} else "succeeded"
        except Exception as error:
            # Exception text may contain signed URLs, HTTP payloads, credentials
            # or answer data. Retain only its type in this durable control log.
            status = store.finish(claim, "error", type(error).__name__)
            result["executed"].append({"action_key": action["action_key"], "kind": action["kind"], "outcome": "error", "error_type": type(error).__name__, "state": status})
            continue
        status = store.finish(claim, outcome)
        result["executed"].append({"action_key": action["action_key"], "kind": action["kind"], "outcome": outcome, "state": status})
    return result


def verified_round(coordinator: Any, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    row = coordinator.weekly_quiz_round(parameters["round_id"])
    for key in ("round_id", "environment", "blind_manifest_sha256"):
        if row.get(key) != parameters.get(key):
            raise ValueError(f"automation round {key} binding changed")
    return row


def submit_expected_benchmark(coordinator: Any, public_coordinator: Any, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Ingest an existing, fully verified artifact before reveal, without inference."""
    from .weekly_llm_contract import digest_post_close_benchmark, sha256_hex, validate_post_close_benchmark
    from .weekly_selector import verify_selector_kit_zip

    verified_round(coordinator, parameters)
    snapshot = ReconciliationStore(coordinator).snapshot()
    row = next(r for r in snapshot["rounds"] if r["round_id"] == parameters["round_id"])
    job = next(j for j in row["benchmark_jobs"] if j["execution_id"] == parameters["execution_id"])
    if job["artifact_sha256"] != parameters["artifact_sha256"]:
        raise ValueError("benchmark artifact binding changed")
    artifact = coordinator.download_content_object(job["artifact_uri"], expected_sha256=job["artifact_sha256"])
    kit_row = row["kit"]
    kit = verify_selector_kit_zip(public_coordinator.download_content_object("supabase://" + kit_row["storage_path"]))
    for key in ("round_id", "environment", "blind_manifest_sha256"):
        if kit.get(key) != parameters[key]:
            raise ValueError(f"benchmark kit {key} binding changed")
    if kit["kit_sha256"] != kit_row["kit_sha256"]:
        raise ValueError("benchmark catalog kit digest mismatch")
    execution = validate_post_close_benchmark(json.loads(artifact), kit=kit,
        context_environment=parameters["environment"], context_round_id=parameters["round_id"])
    if execution["execution_id"] != job["execution_id"] or execution["model"]["requested_id"] != job["model_id"] or execution["provenance"]["config_sha256"] != job["config_sha256"] or execution["provider"] != job["driver"]:
        raise ValueError("benchmark execution differs from frozen expectation")
    digest = digest_post_close_benchmark(execution, kit=kit)
    payload_digest = sha256_hex(execution["payload"])
    receipt = coordinator._rpc("register_weekly_selector_benchmark_v1", {
        "p_execution": execution, "p_execution_sha256": digest, "p_payload_digest": payload_digest,
    })
    if isinstance(receipt, list) and len(receipt) == 1:
        receipt = receipt[0]
    if not isinstance(receipt, Mapping) or any(receipt.get(k) != v for k, v in {
        "execution_id": job["execution_id"], "round_id": parameters["round_id"],
        "environment": parameters["environment"], "execution_sha256": digest,
        "payload_digest": payload_digest,
    }.items()):
        raise ValueError("benchmark immutable receipt mismatch")
    coordinator._rpc("verify_weekly_automation_benchmark_v1", {
        "p_execution_id": job["execution_id"], "p_artifact_sha256": job["artifact_sha256"],
        "p_execution_sha256": digest, "p_payload_digest": payload_digest,
    })
    return receipt


def reveal_evaluated_round(coordinator: Any, parameters: Mapping[str, Any]) -> Any:
    """Reveal only an exact, post-close evaluation after all frozen receipts exist."""
    from .private_evaluation import describe_private_evaluation_artifact
    from .quiz import manifest_sha256

    row = verified_round(coordinator, parameters)
    snapshot = ReconciliationStore(coordinator).snapshot()
    state = next(r for r in snapshot["rounds"] if r["round_id"] == row["round_id"])
    policy = state.get("automation_policy")
    jobs = state.get("benchmark_jobs", [])
    if policy is None or policy["blind_manifest_sha256"] != row["blind_manifest_sha256"] or set(policy["expected_execution_ids"]) != {j["execution_id"] for j in jobs} or any(not j.get("receipt") for j in jobs):
        raise ValueError("required benchmark receipts are incomplete")
    catalog = coordinator.private_weekly_evaluation(row["round_id"])
    if not catalog:
        raise ValueError("private evaluation is missing")
    content = coordinator.download_content_object(catalog["artifact_object_uri"], expected_sha256=catalog["artifact_sha256"])
    described = describe_private_evaluation_artifact(content, expected_artifact_sha256=catalog["artifact_sha256"])
    from .weekly_reconciliation import timestamp
    for field in ("evaluation_id", "round_id", "campaign_id", "environment", "blind_manifest_sha256", "private_index_sha256", "reveal_manifest_sha256", "artifact_sha256"):
        if described.get(field) != catalog.get(field):
            raise ValueError("evaluation catalog binding mismatch: " + field)
    for field in ("round_id", "campaign_id", "environment", "blind_manifest_sha256"):
        if described.get(field) != row.get(field):
            raise ValueError("evaluation round binding mismatch: " + field)
    if described["private_index_sha256"] != row.get("metadata", {}).get("private_index", {}).get("sha256"):
        raise ValueError("evaluation source private index mismatch")
    if timestamp(described["round_closes_at"]) != timestamp(row["closes_at"]) or timestamp(described["round_opens_at"]) != timestamp(row["opens_at"]):
        raise ValueError("evaluation no longer matches exact round window")
    artifact = json.loads(content)
    reveal = artifact["reveal_manifest"]
    if manifest_sha256(reveal) != described["reveal_manifest_sha256"]:
        raise ValueError("evaluation reveal digest mismatch")
    return coordinator._rpc("reveal_weekly_automation_round_v1", {
        "p_round_id": row["round_id"], "p_evaluation_id": described["evaluation_id"],
        "p_reveal_manifest": reveal, "p_reveal_manifest_sha256": described["reveal_manifest_sha256"],
    })
