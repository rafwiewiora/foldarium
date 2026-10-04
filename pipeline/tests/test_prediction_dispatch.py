from copy import deepcopy
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from foldarium_pipeline.prediction_dispatch import reconcile_prediction_dispatch, observe_terminal_dispatch, inspect_prediction_retry_evidence, PredictionNativeRecoveryRequired
from foldarium_pipeline.modal_dispatch import PredictionDispatchObservationUncertain
from test_weekly_reconciliation import plan, snapshot, kinds

NOW=datetime(2026,10,4,tzinfo=timezone.utc)

class DurableDispatchTests(unittest.TestCase):
    def setUp(self):
        self.row={'run_id':'run','status':'pending','attempt_count':0,'max_attempts':1,
            'task_payload':{'task_id':'run'},'lease_owner':None,'lease_expires_at':None}
        self.dispatch=None;self.claimed=False;self.calls=[]
        self.private=Mock();self.private.campaign_prediction_run_statuses.side_effect=lambda _: [deepcopy(self.row)]
        self.private._rpc.side_effect=self.rpc
        self.store=Mock();self.store.snapshot.return_value={'action_history':{}}
        self.action={'kind':'dispatch_prediction','action_key':'key','parameters':{'campaign_id':'campaign','run_id':'run'}}
        self.node=SimpleNamespace(function_call_id='fc-one',task_id='ta-one',status=SimpleNamespace(name='PENDING'),children=[])
        self.call=Mock();self.call.get_call_graph.side_effect=lambda:[self.node]
        self.api=SimpleNamespace(FunctionCall=SimpleNamespace(from_id=Mock(return_value=self.call)))
        self.spawn=Mock(return_value='fc-one')
    def rpc(self,name,p):
        self.calls.append((name,deepcopy(p)))
        if name=='get_weekly_prediction_dispatch_v1':return deepcopy(self.dispatch)
        if name=='prepare_weekly_prediction_dispatch_v1':
            self.dispatch={'dispatch_id':'uuid','run_id':'run','attempt_number':1,'execution_task':p['p_execution_task'],'retry_request':None,'call_id':p.get('p_existing_call_id')}
            return deepcopy(self.dispatch)
        if name=='claim_weekly_prediction_dispatch_v1':
            first=not self.claimed;self.claimed=True
            return {**self.dispatch,'first_claim':first}
        if name=='acknowledge_weekly_prediction_dispatch_v1':
            self.dispatch['call_id']=p['p_call_id'];return deepcopy(self.dispatch)
        if name=='record_weekly_prediction_worker_loss_v1':return {'status':'worker-loss-recorded'}
        raise AssertionError(name)
    def execute(self):
        return reconcile_prediction_dispatch(self.private,self.store,self.action,modal_api=self.api,spawn=self.spawn,now=lambda:NOW)
    def test_initial_submission_is_claimed_once_and_acknowledged_before_return(self):
        self.assertEqual(self.execute()['dispatch_receipt']['call_id'],'fc-one')
        self.assertEqual(self.execute()['status'],'accepted-dispatch-still-pending')
        self.spawn.assert_called_once_with({'task_id':'run'},'uuid',None)
        self.assertEqual(self.calls[1][0],'prepare_weekly_prediction_dispatch_v1')
        self.assertEqual(self.calls[2][0],'claim_weekly_prediction_dispatch_v1')
        self.assertEqual(self.calls[3][0],'acknowledge_weekly_prediction_dispatch_v1')
    def test_unknown_spawn_retains_intent_and_never_resubmits(self):
        self.spawn.side_effect=ConnectionError('lost ack')
        with self.assertRaises(ConnectionError):self.execute()
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.spawn.assert_called_once()
        # Worker-side receipt repairs uncertainty without another spawn.
        self.dispatch['call_id']='fc-one'
        self.assertEqual(self.execute()['status'],'accepted-dispatch-still-pending')
        self.spawn.assert_called_once()
    def test_prior_outbox_receipt_is_adopted_without_spawn(self):
        self.store.snapshot.return_value={'action_history':{'key':{'dispatch_receipt':{'provider':'modal','call_id':'fc-one'}}}}
        self.assertEqual(self.execute()['status'],'accepted-dispatch-still-pending')
        self.spawn.assert_not_called()
    def test_expired_lease_with_pending_call_never_authorizes_loss(self):
        self.execute();self.row.update(status='running',attempt_count=1,lease_owner='modal:ta-one',lease_expires_at='2000-01-01T00:00:00Z')
        self.assertEqual(self.execute()['status'],'accepted-dispatch-still-pending')
        self.assertFalse(any(n=='record_weekly_prediction_worker_loss_v1' for n,_ in self.calls))
    def test_terminal_worker_requires_exact_task_and_expired_lease(self):
        self.execute();self.node.status.name='TIMEOUT'
        self.row.update(status='running',attempt_count=1,lease_owner='modal:ta-one',lease_expires_at='2100-01-01T00:00:00Z')
        self.assertEqual(self.execute()['status'],'terminal-worker-awaits-lease-expiry')
        self.row['lease_expires_at']='2000-01-01T00:00:00Z';self.node.task_id='ta-wrong'
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.node.task_id='ta-one'
        self.assertEqual(self.execute()['status'],'worker-loss-recorded')
        self.assertEqual(self.calls[-1][1]['p_expected_lease_owner'],'modal:ta-one')
        self.assertEqual(self.calls[-1][1]['p_expected_attempt_count'],1)
        self.spawn.assert_called_once()
    def test_missing_graph_output_expiry_or_transport_cannot_authorize_loss(self):
        self.execute()
        for graph in ([],[self.node,self.node]):
            self.call.get_call_graph.side_effect=None;self.call.get_call_graph.return_value=graph
            with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.call.get_call_graph.side_effect=TimeoutError()
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.assertFalse(any(n=='record_weekly_prediction_worker_loss_v1' for n,_ in self.calls))
        self.spawn.assert_called_once()
    def test_success_without_publication_is_not_fabricated_failure(self):
        self.execute();self.node.status.name='SUCCESS'
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.assertFalse(any(n=='record_weekly_prediction_worker_loss_v1' for n,_ in self.calls))
    def test_concurrent_completion_is_not_overwritten(self):
        self.execute();self.node.status.name='FAILURE'
        def graph():
            self.row.update(status='succeeded',attempt_count=1)
            return [self.node]
        self.call.get_call_graph.side_effect=graph
        self.assertEqual(self.execute()['status'],'attempt-already-terminal')
        self.assertFalse(any(n=='record_weekly_prediction_worker_loss_v1' for n,_ in self.calls))
    def test_retry_uses_frozen_task_and_separate_attempt(self):
        self.row.update(status='failed',attempt_count=1,max_attempts=2)
        self.dispatch={'dispatch_id':'retry','run_id':'run','attempt_number':2,'execution_task':{'resources':'frozen-retry'},'retry_request':{'retry_kind':'repeat_once'},'call_id':None}
        self.action['parameters']['attempt_number']=2
        self.assertEqual(self.execute()['status'],'submitted')
        self.spawn.assert_called_once_with({'resources':'frozen-retry'},'retry',{'retry_kind':'repeat_once'})
    def test_legacy_authorized_retry_cannot_synthesize_missing_intent(self):
        self.row.update(status='failed',attempt_count=1,max_attempts=2);self.action['parameters']['attempt_number']=2
        with self.assertRaises(PredictionDispatchObservationUncertain):self.execute()
        self.spawn.assert_not_called()

class DispatchPlannerTests(unittest.TestCase):
    def campaign(self,row):return snapshot(campaigns=[{'campaign_id':'wwpdb-2026-10-03','release_date':'2026-10-03','runs':[row]}])
    def test_retry_dispatch_and_later_worker_loss_have_distinct_outbox_keys(self):
        row={'run_id':'r','status':'failed','attempt_count':1,'max_attempts':2,'dispatch':{'dispatch_id':'d','attempt_number':2,'call_id':None}}
        initial=plan(self.campaign(row))['actions'][0]
        self.assertEqual(initial['kind'],'reconcile_prediction_dispatch')
        self.assertIn('advance_preview',kinds(plan(self.campaign(row))))
        row.update(status='running',attempt_count=2,lease_expires_at='2000-01-01T00:00:00Z');row['dispatch']['call_id']='fc-call'
        loss=plan(self.campaign(row))['actions'][0]
        self.assertNotEqual(initial['action_key'],loss['action_key'])
        row.update(status='failed')
        self.assertEqual(kinds(plan(self.campaign(row))),['advance_preview'])
    def test_expired_legacy_worker_blocks_without_synthetic_call_identity(self):
        row={'run_id':'r','status':'running','attempt_count':1,'max_attempts':1,'lease_expires_at':'2000-01-01T00:00:00Z'}
        result=plan(self.campaign(row))
        self.assertIn('expired-worker-needs-exact-terminal-call-evidence',[b['reason'] for b in result['blocked']])
        self.assertNotIn('reconcile_prediction_dispatch',kinds(result))
    def test_worker_acknowledgement_creates_fresh_action_after_failed_unknown_spawn(self):
        for attempt, status in ((1,'pending'),(2,'failed')):
            row={'run_id':'r','status':status,'attempt_count':attempt-1,'max_attempts':attempt,
                'dispatch':{'dispatch_id':'d','attempt_number':attempt,'call_id':None}}
            unknown=plan(self.campaign(row))['actions'][0]
            row['dispatch']['call_id']='fc-healed'
            observed=plan(self.campaign(row))['actions'][0]
            self.assertNotEqual(unknown['action_key'],observed['action_key'])
            self.assertEqual(observed['parameters']['phase'],'observe')


class RetryEvidenceTests(unittest.TestCase):
    def setUp(self):
        from foldarium_pipeline.contracts import canonical_json
        self.row={'run_id':'run','attempt_count':1,'task_payload':{'method':'boltz2','method_version':'pinned','container_image':'pinned-image'}}
        self.task_sha=hashlib.sha256(canonical_json(self.row['task_payload']).encode()).hexdigest()
        self.descriptor={'format_version':'prediction-failure-diagnostics/v1','run_id':'run','attempt_count':1,'worker_id':'modal:ta-worker',
            'registered_task_sha256':self.task_sha,'effective_task_sha256':self.task_sha,**self.row['task_payload'],'inventory_limited':False,'collection_deadline_reached':False,'files':[]}
        self.private=Mock();self.private.storage_bucket='private'
    def bind(self):
        content=json.dumps(self.descriptor,sort_keys=True,separators=(',',':')).encode()
        digest=hashlib.sha256(content).hexdigest()
        self.catalog={**{k:self.descriptor[k] for k in ('run_id','attempt_count','worker_id','registered_task_sha256')},
            'descriptor_sha256':digest,'descriptor_uri':f'supabase://private/sha256/{digest[:2]}/{digest}','descriptor_size_bytes':len(content)}
        self.private._rpc.return_value={'diagnostics':self.catalog,'scientific_artifacts_registered':False};self.private.download_content_object.return_value=content
        return digest
    def test_absent_legacy_catalog_preserves_existing_retry_policy(self):
        self.private._rpc.return_value={'diagnostics':None,'scientific_artifacts_registered':False}
        self.assertIsNone(inspect_prediction_retry_evidence(self.private,self.row))
        self.private.download_content_object.assert_not_called()
    def test_verified_complete_logs_only_inventory_allows_exact_hash(self):
        sha='a'*64
        self.descriptor['files']=[{'role':'log','relative_path':'logs/stdout.log','status':'included','sha256':sha,'size_bytes':15,'object_uri':f'supabase://private/sha256/aa/{sha}'}]
        digest=self.bind()
        self.assertEqual(inspect_prediction_retry_evidence(self.private,self.row),digest)
    def test_native_omitted_unknown_or_incomplete_inventory_blocks(self):
        for edit in ({'files':[{'role':'native_output','status':'included'}]},
                {'files':[{'role':'native_output','status':'size_limit_omitted'}]},
                {'files':[{'status':'sensitive_path_omitted'}]}, {'inventory_limited':True},
                {'collection_deadline_reached':True}, {'effective_task_sha256':'f'*64}, {'files':None},
                {'files':[{'role':'log','relative_path':'output/model.cif','status':'included'}]}):
            with self.subTest(edit=edit):
                previous=deepcopy(self.descriptor);self.descriptor.update(edit);self.bind()
                with self.assertRaises(PredictionNativeRecoveryRequired):inspect_prediction_retry_evidence(self.private,self.row)
                self.descriptor=previous
    def test_changed_task_or_descriptor_identity_blocks(self):
        for field in ('run_id','attempt_count','registered_task_sha256','worker_id'):
            self.bind();self.catalog[field]='changed'
            with self.subTest(field=field),self.assertRaises(PredictionNativeRecoveryRequired):inspect_prediction_retry_evidence(self.private,self.row)
        self.bind();self.private.download_content_object.return_value=b'corrupt'
        with self.assertRaises(PredictionNativeRecoveryRequired):inspect_prediction_retry_evidence(self.private,self.row)
    def test_public_or_wrong_content_address_is_never_fetched(self):
        self.bind();self.catalog['descriptor_uri']=self.catalog['descriptor_uri'].replace('private/','public/')
        with self.assertRaises(PredictionNativeRecoveryRequired):inspect_prediction_retry_evidence(self.private,self.row)
        self.private.download_content_object.assert_not_called()
    def test_registered_science_blocks_even_without_failure_catalog(self):
        self.private._rpc.return_value={'diagnostics':None,'scientific_artifacts_registered':True}
        with self.assertRaises(PredictionNativeRecoveryRequired):inspect_prediction_retry_evidence(self.private,self.row)
        self.private.download_content_object.assert_not_called()
