from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from foldarium_pipeline.weekly_reconciliation import Gates, plan_reconciliation
from foldarium_pipeline.weekly_reconciliation_store import reconcile_weekly

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
DIGEST = 'a' * 64
ALL = Gates(**{name: True for name in Gates.__dataclass_fields__})
EXECUTION = '00000000-0000-4000-8000-000000000001'


def round_row(rid='weekly-old', **changes):
    return {
        'round_id': rid, 'campaign_id': 'wwpdb-2026-09-05', 'environment': 'production',
        'status': 'open', 'opens_at': '2026-09-05T03:00:00Z',
        'closes_at': '2026-09-23T00:00:00Z', 'opened_at': '2026-09-05T03:00:00Z',
        'blind_manifest_sha256': DIGEST, 'metadata': {}, 'kit': {'kit_sha256': 'b' * 64},
        'automation_policy': {'blind_manifest_sha256': DIGEST, 'expected_execution_ids': []},
        'benchmark_jobs': [], 'evaluation_ready': True, 'featured_registered': True, **changes,
    }


def snapshot(*rounds, campaigns=None):
    return {'campaigns': campaigns if campaigns is not None else [{'campaign_id': 'wwpdb-2026-10-03', 'release_date': '2026-10-03', 'runs': []}], 'rounds': list(rounds)}


def plan(state, gates=ALL, drivers=(), policy=None, scope=None):
    return plan_reconciliation(state, now=NOW, gates=gates, preview_version='v4', production_suffix='beta-v2', available_drivers=drivers, benchmark_policy=policy, lifecycle_scope=scope)


def kinds(result):
    return [a['kind'] for a in result['actions']]


class PlannerTests(unittest.TestCase):
    def test_old_round_revealed_from_backlog_not_current_saturday(self):
        result = plan(snapshot(round_row()))
        self.assertIn('reveal', kinds(result))
        action = next(a for a in result['actions'] if a['kind'] == 'reveal')
        self.assertEqual(action['parameters']['round_id'], 'weekly-old')

    def test_default_gates_allow_no_mutations(self):
        self.assertEqual(plan(snapshot(round_row()), Gates())['actions'], [])

    def test_explicit_empty_expectations_required(self):
        result = plan(snapshot(round_row(automation_policy=None)))
        self.assertNotIn('reveal', kinds(result))
        self.assertIn('benchmark-expectations-not-frozen', [x['reason'] for x in result['blocked']])

    def test_expected_artifact_submitted_before_reveal(self):
        row = round_row(automation_policy={'blind_manifest_sha256': DIGEST, 'expected_execution_ids': [EXECUTION]}, benchmark_jobs=[{'execution_id': EXECUTION, 'driver': 'claude', 'artifact_uri': 'private-ref', 'artifact_sha256': 'c' * 64}])
        result = plan(snapshot(row))
        self.assertIn('submit_benchmark', kinds(result))
        self.assertNotIn('reveal', kinds(result))
        row['benchmark_jobs'][0]['receipt'] = {'execution_id': EXECUTION}
        self.assertIn('reveal', kinds(plan(snapshot(row))))

    def test_missing_remote_runner_is_reported_without_fake_work(self):
        row = round_row(automation_policy={'blind_manifest_sha256': DIGEST, 'expected_execution_ids': [EXECUTION]}, benchmark_jobs=[{'execution_id': EXECUTION, 'driver': 'cursor-cli', 'model_id': 'exact', 'config_sha256': DIGEST, 'max_cost_usd': 10}])
        result = plan(snapshot(row))
        self.assertNotIn('score_benchmark', kinds(result))
        self.assertNotIn('reveal', kinds(result))
        self.assertIn('llm-driver-unavailable:cursor-cli', [x['reason'] for x in result['blocked']])
        self.assertIn('score_benchmark', kinds(plan(snapshot(row), drivers=('cursor-cli',))))

    def test_benchmark_does_not_submit_while_voting_open(self):
        row = round_row(closes_at='2026-10-07T00:00:00Z', automation_policy={'blind_manifest_sha256': DIGEST, 'expected_execution_ids': [EXECUTION]}, benchmark_jobs=[{'execution_id': EXECUTION, 'driver': 'claude', 'artifact_uri': 'x', 'artifact_sha256': 'c' * 64}])
        self.assertNotIn('submit_benchmark', kinds(plan(snapshot(row))))
        self.assertNotIn('reveal', kinds(plan(snapshot(row))))

    def test_policy_membership_and_digest_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'membership'):
            plan(snapshot(round_row(automation_policy={'blind_manifest_sha256': DIGEST, 'expected_execution_ids': [EXECUTION]})))
        with self.assertRaisesRegex(ValueError, 'manifest mismatch'):
            plan(snapshot(round_row(automation_policy={'blind_manifest_sha256': 'x', 'expected_execution_ids': []})))

    def test_lost_handoff_spawn_replanned_after_successor_already_exists(self):
        metadata = {'retrospective_release': {'policy': 'next-weekly-activation', 'original_closes_at': '2026-09-09T00:00:00Z', 'safety_closes_at': '2026-09-23T00:00:00Z', 'configured_at': '2026-09-09T00:00:00Z'}}
        previous = round_row(metadata=metadata)
        successor = round_row('weekly-new', opens_at='2026-09-12T03:00:00Z', opened_at='2026-09-12T03:00:00Z')
        result = plan(snapshot(previous, successor))
        action = next(a for a in result['actions'] if a['kind'] == 'activate_successor')
        self.assertEqual(action['parameters']['successor_round_id'], 'weekly-new')
        self.assertFalse(any(a['kind'] == 'reveal' and a['identity'] == 'weekly-old' for a in result['actions']))
        previous['metadata']['retrospective_release']['activated_by_round_id'] = 'weekly-new'
        result = plan(snapshot(previous, successor))
        self.assertFalse(any(a['kind'] == 'activate_successor' for a in result['actions']))
        self.assertTrue(any(a['kind'] == 'reveal' and a['identity'] == 'weekly-old' for a in result['actions']))

    def test_preview_repair_identity_is_preserved_and_expiry_never_silently_changed(self):
        row = round_row('preview-exact-v5', campaign_id='wwpdb-2026-10-03', environment='preview', closes_at='2026-10-07T00:00:00Z')
        result = plan(snapshot(row))
        self.assertEqual(next(a for a in result['actions'] if a['kind'] == 'promote')['parameters']['source_round_id'], 'preview-exact-v5')
        row['closes_at'] = '2026-09-24T00:00:00Z'
        result = plan(snapshot(row))
        self.assertNotIn('promote', kinds(result))
        self.assertIn('expired-preview-needs-explicit-new-voting-window', [x['reason'] for x in result['blocked']])

    def test_promotion_respects_kit_registration_side_effect_gate(self):
        row = round_row('p', campaign_id='wwpdb-2026-10-03', environment='preview', closes_at='2026-10-07T00:00:00Z')
        self.assertNotIn('promote', kinds(plan(snapshot(row), Gates(production=True))))

    def test_registered_but_unspawned_prediction_recovered_without_new_run(self):
        state = snapshot(campaigns=[{'campaign_id': 'wwpdb-2026-10-03', 'release_date': '2026-10-03', 'runs': [{'run_id': 'run1', 'status': 'pending', 'attempt_count': 0}, {'run_id': 'run2', 'status': 'succeeded', 'attempt_count': 1}]}])
        result = plan(state)
        self.assertEqual(kinds(result), ['dispatch_prediction'])
        self.assertEqual(result['actions'][0]['parameters']['run_id'], 'run1')

    def test_revealed_round_only_publishes_retrospective(self):
        self.assertEqual([a['kind'] for a in plan(snapshot(round_row(status='revealed')))['actions'] if a['identity'] == 'weekly-old'], ['publish_retrospective'])

    def test_action_identity_is_stable_across_time_and_input_order(self):
        state = snapshot(round_row(), round_row('other'))
        a = plan(state)
        state['rounds'].reverse()
        self.assertEqual(a['actions'], plan(state)['actions'])

    def test_ambiguous_legacy_production_does_not_enroll_or_repair_siblings(self):
        rows = [round_row('prototype-' + str(i), automation_policy=None, kit=None, featured_registered=False, evaluation_ready=False) for i in range(6)]
        rows.append(round_row('already-published', status='revealed', automation_policy=None, retrospective_published=True))
        policy = {'schema': 'foldarium.weekly-benchmark-policy/v1', 'policy_id': 'explicit-none', 'required_methods': []}
        result = plan(snapshot(*rows), policy=policy)
        self.assertFalse(any(a['identity'] in {r['round_id'] for r in rows} for a in result['actions']))
        self.assertTrue(any(x['reason'] == 'ambiguous-production-identity-needs-explicit-scope' for x in result['blocked']))

    def test_explicit_scope_binds_exact_legacy_round_and_manifest(self):
        rows = [round_row('prototype', automation_policy=None), round_row('published', status='revealed', automation_policy=None)]
        selected = {key: rows[1][key] for key in ('campaign_id', 'environment', 'round_id', 'blind_manifest_sha256')}
        scope = {'schema': 'foldarium.weekly-lifecycle-scope/v1', 'canonical_rounds': [selected]}
        result = plan(snapshot(*rows), scope=scope)
        self.assertEqual([(a['kind'], a['identity']) for a in result['actions'] if a['identity'] in {'prototype', 'published'}], [('publish_retrospective', 'published')])
        selected['blind_manifest_sha256'] = 'c' * 64
        result = plan(snapshot(*rows), scope=scope)
        self.assertFalse(any(a['identity'] in {'prototype', 'published'} for a in result['actions']))
        self.assertIn('canonical-round-binding-unavailable:production', [x['reason'] for x in result['blocked']])

    def test_frozen_archival_expectations_recover_only_explicitly_enrolled_sibling(self):
        result = plan(snapshot(round_row('archive'), round_row('prototype', automation_policy=None)))
        self.assertTrue(any(a['kind'] == 'reveal' and a['identity'] == 'archive' for a in result['actions']))
        self.assertFalse(any(a['identity'] == 'prototype' for a in result['actions']))

    def test_explicit_preview_identity_resolves_ambiguity_without_latest_guess(self):
        rows = [round_row('preview-v4', environment='preview', campaign_id='wwpdb-2026-10-03', closes_at='2026-10-07T00:00:00Z'), round_row('preview-v5', environment='preview', campaign_id='wwpdb-2026-10-03', closes_at='2026-10-07T00:00:00Z')]
        self.assertNotIn('promote', kinds(plan(snapshot(*rows))))
        scope = {'schema': 'foldarium.weekly-lifecycle-scope/v1', 'canonical_rounds': [{key: rows[0][key] for key in ('campaign_id', 'environment', 'round_id', 'blind_manifest_sha256')}]}
        self.assertEqual(next(a for a in plan(snapshot(*rows), scope=scope)['actions'] if a['kind'] == 'promote')['parameters']['source_round_id'], 'preview-v4')
        scope['canonical_rounds'][0]['round_id'] = 'missing-preview'
        self.assertEqual(plan(snapshot(*rows), scope=scope)['actions'], [])

    def test_drafts_retired_and_never_opened_rounds_have_no_round_actions(self):
        rows = [round_row('draft', status='draft'), round_row('withdrawn', status='withdrawn'), round_row('failed', status='failed'), round_row('never-opened', opened_at=None)]
        self.assertFalse(any(a['identity'] in {r['round_id'] for r in rows} for a in plan(snapshot(*rows))['actions']))

    def test_scope_rejects_unknown_fields_and_duplicate_campaign_environment(self):
        from foldarium_pipeline.weekly_reconciliation import validate_lifecycle_scope
        selected = {key: round_row()[key] for key in ('campaign_id', 'environment', 'round_id', 'blind_manifest_sha256')}
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_lifecycle_scope({'schema': 'foldarium.weekly-lifecycle-scope/v1', 'canonical_rounds': [selected, selected]})
        with self.assertRaisesRegex(ValueError, 'fields'):
            validate_lifecycle_scope({'schema': 'foldarium.weekly-lifecycle-scope/v1', 'canonical_rounds': [{**selected, 'guess_latest': True}]})


class MemoryStore:
    def __init__(self, state):
        self.state = state
        self.enqueued = []
        self.finished = []
        self.unclaimable = set()
    def snapshot(self):
        return deepcopy(self.state)
    def enqueue(self, action):
        self.enqueued.append(action)
    def claim(self, key):
        return None if key in self.unclaimable else {'action_key': key, 'lease_token': 'lease'}
    def record_dispatch(self, claim, receipt):
        self.state.setdefault('action_history', {}).setdefault(claim['action_key'], {})['dispatch_receipt'] = receipt
    def finish(self, claim, outcome, error=None):
        self.state.setdefault('action_history', {}).setdefault(claim['action_key'], {})['updated_at'] = NOW.isoformat()
        self.finished.append((outcome, error))
        return {'status': outcome}


class EngineTests(unittest.TestCase):
    def run_pass(self, store, executor, **kw):
        return reconcile_weekly(store, executor, gates=ALL, preview_version='v4', production_suffix='beta-v2', clock=lambda: NOW, **kw)

    def test_dry_run_has_no_outbox_or_execution_writes(self):
        store = MemoryStore(snapshot(round_row()))
        self.run_pass(store, lambda _: self.fail('executed'))
        self.assertEqual(store.enqueued, [])
        self.assertEqual(store.finished, [])

    def test_transport_success_without_state_change_remains_waiting(self):
        store = MemoryStore(snapshot(round_row()))
        self.run_pass(store, lambda _: {'status': 'success'}, apply=True)
        self.assertTrue(all(s == 'waiting' for s, _ in store.finished))

    def test_state_change_required_for_completed_action(self):
        store = MemoryStore(snapshot(round_row()))
        def execute(action):
            if action['kind'] == 'reveal':
                store.state['rounds'][0]['status'] = 'revealed'
        self.run_pass(store, execute, apply=True)
        self.assertIn(('succeeded', None), store.finished)

    def test_retry_error_records_type_not_sensitive_exception(self):
        store = MemoryStore(snapshot(round_row()))
        def execute(_):
            raise RuntimeError('Bearer very-secret and private answers')
        result = self.run_pass(store, execute, apply=True)
        self.assertNotIn('very-secret', str(result))
        self.assertTrue(all(row == ('error', 'RuntimeError') for row in store.finished))

    def test_unclaimable_first_action_does_not_starve_backlog(self):
        store = MemoryStore(snapshot(round_row()))
        store.unclaimable.add(plan(store.state)['actions'][0]['action_key'])
        called = []
        self.run_pass(store, called.append, apply=True, max_actions=1)
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0]['kind'], 'reveal')


class EnrollmentAndFairnessTests(unittest.TestCase):
    def policy(self, methods=None):
        return {'schema': 'foldarium.weekly-benchmark-policy/v1', 'policy_id': 'reviewed-v1', 'required_methods': methods if methods is not None else [{'driver':'anthropic-api','model_id':'exact-model','config_sha256':DIGEST,'max_cost_usd':10}]}

    def test_policy_enrollment_is_stable_and_does_not_default_to_empty(self):
        state=snapshot(round_row(automation_policy=None))
        self.assertNotIn('freeze_policy', kinds(plan(state)))
        a=next(a for a in plan(state, policy=self.policy())['actions'] if a['kind']=='freeze_policy')
        b=next(a for a in plan(state, policy=self.policy())['actions'] if a['kind']=='freeze_policy')
        self.assertEqual(a,b)
        self.assertEqual(len(a['parameters']['expected_executions']),1)
        changed=self.policy();changed['policy_id']='reviewed-v2'
        c=next(a for a in plan(state, policy=changed)['actions'] if a['kind']=='freeze_policy')
        self.assertNotEqual(a['parameters']['expected_executions'][0]['execution_id'],c['parameters']['expected_executions'][0]['execution_id'])
        empty=next(a for a in plan(state, policy=self.policy([]))['actions'] if a['kind']=='freeze_policy')
        self.assertEqual(empty['parameters']['expected_executions'],[])

    def test_policy_rejects_unbounded_or_unknown_configuration(self):
        for value in [0,-1,float('inf'),float('nan'),True]:
            policy=self.policy();policy['required_methods'][0]['max_cost_usd']=value
            with self.assertRaises(ValueError): plan(snapshot(),policy=policy)
        policy=self.policy();policy['required_methods'][0]['api_key']='secret'
        with self.assertRaises(ValueError): plan(snapshot(),policy=policy)

    def test_old_waiting_actions_do_not_starve_new_ready_actions(self):
        campaigns=[{'campaign_id':f'wwpdb-2026-09-{day:02d}','release_date':f'2026-09-{day:02d}','runs':[]} for day in (5,12,19,26)]
        campaigns.append({'campaign_id':'wwpdb-2026-10-03','release_date':'2026-10-03','runs':[]})
        store=MemoryStore(snapshot(round_row(),campaigns=campaigns))
        called=[]
        args={'gates':ALL,'preview_version':'v4','production_suffix':'beta-v2','apply':True,'max_actions':4,'clock':lambda:NOW}
        reconcile_weekly(store,called.append,**args)
        self.assertEqual(len(called),4)
        called.clear()
        reconcile_weekly(store,called.append,**args)
        self.assertIn('reveal',[a['kind'] for a in called])

    def test_acknowledged_dispatch_receipt_is_durable_before_wait(self):
        store=MemoryStore(snapshot())
        result=reconcile_weekly(store,lambda _: {'dispatch_receipt':{'provider':'fixture','call_id':'call1'}},gates=ALL,preview_version='v4',production_suffix='beta-v2',apply=True,clock=lambda:NOW)
        key=result['actions'][0]['action_key']
        self.assertEqual(store.state['action_history'][key]['dispatch_receipt']['call_id'],'call1')
