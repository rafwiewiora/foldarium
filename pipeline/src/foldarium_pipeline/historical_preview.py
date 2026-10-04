"""Explicit historical Preview research, preserving its original closed identity.

No human ballots are read, no production identity is invented, and no round row
is reopened or revealed. All scientific and sanitized publication bytes remain in
private Storage; only the atomic publication catalog authorizes public serving.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_json
from .private_evaluation import (
    _build_bound_evaluation_artifact, _describe_bound_evaluation_artifact,
    recover_legacy_ligand_eligibility,
)
from urllib.parse import urlsplit
from .reference_disposition import validate_reference_disposition
from .weekly_llm_contract import digest_post_close_benchmark, validate_post_close_benchmark
from .weekly_reconciliation import timestamp
from .weekly_selector import verify_selector_kit_zip
from .wednesday_reveal import (
    _private_index, _validated_round, _evaluate_validated_round,
    evaluate_ligand_pose, fetch_rcsb_released_reference,
)

EVALUATION_FORMAT = 'foldarium.historical-preview-evaluation/v1'
PUBLICATION_FORMAT = 'foldarium.historical-preview-research/v1'
SCOPE_FIELDS = ('round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'private_index_sha256', 'opens_at', 'closes_at')


class HistoricalPreviewError(ValueError):
    pass


def _assert_sanitized_artifact(value):
    forbidden = {'user_id', 'session_id', 'participant_link', 'vote_id', 'trace', 'app_state', 'auth', 'object_uri', 'reference_uri', 'private_index', 'prompt', 'reasoning_trace'}
    if isinstance(value, Mapping):
        if forbidden.intersection(value):
            raise HistoricalPreviewError('historical public artifact contains private fields')
        for child in value.values():
            _assert_sanitized_artifact(child)
    elif isinstance(value, list):
        for child in value:
            _assert_sanitized_artifact(child)
    elif isinstance(value, str) and (urlsplit(value).scheme or '://' in value):
        raise HistoricalPreviewError('historical public artifact contains a URI')


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source(row: Mapping[str, Any]) -> dict[str, Any]:
    return {**{key: row[key] for key in SCOPE_FIELDS if key != 'private_index_sha256'},
        'private_index_sha256': row['metadata']['private_index']['sha256']}


def require_historical_source(row: Mapping[str, Any], scope: Mapping[str, Any], *, now: datetime | None = None) -> None:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or row.get('environment') != 'preview' or row.get('status') != 'open' or row.get('revealed_at') is not None or row.get('reveal_manifest') is not None or timestamp(row['closes_at']) > current:
        raise HistoricalPreviewError('historical research requires the original closed unrevealed Preview')
    actual = _source(row)
    for key in SCOPE_FIELDS:
        if key in {'opens_at', 'closes_at'}:
            equal = timestamp(actual[key]) == timestamp(scope[key])
        else:
            equal = actual[key] == scope.get(key)
        if not equal:
            raise HistoricalPreviewError('historical source binding changed: ' + key)
    if not scope.get('scope_id'):
        raise HistoricalPreviewError('explicit historical scope authorization is missing')


def describe_historical_evaluation(content: bytes, *, expected_sha256: str | None = None) -> dict[str, Any]:
    descriptor = _describe_bound_evaluation_artifact(content, expected_artifact_sha256=expected_sha256,
        expected_environment='preview', expected_format_version=EVALUATION_FORMAT)
    counts = json.loads(content)['counts']
    descriptor.update({key: counts[key] for key in ('scorable_item_count', 'excluded_item_count') if key in counts})
    return descriptor


def _verify_descriptor(row, scope, descriptor, content):
    require_historical_source(row, scope)
    described = describe_historical_evaluation(content, expected_sha256=descriptor['artifact_sha256'])
    for key, value in described.items():
        observed = descriptor.get(key)
        if key in {'round_opens_at', 'round_closes_at'}:
            equal = timestamp(observed) == timestamp(value)
        else:
            equal = observed == value
        if not equal:
            raise HistoricalPreviewError('historical evaluation descriptor differs: ' + key)
    for key in ('round_id', 'campaign_id', 'environment', 'blind_manifest_sha256', 'private_index_sha256'):
        if described[key] != scope[key]:
            raise HistoricalPreviewError('historical evaluation source differs: ' + key)
    for key in ('opens_at', 'closes_at'):
        if timestamp(described['round_' + key]) != timestamp(scope[key]):
            raise HistoricalPreviewError('historical evaluation window differs')
    return described


def _catalog(coordinator, round_id):
    result = coordinator._rpc('get_weekly_historical_preview_v1', {'p_round_id': round_id})
    if not isinstance(result, dict) or not isinstance(result.get('scope'), dict):
        raise HistoricalPreviewError('explicit historical scope authorization is missing')
    return result


def _store_private(coordinator, content: bytes) -> Mapping[str, Any]:
    stored = coordinator.store_bytes(content, 'application/json')
    digest = _sha(content)
    if stored.get('sha256') != digest or stored.get('size_bytes') != len(content) or stored.get('object_uri') != f'supabase://{coordinator.storage_bucket}/sha256/{digest[:2]}/{digest}':
        raise HistoricalPreviewError('historical artifact Storage receipt differs')
    return stored


def materialize_historical_evaluation(round_id: str, *, coordinator: Any, destination: Path,
        prediction_resolver=None, reference_resolver=fetch_rcsb_released_reference,
        evaluator=evaluate_ligand_pose) -> dict[str, Any]:
    coordinator.require_private_bucket()
    catalog = _catalog(coordinator, round_id)
    row, private_content = coordinator.weekly_quiz_reveal_inputs(round_id)
    scope = catalog['scope']
    require_historical_source(row, scope)
    if catalog.get('evaluation') is not None:
        descriptor = catalog['evaluation']
        content = coordinator.download_content_object(descriptor['artifact_object_uri'], expected_sha256=descriptor['artifact_sha256'])
        _verify_descriptor(row, scope, descriptor, content)
        # Re-enter the locked source validation even on an idempotent replay.
        coordinator._rpc('register_weekly_historical_evaluation_v1', {'p_round_id': round_id, 'p_descriptor': descriptor})
        return {'status': 'already-evaluated-historical-preview', 'round_id': round_id, 'evaluation_id': descriptor['evaluation_id']}
    recovered = recover_legacy_ligand_eligibility(coordinator, row, private_content)
    private = _private_index(row, private_content)
    validated_id, blind, items = _validated_round(row, private, recovered_ligand_eligibility=recovered)
    resolver = prediction_resolver or (lambda choice: coordinator.download_predicted_complex(choice.get('run_id'), choice.get('sample_id')))
    result = _evaluate_validated_round(validated_id, blind, items, destination,
        prediction_resolver=resolver, reference_resolver=reference_resolver,
        evaluator=evaluator, include_answer_overlays=True)
    result['status'] = 'evaluated-private-postclose'
    content, descriptor = _build_bound_evaluation_artifact(row, result,
        environment='preview', format_version_override=EVALUATION_FORMAT)
    descriptor = describe_historical_evaluation(content)
    stored = _store_private(coordinator, content)
    descriptor['artifact_object_uri'] = stored['object_uri']
    _verify_descriptor(row, scope, descriptor, content)
    registered = coordinator._rpc('register_weekly_historical_evaluation_v1', {'p_round_id': round_id, 'p_descriptor': descriptor})
    if registered != descriptor:
        raise HistoricalPreviewError('historical evaluation registration differs')
    return {'status': 'evaluated-historical-preview', 'round_id': round_id, 'evaluation_id': descriptor['evaluation_id']}


def _validated_executions(coordinator, public_coordinator, row):
    state = coordinator._rpc('weekly_automation_snapshot_v1', {})
    current = next(r for r in state['rounds'] if r['round_id'] == row['round_id'])
    policy = current.get('automation_policy')
    if not policy or policy['environment'] != 'preview' or policy['blind_manifest_sha256'] != row['blind_manifest_sha256']:
        raise HistoricalPreviewError('historical publication requires explicit frozen benchmark expectations')
    expected = set(policy['expected_execution_ids'])
    jobs = current['benchmark_jobs']
    if expected != {j['execution_id'] for j in jobs} or any(not j.get('receipt') for j in jobs):
        raise HistoricalPreviewError('historical publication requires every immutable benchmark receipt')
    if not jobs:
        return []
    catalog_kit = current['kit']
    kit = verify_selector_kit_zip(public_coordinator.download_content_object('supabase://' + catalog_kit['storage_path']))
    if kit['kit_sha256'] != catalog_kit['kit_sha256'] or kit['round_id'] != row['round_id'] or kit['environment'] != 'preview' or kit['blind_manifest_sha256'] != row['blind_manifest_sha256']:
        raise HistoricalPreviewError('historical benchmark kit binding differs')
    executions = []
    for job in sorted(jobs, key=lambda j: j['execution_id']):
        content = coordinator.download_content_object(job['artifact_uri'], expected_sha256=job['artifact_sha256'])
        execution = validate_post_close_benchmark(json.loads(content), kit=kit, context_environment="preview", context_round_id=row["round_id"])
        receipt = job['receipt']
        if execution['execution_id'] != job['execution_id'] or execution['provider'] != job['driver'] or execution['model']['requested_id'] != job['model_id'] or execution['provenance']['config_sha256'] != job['config_sha256'] or digest_post_close_benchmark(execution, kit=kit) != receipt['execution_sha256']:
            raise HistoricalPreviewError('historical benchmark artifact differs from its receipt')
        # Payload digest is separately recorded by the immutable submission RPC.
        from .weekly_selector import digest_selector_submission
        if digest_selector_submission(execution['payload']) != receipt['payload_digest']:
            raise HistoricalPreviewError('historical benchmark payload differs from its receipt')
        executions.append((execution, receipt))
    return executions


def build_historical_publication(row, scope, descriptor, content: bytes, executions) -> bytes:
    described = _verify_descriptor(row, scope, descriptor, content)
    artifact = json.loads(content)
    reveal = artifact['reveal_manifest']
    references = {r['item_id']: r for r in artifact['references']}
    blind_items = {i['id']: i for i in artifact['blind_manifest']['items']}
    items = []
    for item in sorted(reveal['items'], key=lambda i: i['id']):
        blind_choices = {c['id']: c for c in blind_items[item['id']]['choices']}
        unscorable = validate_reference_disposition(item)
        choices = []
        for choice in sorted(item['choices'], key=lambda c: c['id']):
            public = {key: choice[key] for key in ('id', 'method', 'method_version', 'rmsd', 'correct', 'accepted_correct', 'evaluator_version', 'reference_sha256')}
            public['cluster_id'] = blind_choices[choice['id']].get('cluster_id')
            score = blind_choices[choice['id']].get('smina_score')
            if isinstance(score, Mapping) and isinstance(score.get('value'), (int, float)):
                public['smina_affinity_kcal_mol'] = score['value']
            choices.append(public)
        projected = {'id': item['id'], 'target_id': references[item['id']]['target_id'], 'choices': choices, 'evaluation_status': 'unscorable' if unscorable else 'scored'}
        if unscorable:
            projected['reference_disposition'] = item['reference_disposition']
        items.append(projected)
    by_item = {i['id']: i for i in items}
    benchmarks = []
    for execution, receipt in executions:
        decisions = []
        for decision in execution['payload']['items']:
            scored_item = by_item[decision['item_id']]
            choices = scored_item['choices']
            no_raw_correct = not any(c['correct'] for c in choices)
            no_accepted_correct = not any(c['accepted_correct'] for c in choices)
            exact = decision['unclustered']
            exact_correct = no_raw_correct if exact['selection_kind'] == 'none' else next(c['correct'] for c in choices if c['id'] == exact['choice_id'])
            clustered = decision['clustered']
            cluster_correct = no_accepted_correct if clustered['selection_kind'] == 'none' else any(c['accepted_correct'] for c in choices if c['cluster_id'] == clustered['cluster_id'])
            if scored_item['evaluation_status'] == 'unscorable':
                exact_correct = cluster_correct = None
            decisions.append({'item_id': decision['item_id'], 'clustered': clustered, 'unclustered': exact, 'clustered_correct': cluster_correct, 'unclustered_correct': exact_correct})
        benchmarks.append({'execution_id': execution['execution_id'], 'driver': execution['provider'], 'model_id': execution['model']['requested_id'], 'config_sha256': execution['provenance']['config_sha256'], **{k: receipt[k] for k in ('execution_sha256', 'payload_digest')}, 'decisions': sorted(decisions, key=lambda d: d['item_id'])})
    public = {'format_version': PUBLICATION_FORMAT, 'scope': 'historical-preview-research',
        'human_cohort': {'included': False, 'denominator': None, 'reason': 'no-human-votes-in-historical-research'},
        'source': {**{key: scope[key] for key in SCOPE_FIELDS}, 'scope_id': scope['scope_id'], 'evaluation_id': described['evaluation_id'], 'evaluation_artifact_sha256': described['artifact_sha256'], **{key: described[key] for key in ('reveal_manifest_sha256', 'reference_set_sha256', 'prediction_set_sha256')}},
        'counts': artifact['counts'], 'policy': artifact['policy'], 'items': items,
        'benchmarks': sorted(benchmarks, key=lambda b: b['execution_id'])}
    _assert_sanitized_artifact(public)
    return canonical_json(public).encode()


def materialize_historical_publication(round_id: str, *, coordinator: Any, public_coordinator: Any) -> dict[str, Any]:
    coordinator.require_private_bucket()
    catalog = _catalog(coordinator, round_id)
    row = coordinator.weekly_quiz_round(round_id)
    scope, descriptor = catalog['scope'], catalog.get('evaluation')
    require_historical_source(row, scope)
    if not descriptor:
        raise HistoricalPreviewError('historical publication requires verified private evaluation')
    content = coordinator.download_content_object(descriptor['artifact_object_uri'], expected_sha256=descriptor['artifact_sha256'])
    executions = _validated_executions(coordinator, public_coordinator, row)
    public_content = build_historical_publication(row, scope, descriptor, content, executions)
    stored = _store_private(coordinator, public_content)
    registered = coordinator._rpc('publish_weekly_historical_preview_v1', {'p_round_id': round_id,
        'p_public_artifact_canonical': public_content.decode(), 'p_object_uri': stored['object_uri']})
    if registered.get('public_artifact_sha256') != stored['sha256'] or registered.get('round_id') != round_id:
        raise HistoricalPreviewError('historical publication registration differs')
    return {'status': 'published-historical-preview', 'round_id': round_id, 'public_artifact_sha256': stored['sha256']}
