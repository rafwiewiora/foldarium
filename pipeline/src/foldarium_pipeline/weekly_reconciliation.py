"""Exact-identity Weekly desired-state planning, independent of execution provider.

No wall-clock-derived round is substituted for a stored round. Plans contain only
identities/digests, never manifests, answers, task payloads, or credentials.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import uuid
from typing import Any, Mapping

from .weekly_lifecycle import delayed_retrospective_release


@dataclass(frozen=True)
class Gates:
    intake: bool = False
    predictions: bool = False
    preview: bool = False
    production: bool = False
    kits: bool = False
    featured: bool = False
    evaluation: bool = False
    reveal: bool = False
    retrospective: bool = False
    benchmarks: bool = False


@dataclass(frozen=True)
class Action:
    kind: str
    identity: str
    parameters: Mapping[str, Any]
    gate: str

    @property
    def key(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()

    def record(self) -> dict[str, Any]:
        return {"action_key": self.key, **asdict(self)}


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("reconciliation timestamps require a timezone")
    return parsed.astimezone(timezone.utc)


def validate_benchmark_policy(policy: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate an explicit deployment policy; absent is distinct from empty."""
    if policy is None:
        return None
    if not isinstance(policy, Mapping) or set(policy) != {"schema", "policy_id", "required_methods"}:
        raise ValueError("invalid benchmark policy fields")
    if policy["schema"] != "foldarium.weekly-benchmark-policy/v1" or not isinstance(policy["policy_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", policy["policy_id"]):
        raise ValueError("invalid benchmark policy identity")
    methods = policy["required_methods"]
    if not isinstance(methods, list) or len(methods) > 20:
        raise ValueError("benchmark policy requires at most 20 methods")
    seen = set()
    normalized = []
    for method in methods:
        if not isinstance(method, Mapping) or set(method) != {"driver", "model_id", "config_sha256", "max_cost_usd"}:
            raise ValueError("invalid benchmark method fields")
        if not isinstance(method["driver"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", method["driver"]):
            raise ValueError("invalid benchmark driver")
        if not isinstance(method["model_id"], str) or not 1 <= len(method["model_id"]) <= 200:
            raise ValueError("exact benchmark model_id required")
        if not isinstance(method["config_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", method["config_sha256"]):
            raise ValueError("exact benchmark config_sha256 required")
        cost = method["max_cost_usd"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0:
            raise ValueError("benchmark max_cost_usd must be finite and positive")
        identity = (method["driver"], method["model_id"], method["config_sha256"])
        if identity in seen:
            raise ValueError("duplicate benchmark method")
        seen.add(identity)
        normalized.append(dict(method))
    normalized.sort(key=lambda m: (m["driver"], m["model_id"], m["config_sha256"]))
    return {"schema": policy["schema"], "policy_id": policy["policy_id"], "required_methods": normalized}


def validate_lifecycle_scope(scope: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Bind legacy canonical identities explicitly; never infer latest by name."""
    if scope is None:
        return None
    if not isinstance(scope, Mapping) or set(scope) != {"schema", "canonical_rounds"} or scope["schema"] != "foldarium.weekly-lifecycle-scope/v1":
        raise ValueError("invalid lifecycle scope")
    rows = scope["canonical_rounds"]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("invalid canonical round scope")
    seen = set()
    normalized = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"campaign_id", "environment", "round_id", "blind_manifest_sha256"}:
            raise ValueError("invalid canonical round fields")
        if row["environment"] not in {"preview", "production"}:
            raise ValueError("invalid canonical environment")
        for key in ("campaign_id", "round_id"):
            if not isinstance(row[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", row[key]):
                raise ValueError("invalid canonical identity")
        if not isinstance(row["blind_manifest_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["blind_manifest_sha256"]):
            raise ValueError("invalid canonical manifest digest")
        key = (row["campaign_id"], row["environment"])
        if key in seen:
            raise ValueError("duplicate canonical campaign/environment")
        seen.add(key)
        normalized.append(dict(row))
    return {"schema": scope["schema"], "canonical_rounds": sorted(normalized, key=lambda r: (r["campaign_id"], r["environment"]))}


def benchmark_expectations(row: Mapping[str, Any], policy: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    normalized = validate_benchmark_policy(policy)
    policy_digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    jobs = []
    for method in normalized["required_methods"]:
        identity = {"environment": row["environment"], "round_id": row["round_id"],
            "blind_manifest_sha256": row["blind_manifest_sha256"], "policy_sha256": policy_digest, **method}
        execution_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(identity, sort_keys=True, separators=(",", ":"))))
        jobs.append({"execution_id": execution_id, **method})
    return policy_digest, jobs


def plan_reconciliation(
    snapshot: Mapping[str, Any], *, now: datetime, gates: Gates,
    preview_version: str, production_suffix: str,
    available_drivers: tuple[str, ...] = (),
    benchmark_policy: Mapping[str, Any] | None = None,
    lifecycle_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan one bounded pass, with dependencies reconsidered on the next snapshot.

    A frozen explicit benchmark expectation is required before revealing any
    round. Empty expectations are supported but must be explicitly registered.
    Benchmark ingestion is closed-but-UNREVEALED, as required by the existing RPC.
    """
    if now.tzinfo is None:
        raise ValueError("now requires a timezone")
    now = now.astimezone(timezone.utc)
    deployment_policy = validate_benchmark_policy(benchmark_policy)
    scope = validate_lifecycle_scope(lifecycle_scope)
    canonical = {(r["campaign_id"], r["environment"]): r for r in scope["canonical_rounds"]} if scope else {}
    rounds = list(snapshot["rounds"])
    campaigns = list(snapshot["campaigns"])
    if len(rounds) > 1000 or len(campaigns) > 1000:
        raise ValueError("reconciliation snapshot exceeds bound")
    by_id = {r["round_id"]: r for r in rounds}
    if len(by_id) != len(rounds):
        raise ValueError("duplicate round identity")
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []

    def block(identity: str, reason: str) -> None:
        blocked.append({"identity": identity, "reason": reason})

    def add(kind: str, identity: str, gate: str, **parameters: Any) -> None:
        action = Action(kind, identity, parameters, gate)
        if getattr(gates, gate):
            actions.append(action.record())
        else:
            block(identity, f"gate-disabled:{gate}:{kind}")

    eligible_ids = set()
    unavailable_scope_campaigns = set()
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rounds:
        if row["status"] in {"open", "revealed"} and row.get("opened_at"):
            groups.setdefault((row["campaign_id"], row["environment"]), []).append(row)
    for key, grouped in sorted(groups.items()):
        selected = canonical.get(key)
        if selected is not None:
            matching = [r for r in grouped if r["round_id"] == selected["round_id"] and r["blind_manifest_sha256"] == selected["blind_manifest_sha256"]]
            if len(matching) != 1:
                block(key[0], "canonical-round-binding-unavailable:" + key[1])
                unavailable_scope_campaigns.add(key[0])
                continue
            eligible_ids.add(matching[0]["round_id"])
            for row in grouped:
                if row["round_id"] != selected["round_id"]:
                    block(row["round_id"], "outside-canonical-lifecycle-scope")
        elif len(grouped) == 1:
            eligible_ids.add(grouped[0]["round_id"])
        elif key[1] == "production":
            # Explicitly frozen round expectations are already durable enrollment.
            # This preserves audited archival recovery without enrolling siblings.
            for row in grouped:
                if row.get("automation_policy") is not None:
                    eligible_ids.add(row["round_id"])
                else:
                    block(row["round_id"], "ambiguous-production-identity-needs-explicit-scope")
        elif not any(r["campaign_id"] == key[0] and r["environment"] == "production" and r["status"] not in {"failed", "withdrawn"} for r in rounds):
            # Once production exists these old Preview experiments are dormant;
            # no canonical mapping is needed merely to process production.
            block(key[0], "ambiguous-preview-identity")

    for key in canonical.keys() - groups.keys():
        block(key[0], "canonical-round-binding-unavailable:" + key[1])
        unavailable_scope_campaigns.add(key[0])
    eligible_ids.difference_update(r["round_id"] for r in rounds if r["campaign_id"] in unavailable_scope_campaigns)

    saturday = now.date() - timedelta(days=(now.weekday() - 5) % 7)
    campaign_ids = {c["campaign_id"] for c in campaigns}
    current_campaign = f"wwpdb-{saturday.isoformat()}"
    if current_campaign not in campaign_ids:
        add("intake", current_campaign, "intake", release_date=saturday.isoformat())
    for campaign in sorted(campaigns, key=lambda c: c["release_date"]):
        cid = campaign["campaign_id"]
        release = campaign["release_date"]
        if cid in unavailable_scope_campaigns:
            continue
        # Existing Preview IDs are authoritative (including repaired v5 rounds).
        previews = [r for r in rounds if r["campaign_id"] == cid and r["environment"] == "preview" and r["status"] == "open"]
        productions = [r for r in rounds if r["campaign_id"] == cid and r["environment"] == "production" and r["status"] not in {"failed", "withdrawn"}]
        if not previews and not productions:
            pending = [r for r in campaign.get("runs", []) if r["status"] in {"pending", "queued"} and r.get("attempt_count", 0) == 0]
            repairs = []
            for run in campaign.get("runs", []):
                dispatch = run.get("dispatch")
                if run["status"] == "failed" and run.get("attempt_count") == 1 and run.get("max_attempts") == 2:
                    if dispatch and dispatch["attempt_number"] == 2:
                        repairs.append((run, dispatch, "observe" if dispatch.get("call_id") else "dispatch"))
                    else:
                        block(run["run_id"], "legacy-authorized-retry-needs-exact-dispatch-recovery")
                elif run["status"] == "running" and run.get("lease_expires_at") and timestamp(run["lease_expires_at"]) <= now:
                    if dispatch and dispatch.get("call_id") and dispatch["attempt_number"] == run.get("attempt_count"):
                        repairs.append((run, dispatch, "worker_loss"))
                    else:
                        block(run["run_id"], "expired-worker-needs-exact-terminal-call-evidence")
            for run, dispatch, phase in repairs:
                add("reconcile_prediction_dispatch", dispatch["dispatch_id"], "predictions", campaign_id=cid,
                    run_id=run["run_id"], attempt_number=dispatch["attempt_number"], phase=phase)
            if pending:
                for run in pending:
                    dispatch = run.get("dispatch")
                    if dispatch and dispatch.get("call_id"):
                        add("reconcile_prediction_dispatch", dispatch["dispatch_id"], "predictions", campaign_id=cid,
                            run_id=run["run_id"], attempt_number=1, phase="observe")
                    else:
                        original = Action("dispatch_prediction", run["run_id"], {"campaign_id": cid, "run_id": run["run_id"]}, "predictions")
                        prior = snapshot.get("action_history", {}).get(original.key, {}).get("dispatch_receipt")
                        if run.get("dispatch_tracking_eligible") is False and not prior:
                            block(run["run_id"], "legacy-initial-dispatch-needs-exact-call-evidence")
                        else:
                            add("dispatch_prediction", run["run_id"], "predictions", campaign_id=cid, run_id=run["run_id"])
            else:
                # Keep this desired stage present while retry handoffs run.
                # Otherwise the outbox can mistake a temporary dependency for
                # completed assembly and never revisit this same campaign key.
                add("advance_preview", cid, "preview", release_date=release,
                    round_id=f"preview-weekly-{release}-nextweekly-{preview_version}")
        elif previews and not productions:
            previews = [r for r in previews if r["round_id"] in eligible_ids]
            if len(previews) != 1:
                block(cid, "ambiguous-preview-identity")
                continue
            source = previews[0]
            if source.get("historical_scope"):
                continue  # Explicit research scope never reopens a human voting window.
            if timestamp(source["closes_at"]) <= now:
                block(cid, "expired-preview-needs-explicit-historical-recovery")
                continue
            if not gates.kits:
                block(cid, "gate-disabled:kits:promotion-registers-kit")
                continue
            add("promote", cid, "production", source_round_id=source["round_id"],
                round_id=f"weekly-{release}-{production_suffix}",
                source_manifest_sha256=source["blind_manifest_sha256"],
                opens_at=source["opens_at"], closes_at=source["closes_at"])

    production = sorted([r for r in rounds if r["environment"] == "production" and r["round_id"] in eligible_ids], key=lambda r: (timestamp(r["opens_at"]), r["round_id"]))
    for row in sorted(rounds, key=lambda r: (r["opens_at"], r["round_id"])):
        rid = row["round_id"]
        historical = row["environment"] == "preview" and bool(row.get("historical_scope"))
        if not historical and (row["environment"] != "production" or row["round_id"] not in eligible_ids):
            continue
        if historical:
            scope = row["historical_scope"]
            exact = all(scope.get(k) == row.get(k) for k in ("round_id", "campaign_id", "environment", "blind_manifest_sha256", "private_index_sha256"))
            exact = exact and all(timestamp(scope[k]) == timestamp(row[k]) for k in ("opens_at", "closes_at"))
            if not exact or row["status"] != "open" or row.get("revealed_at") or timestamp(row["closes_at"]) > now:
                block(rid, "historical-source-binding-changed")
                continue
            if row.get("historical_published"):
                continue
        binding = {"round_id": rid, "environment": row["environment"], "blind_manifest_sha256": row["blind_manifest_sha256"]}
        if row["status"] == "revealed":
            if not row.get("retrospective_published"):
                add("publish_retrospective", rid, "retrospective", **binding)
            continue
        if not historical and not row.get("featured_registered"):
            add("freeze_featured", rid, "featured", **binding)
        if not row.get("kit"):
            add("register_kit", rid, "kits", **binding)
        policy = row.get("automation_policy")
        if policy is None:
            if deployment_policy is None:
                block(rid, "benchmark-expectations-not-frozen")
            else:
                policy_digest, expected_jobs = benchmark_expectations(row, deployment_policy)
                add("freeze_policy", rid, "benchmarks", **binding,
                    policy_sha256=policy_digest, expected_executions=expected_jobs)
        elif policy["blind_manifest_sha256"] != row["blind_manifest_sha256"]:
            raise ValueError(f"automation policy manifest mismatch for {rid}")
        jobs = row.get("benchmark_jobs", [])
        expected = set(policy["expected_execution_ids"]) if policy else set()
        if policy and expected != {j["execution_id"] for j in jobs}:
            raise ValueError(f"benchmark expectation membership mismatch for {rid}")
        missing_receipts = []
        for job in jobs:
            if job.get("receipt"):
                continue
            missing_receipts.append(job["execution_id"])
            if not row.get("kit"):
                block(rid, "benchmark-awaiting-kit")
            elif not job.get("artifact_uri"):
                if not job.get("max_cost_usd"):
                    block(rid, "llm-budget-not-frozen")
                elif job["driver"] not in available_drivers:
                    block(rid, f"llm-driver-unavailable:{job['driver']}")
                else:
                    add("score_benchmark", job["execution_id"], "benchmarks", **binding,
                        execution_id=job["execution_id"], driver=job["driver"],
                        model_id=job["model_id"], config_sha256=job["config_sha256"],
                        max_cost_usd=job.get("max_cost_usd"))
            elif timestamp(row["closes_at"]) <= now:
                add("submit_benchmark", job["execution_id"], "benchmarks", **binding,
                    execution_id=job["execution_id"], artifact_sha256=job["artifact_sha256"])
        if historical:
            if not row.get("historical_evaluation_ready"):
                add("evaluate_historical_preview", rid, "evaluation", **binding, scope_id=scope["scope_id"])
            elif policy is not None and not missing_receipts:
                add("publish_historical_preview", rid, "retrospective", **binding, scope_id=scope["scope_id"])
            if missing_receipts:
                block(rid, "awaiting-required-benchmark-receipts")
            continue
        release_policy = delayed_retrospective_release(row)
        if release_policy and not release_policy.get("activated_by_round_id"):
            candidates = [r for r in production if timestamp(r["opens_at"]) > timestamp(row["opens_at"]) and timestamp(r["opens_at"]) <= now and r["status"] == "open"]
            if not candidates:
                block(rid, "awaiting-exact-successor-activation")
            else:
                # Close only; reveal must wait for expected benchmark receipts.
                add("activate_successor", rid, "reveal", **binding, successor_round_id=candidates[0]["round_id"])
            continue
        if timestamp(row["closes_at"]) > now:
            continue
        if not row.get("evaluation_ready"):
            add("evaluate", rid, "evaluation", **binding)
        if policy is None or missing_receipts:
            if missing_receipts:
                block(rid, "awaiting-required-benchmark-receipts")
            continue
        if row.get("evaluation_ready"):
            add("reveal", rid, "reveal", **binding)
    return {"schema": "foldarium.weekly-reconciliation-plan/v1", "as_of": now.isoformat(), "actions": actions, "blocked": blocked}
