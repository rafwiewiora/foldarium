"""New enrollment boundaries; legacy immutable policy/receipt semantics remain."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import unittest

from foldarium_pipeline.weekly_reconciliation import (
    Gates, benchmark_expectations, plan_reconciliation, validate_benchmark_policy,
)
from foldarium_pipeline.weekly_reconciliation_store import freeze_expected_benchmarks
from test_weekly_reconciliation import ALL, DIGEST, round_row, snapshot

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)

def legacy_policy():
    return {"schema":"foldarium.weekly-benchmark-policy/v1", "policy_id":"reviewed-v1", "required_methods":[{"driver":"anthropic-api", "model_id":"exact-model", "config_sha256":DIGEST,"max_cost_usd":10}]}

def policy():
    return {**legacy_policy(),
        'schema': 'foldarium.weekly-benchmark-policy/v2', 'policy_id': 'canonical-production-v2',
        'enrollment_scope': {'kind': 'canonical-production-weekly', 'first_release_date': '2026-10-03',
            'production_round_suffix': 'beta-v2', 'max_weekly_cost_usd': 10}}

def current(release='2026-10-03', **changes):
    return round_row(f'weekly-{release}-beta-v2', campaign_id=f'wwpdb-{release}',
        automation_policy=None, opens_at=f'{release}T03:00:00Z', opened_at=f'{release}T03:00:00Z',
        closes_at='2026-10-07T00:00:00Z', **changes)

def plan(state, pol=None, now=NOW):
    return plan_reconciliation(state, now=now, gates=ALL, preview_version='v4',
        production_suffix='beta-v2', benchmark_policy=policy() if pol is None else pol)

def freezes(state, **kwargs):
    return [a for a in plan(state, **kwargs)['actions'] if a['kind']=='freeze_policy']

class Coordinator:
    def __init__(self, state): self.state=state; self.calls=[]
    def _rpc(self, name, params):
        if name=='weekly_automation_snapshot_v1': return self.state
        self.calls.append((name,params)); return {'round_id':params['p_round_id']}

class BenchmarkScopeTests(unittest.TestCase):
    def test_v1_canonical_bytes_digest_and_execution_uuid_unchanged(self):
        p=legacy_policy()
        normalized=validate_benchmark_policy(p)
        expected='{"policy_id":"reviewed-v1","required_methods":[{"config_sha256":"'+DIGEST+'","driver":"anthropic-api","max_cost_usd":10,"model_id":"exact-model"}],"schema":"foldarium.weekly-benchmark-policy/v1"}'
        self.assertEqual(json.dumps(normalized,sort_keys=True,separators=(',',':')),expected)
        digest,jobs=benchmark_expectations(round_row(),p)
        self.assertEqual(digest,hashlib.sha256(expected.encode()).hexdigest())
        # Pinned independently against the pre-v2 parent implementation.
        self.assertEqual(jobs[0]['execution_id'],'c046221b-cda5-5411-b840-4327058d5614')

    def test_current_and_future_canonical_production_only(self):
        self.assertEqual(len(freezes(snapshot(current()))),1)
        future=current('2026-10-10');future['closes_at']='2026-10-14T00:00:00Z'
        state=snapshot(future,campaigns=[{'campaign_id':future['campaign_id'],'release_date':'2026-10-10','runs':[]}])
        self.assertEqual(len(freezes(state,now=datetime(2026,10,11,tzinfo=timezone.utc))),1)
        self.assertEqual(freezes(state),[])  # not yet open

    def test_historical_preview_before_start_alias_expiry_and_date_mismatch_block(self):
        for change in ({'environment':'preview'}, {'round_id':'weekly-2026-10-03-sibling'},
                       {'closes_at':'2026-10-05T00:00:00Z'}, {'opened_at':None}, {'revealed_at':'2026-10-04T00:00Z'}):
            r=current();r.update(change)
            self.assertEqual(freezes(snapshot(r)),[],change)
        for release in ('2026-09-26','2026-10-04'):
            r=current(release);state=snapshot(r,campaigns=[{'campaign_id':r['campaign_id'],'release_date':release,'runs':[]}])
            self.assertEqual(freezes(state),[])
        r=current();state=snapshot(r);state['campaigns'][0]['release_date']='2026-10-10'
        self.assertEqual(freezes(state),[])
        r=current(environment='preview');r['historical_scope']={k:r[k] for k in ('round_id','campaign_id','environment','blind_manifest_sha256','opens_at','closes_at')}
        r['private_index_sha256']=DIGEST;r['historical_scope']['private_index_sha256']=DIGEST;r['historical_scope']['scope_id']='fixture'
        r['closes_at']='2026-10-04T00:00Z';r['historical_scope']['closes_at']=r['closes_at']
        result=plan(snapshot(r));self.assertEqual([a for a in result['actions'] if a['kind']=='freeze_policy'],[])
        self.assertIn('benchmark-enrollment-outside-production-scope',[b['reason'] for b in result['blocked']])

    def test_frozen_sibling_blocks_new_week_without_invalidating_old_authority(self):
        r=current();sibling=round_row('archived-preview',campaign_id=r['campaign_id'],environment='preview')
        state=snapshot(r,sibling)
        self.assertEqual(freezes(state),[])
        legacy=round_row();result=plan(snapshot(legacy))
        self.assertIn('reveal',[a['kind'] for a in result['actions'] if a['identity']==legacy['round_id']])
        historical=round_row('historical-approved',environment='preview',private_index_sha256=DIGEST,historical_evaluation_ready=True)
        historical['historical_scope']={k:historical[k] for k in ('round_id','campaign_id','environment','blind_manifest_sha256','private_index_sha256','opens_at','closes_at')}
        historical['historical_scope']['scope_id']='approved-scope'
        self.assertIn('publish_historical_preview',[a['kind'] for a in plan(snapshot(historical))['actions']])

    def test_scope_changes_hash_uuid_and_nonempty_total_cap_is_enforced(self):
        a,jobs=benchmark_expectations(current(),policy());changed=policy();changed['enrollment_scope']['first_release_date']='2026-10-10'
        b,new=benchmark_expectations(current(),changed);self.assertNotEqual(a,b);self.assertNotEqual(jobs,new)
        for mutate in (lambda p:p['required_methods'].clear(), lambda p:p['required_methods'].append({**p['required_methods'][0],'model_id':'second'}),
                       lambda p:p['enrollment_scope'].update(first_release_date='2026-10-04'),lambda p:p['enrollment_scope'].update(max_weekly_cost_usd=True)):
            value=policy();mutate(value)
            with self.assertRaises(ValueError):validate_benchmark_policy(value)

    def test_freeze_executor_refetches_scope_digest_jobs_and_uses_atomic_rpc(self):
        state=snapshot(current());action=freezes(state)[0];c=Coordinator(state)
        def execute(a=action,p=policy()):
            return freeze_expected_benchmarks(c,a,benchmark_policy=p,lifecycle_scope=None,
                preview_version='v4',production_suffix='beta-v2',clock=lambda:NOW)
        execute();self.assertEqual(c.calls[0][0],'freeze_weekly_automation_policy_v2')
        self.assertEqual(c.calls[0][1]['p_blind_manifest_sha256'],DIGEST)
        for mutate in (lambda a:a['parameters'].update(policy_sha256='f'*64),
                       lambda a:a['parameters']['expected_executions'][0].update(max_cost_usd=11)):
            stale=deepcopy(action);mutate(stale)
            with self.assertRaisesRegex(ValueError,'stale or outside'):execute(stale)
        changed=policy();changed['enrollment_scope']['first_release_date']='2026-10-10'
        with self.assertRaisesRegex(ValueError,'stale or outside'):execute(p=changed)
        c.state['rounds'][0]['closes_at']='2026-10-05T00:00Z'
        with self.assertRaisesRegex(ValueError,'stale or outside'):execute()
        self.assertEqual(len(c.calls),1)

    def test_explicit_v1_stale_intent_cannot_be_replayed_under_v2(self):
        state=snapshot(current());old=freezes(state,pol=legacy_policy())[0]
        c=Coordinator(state)
        with self.assertRaisesRegex(ValueError,'stale or outside'):
            freeze_expected_benchmarks(c,old,benchmark_policy=policy(),lifecycle_scope=None,
                preview_version='v4',production_suffix='beta-v2',clock=lambda:NOW)
        self.assertEqual(c.calls,[])

if __name__=='__main__':unittest.main()
