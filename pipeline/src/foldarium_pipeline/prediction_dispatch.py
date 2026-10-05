"""Durable, attempt-bound prediction handoffs and conservative Modal loss proof.

Unknown submission outcomes retain their intent. Neither absent output nor an
expired lease is sufficient authority to dispatch another scientific attempt.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .modal_dispatch import PredictionDispatchObservationUncertain
from .weekly_reconciliation import timestamp


def _run(private: Any, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = private.campaign_prediction_run_statuses(parameters['campaign_id'])
    matching = [row for row in rows if row['run_id'] == parameters['run_id']]
    if len(matching) != 1:
        raise ValueError('exact campaign prediction run is absent')
    return matching[0]


def observe_terminal_dispatch(call_id: str, *, modal_api: Any) -> Mapping[str, Any]:
    """Public SDK graph only; missing/ambiguous retained input stays uncertain."""
    try:
        nodes = list(modal_api.FunctionCall.from_id(call_id).get_call_graph())
        matching = []
        while nodes:
            node = nodes.pop()
            nodes.extend(node.children)
            if node.function_call_id == call_id:
                matching.append(node)
    except Exception as error:
        raise PredictionDispatchObservationUncertain('dispatch graph is unavailable') from error
    if len(matching) != 1:
        raise PredictionDispatchObservationUncertain('dispatch has no unique retained input')
    node = matching[0]
    status = getattr(node.status, 'name', None)
    if status not in {'PENDING', 'SUCCESS', 'FAILURE', 'INIT_FAILURE', 'TERMINATED', 'TIMEOUT'}:
        raise PredictionDispatchObservationUncertain('dispatch input state is unknown')
    return {'call_id': call_id, 'task_id': node.task_id, 'terminal_status': status}


def submit_prepared_dispatch(private: Any, dispatch: Mapping[str, Any], *, spawn: Callable[..., str]) -> str:
    """Single durable submission boundary, shared by manual intake and cron."""
    claim = private._rpc('claim_weekly_prediction_dispatch_v1', {'p_dispatch_id': dispatch['dispatch_id']})
    if not claim['first_claim']:
        if claim.get('call_id'):
            return claim['call_id']
        raise PredictionDispatchObservationUncertain('submission acknowledgement is absent; original intent retained')
    call_id = spawn(dispatch['execution_task'], dispatch['dispatch_id'], dispatch.get('retry_request'))
    private._rpc('acknowledge_weekly_prediction_dispatch_v1', {
        'p_dispatch_id': dispatch['dispatch_id'], 'p_call_id': call_id})
    return call_id


def reconcile_prediction_dispatch(private: Any, store: Any, action: Mapping[str, Any], *,
        modal_api: Any, spawn: Callable[[Mapping[str, Any], str, Mapping[str, Any] | None], str],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> Mapping[str, Any]:
    p = action['parameters']
    row = _run(private, p)
    attempt = int(p.get('attempt_number', 1))
    dispatch = private._rpc('get_weekly_prediction_dispatch_v1', {
        'p_run_id': row['run_id'], 'p_attempt_number': attempt})
    if not dispatch:
        if attempt != 1:
            raise PredictionDispatchObservationUncertain('authorized retry has no exact durable dispatch intent')
        if row['status'] not in {'pending', 'queued'} or row['attempt_count'] != 0:
            return {'status': 'already-claimed'}
        prior = store.snapshot().get('action_history', {}).get(action['action_key'], {}).get('dispatch_receipt')
        if prior and prior.get('provider') != 'modal':
            raise ValueError('prediction dispatch provider changed')
        dispatch = private._rpc('prepare_weekly_prediction_dispatch_v1', {
            'p_run_id': row['run_id'], 'p_execution_task': row['task_payload'],
            'p_retry_request': None, 'p_existing_call_id': prior['call_id'] if prior else None})
    if dispatch['run_id'] != row['run_id'] or dispatch['attempt_number'] != attempt:
        raise ValueError('prediction dispatch identity changed')
    # A fresh DB read protects the claim/finish race before any new operation.
    row = _run(private, p)
    if row['attempt_count'] > attempt or (row['attempt_count'] == attempt and row['status'] in {'succeeded', 'failed', 'cancelled'}):
        return {'status': 'attempt-already-terminal'}
    if dispatch.get('call_id'):
        evidence = observe_terminal_dispatch(dispatch['call_id'], modal_api=modal_api)
        if evidence['terminal_status'] == 'PENDING':
            return {'status': 'accepted-dispatch-still-pending'}
        if evidence['terminal_status'] == 'SUCCESS':
            # Normal success must already have finalized the durable run. A
            # disagreement needs artifact recovery, never synthetic failure.
            raise PredictionDispatchObservationUncertain('successful worker lacks matching durable completion')
        row = _run(private, p)
        if row['attempt_count'] > attempt or (row['attempt_count'] == attempt and row['status'] in {'succeeded', 'failed', 'cancelled'}):
            return {'status': 'attempt-already-terminal'}
        if row['status'] == 'running':
            if evidence['terminal_status'] in {'FAILURE', 'INIT_FAILURE'}:
                raise PredictionNativeRecoveryRequired('claimed worker failure may have unpublished native outputs; artifact recovery review required')
            if not row.get('lease_expires_at') or timestamp(row['lease_expires_at']) >= now():
                return {'status': 'terminal-worker-awaits-lease-expiry'}
            if not evidence['task_id'] or row.get('lease_owner') != 'modal:' + evidence['task_id']:
                raise PredictionDispatchObservationUncertain('terminal call does not own exact current lease')
        return private._rpc('record_weekly_prediction_worker_loss_v1', {
            'p_dispatch_id': dispatch['dispatch_id'], 'p_call_id': evidence['call_id'],
            'p_task_id': evidence['task_id'], 'p_terminal_status': evidence['terminal_status'],
            'p_expected_attempt_count': row['attempt_count'],
            'p_expected_lease_owner': row.get('lease_owner'),
            'p_expected_lease_expires_at': row.get('lease_expires_at')})
    # Worker-side acknowledgement can heal an accepted invocation even if the
    # control caller lost its response. Unknown outcomes never resubmit.
    call_id = submit_prepared_dispatch(private, dispatch, spawn=spawn)
    return {'status': 'submitted', 'dispatch_receipt': {'provider': 'modal', 'call_id': call_id}}


class PredictionNativeRecoveryRequired(RuntimeError):
    """Preserved or unverifiable native evidence requires artifact recovery."""


def inspect_prediction_retry_evidence(private: Any, row: Mapping[str, Any]) -> str | None:
    """Return the exact reviewed logs-only descriptor hash, never its contents.

    Missing legacy evidence preserves the existing retry policy. A catalogued
    descriptor must be completely verified and show a complete native-free
    inventory. Native outputs (including omitted entries) need recovery review.
    """
    import hashlib
    import json
    import re
    from .contracts import canonical_json
    evidence = private._rpc('get_prediction_failure_diagnostics_v1', {
        'p_run_id': row['run_id'], 'p_attempt_count': row['attempt_count']})
    if not isinstance(evidence, Mapping) or evidence.get('scientific_artifacts_registered') is not False or (row.get('result') or {}).get('samples'):
        raise PredictionNativeRecoveryRequired('registered scientific artifacts or unknown evidence require recovery review')
    if 'diagnostics' not in evidence:
        raise PredictionNativeRecoveryRequired('diagnostic catalog inspection is incomplete')
    catalog = evidence['diagnostics']
    if catalog is None:
        return None
    try:
        digest = catalog['descriptor_sha256']
        task_digest = hashlib.sha256(canonical_json(row['task_payload']).encode()).hexdigest()
        if not re.fullmatch('[0-9a-f]{64}', digest) or catalog['run_id'] != row['run_id'] or catalog['attempt_count'] != row['attempt_count'] or catalog['registered_task_sha256'] != task_digest:
            raise ValueError('diagnostic catalog source mismatch')
        if type(catalog['descriptor_size_bytes']) is not int or not 1 <= catalog['descriptor_size_bytes'] <= 262144:
            raise ValueError('diagnostic descriptor exceeds bound')
        def private_uri(uri, sha):
            expected = f'supabase://{private.storage_bucket}/sha256/{sha[:2]}/{sha}'
            if uri != expected:
                raise ValueError('diagnostic artifact is outside exact private storage')
        private_uri(catalog['descriptor_uri'], digest)
        content = private.download_content_object(catalog['descriptor_uri'], expected_sha256=digest)
        if len(content) != catalog['descriptor_size_bytes'] or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('diagnostic descriptor content mismatch')
        descriptor = json.loads(content)
        if descriptor['format_version'] != 'prediction-failure-diagnostics/v1' or any(descriptor[key] != catalog[key] for key in ('run_id','attempt_count','worker_id','registered_task_sha256')):
            raise ValueError('diagnostic descriptor source mismatch')
        if row['attempt_count'] != 1 or descriptor.get('effective_task_sha256') != task_digest:
            raise ValueError('first execution differs from the registered scientific task')
        task = row['task_payload']
        if any(descriptor[key] != task[key] for key in ('method','method_version','container_image')):
            raise ValueError('diagnostic method identity mismatch')
        if descriptor.get('inventory_limited') is not False or descriptor.get('collection_deadline_reached') is not False:
            raise ValueError('diagnostic inventory is incomplete')
        files = descriptor['files']
        if not isinstance(files, list) or len(files) > 2000:
            raise ValueError('diagnostic file inventory is invalid')
        for entry in files:
            if not isinstance(entry, dict) or entry.get('role') != 'log':
                raise ValueError('native or unidentified output needs artifact recovery')
            if entry.get('relative_path') not in {'logs/stdout.log','logs/stderr.log'} or entry.get('status') not in {
                    'included','empty','unreadable_or_nonregular','total_size_limit_omitted','symlink_omitted'}:
                raise ValueError('diagnostic log inventory is malformed')
            if entry.get('status') == 'included':
                sha = entry.get('sha256')
                if not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha) or type(entry.get('size_bytes')) is not int or entry['size_bytes'] < 0:
                    raise ValueError('diagnostic log identity is invalid')
                private_uri(entry['object_uri'],sha)
        return digest
    except Exception as error:
        raise PredictionNativeRecoveryRequired('exact failure evidence requires artifact recovery review') from error
