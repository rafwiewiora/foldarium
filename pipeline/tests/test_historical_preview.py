from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from foldarium_pipeline.historical_preview import (HistoricalPreviewError, SCOPE_FIELDS,
    materialize_historical_evaluation, materialize_historical_publication,
    describe_historical_evaluation, build_historical_publication, require_historical_source)
from foldarium_pipeline.private_evaluation import describe_private_evaluation_artifact, PrivateEvaluationError
from test_private_evaluation import round_fixture, FakeCoordinator, minimal_reference_mmcif_gz, overlay_evaluation_score, coordinate
from test_weekly_reconciliation import plan, snapshot, round_row, kinds

class HistoricalCoordinator(FakeCoordinator):
    def __init__(self):
        row, _, content = round_fixture()
        row['environment']='preview'
        super().__init__(row,content)
        self.scope={**{k:row[k] for k in SCOPE_FIELDS if k!='private_index_sha256'},'private_index_sha256':row['metadata']['private_index']['sha256'],'scope_id':'00000000-0000-4000-8000-000000000002'}
        self.evaluation=None
        self.publication=None
    def _rpc(self,name,args):
        self.calls.append((name,args))
        if name=='get_weekly_historical_preview_v1':
            return {'scope':deepcopy(self.scope),'evaluation':deepcopy(self.evaluation),'publication':deepcopy(self.publication)}
        if name=='register_weekly_historical_evaluation_v1':
            require_historical_source(self.round_record,self.scope)
            self.evaluation=deepcopy(args['p_descriptor']);return deepcopy(self.evaluation)
        if name=='weekly_automation_snapshot_v1':
            return {'rounds':[{'round_id':self.round_record['round_id'],'automation_policy':{'environment':'preview','blind_manifest_sha256':self.round_record['blind_manifest_sha256'],'expected_execution_ids':[]},'benchmark_jobs':[]}]}
        if name=='publish_weekly_historical_preview_v1':
            require_historical_source(self.round_record,self.scope)
            self.publication={'round_id':args['p_round_id'],'public_artifact_sha256':hashlib.sha256(args['p_public_artifact_canonical'].encode()).hexdigest()}
            return deepcopy(self.publication)
        raise AssertionError(name)

def materialize(coordinator, evaluator=None, *, reference_bytes=None):
    # A fixture SkipTest must not be caught by the production resolver wrapper.
    if reference_bytes is None:
        reference_bytes = minimal_reference_mmcif_gz()
    with tempfile.TemporaryDirectory() as folder:
        return materialize_historical_evaluation(coordinator.round_record['round_id'],coordinator=coordinator,destination=Path(folder),
            reference_resolver=lambda target:coordinate(reference_bytes,'https://files.rcsb.org/download/'+target['target_id']+'.cif.gz'),
            evaluator=evaluator or (lambda *a,**k:overlay_evaluation_score()))

class HistoricalTests(unittest.TestCase):
    def test_private_evaluation_idempotent_original_identity(self):
        c=HistoricalCoordinator();before=deepcopy(c.round_record)
        first=materialize(c);second=materialize(c)
        self.assertEqual(first['evaluation_id'],second['evaluation_id']);self.assertEqual(len(c.stored_contents),1)
        self.assertEqual(c.round_record,before)
        content=c.stored_contents[0]
        self.assertEqual(describe_historical_evaluation(content)['environment'],'preview')
        with self.assertRaises(PrivateEvaluationError):describe_private_evaluation_artifact(content)
    def test_source_drift_fails_before_storage(self):
        for key,value in [('closes_at','2026-08-18T20:00:00Z'),('environment','production'),('blind_manifest_sha256','f'*64)]:
            c=HistoricalCoordinator();c.round_record[key]=value
            with self.assertRaises(HistoricalPreviewError):materialize(c, reference_bytes=b'must not resolve')
            self.assertEqual(c.stored_contents,[])
    def test_no_authorization_or_active_window(self):
        c=HistoricalCoordinator();c.scope=None
        with self.assertRaises(HistoricalPreviewError):materialize(c, reference_bytes=b'must not resolve')
        c=HistoricalCoordinator();c.round_record['closes_at']='2099-01-01T00:00:00Z';c.scope['closes_at']=c.round_record['closes_at']
        with self.assertRaises(HistoricalPreviewError):materialize(c, reference_bytes=b'must not resolve')
    def test_publication_preserves_full_population_without_human_cohort(self):
        c=HistoricalCoordinator();materialize(c);before=deepcopy(c.round_record)
        first=materialize_historical_publication(c.round_record['round_id'],coordinator=c,public_coordinator=object())
        artifact=json.loads(c.stored_contents[-1]);self.assertEqual(artifact['source']['environment'],'preview')
        self.assertEqual(artifact['counts']['item_count'],len(before['blind_manifest']['items']))
        self.assertEqual(artifact['counts']['choice_count'],sum(len(i['choices']) for i in before['blind_manifest']['items']))
        self.assertEqual(artifact['human_cohort']['included'],False);self.assertIsNone(artifact['human_cohort']['denominator'])
        self.assertEqual(c.round_record,before);self.assertEqual(first['status'],'published-historical-preview')
        self.assertNotIn('user_id',json.dumps(artifact));self.assertNotIn('supabase://',json.dumps(artifact))
        again=materialize_historical_publication(c.round_record['round_id'],coordinator=c,public_coordinator=object())
        self.assertEqual(first,again)
    def test_descriptor_wrong_source_fails(self):
        c=HistoricalCoordinator();materialize(c);c.evaluation['round_opens_at']='2026-08-15T00:00:00Z'
        with self.assertRaises(HistoricalPreviewError):build_historical_publication(c.round_record,c.scope,c.evaluation,c.stored_contents[0],[])
    def test_planner_exact_historical_scope_never_promotes_or_reveals(self):
        row=round_row('preview-exact',environment='preview',private_index_sha256='b'*64,featured_registered=False,historical_evaluation_ready=False)
        row['historical_scope']={k:row[k] for k in SCOPE_FIELDS};row['historical_scope']['scope_id']='scope1'
        result=plan(snapshot(row));self.assertIn('evaluate_historical_preview',kinds(result))
        self.assertFalse({'promote','reveal','freeze_featured','activate_successor'}.intersection(kinds(result)))
        row['historical_evaluation_ready']=True
        self.assertIn('publish_historical_preview',kinds(plan(snapshot(row))))
        row['private_index_sha256']='c'*64
        result=plan(snapshot(row));self.assertNotIn('publish_historical_preview',kinds(result))
        self.assertIn('historical-source-binding-changed',[b['reason'] for b in result['blocked']])

class HistoricalScienceTests(unittest.TestCase):
    def test_audited_unscorable_full_population_and_no_none_reward(self):
        from test_reference_disposition import disposition
        from foldarium_pipeline.evaluation import UnscorableReferenceError
        def unscorable(*a,**k):raise UnscorableReferenceError(disposition())
        c=HistoricalCoordinator();materialize(c,unscorable)
        item_id=c.round_record['blind_manifest']['items'][0]['id']
        execution={'execution_id':'test-execution','provider':'cursor','model':{'requested_id':'exact'},'provenance':{'config_sha256':'a'*64},'payload':{'items':[{'item_id':item_id,'clustered':{'selection_kind':'none'},'unclustered':{'selection_kind':'none'}}]}}
        receipt={'execution_sha256':'b'*64,'payload_digest':'c'*64}
        public=json.loads(build_historical_publication(c.round_record,c.scope,c.evaluation,c.stored_contents[0],[(execution,receipt)]))
        self.assertEqual(public['counts'],dict(item_count=1,choice_count=2,scorable_item_count=0,excluded_item_count=1))
        self.assertIsNone(public['benchmarks'][0]['decisions'][0]['unclustered_correct'])
        self.assertIsNone(public['benchmarks'][0]['decisions'][0]['clustered_correct'])
        self.assertEqual(len(public['items'][0]['choices']),2)
        self.assertEqual(c.evaluation['excluded_item_count'],1)
        for choice in public['items'][0]['choices']:self.assertIsNone(choice['rmsd'])
    def test_exact_track_uses_raw_score_while_cluster_track_uses_accepted_score(self):
        c=HistoricalCoordinator();rmsds=iter([.8,2.0])
        materialize(c,lambda *a,**k:{**overlay_evaluation_score(),'rmsd':next(rmsds)})
        first=json.loads(build_historical_publication(c.round_record,c.scope,c.evaluation,c.stored_contents[0],[]))
        item=first['items'][0];wrong=next(choice for choice in item['choices'] if not choice['correct'])
        self.assertTrue(wrong['accepted_correct'])
        execution={'execution_id':'test-execution','provider':'cursor','model':{'requested_id':'exact'},'provenance':{'config_sha256':'a'*64},'payload':{'items':[{'item_id':item['id'],'clustered':{'selection_kind':'cluster','cluster_id':wrong['cluster_id']},'unclustered':{'selection_kind':'exact','choice_id':wrong['id']}}]}}
        result=json.loads(build_historical_publication(c.round_record,c.scope,c.evaluation,c.stored_contents[0],[(execution,{'execution_sha256':'b'*64,'payload_digest':'c'*64})]))
        decision=result['benchmarks'][0]['decisions'][0]
        self.assertFalse(decision['unclustered_correct']);self.assertTrue(decision['clustered_correct'])
    def test_registered_execution_requires_exact_immutable_receipt(self):
        from foldarium_pipeline.historical_preview import _validated_executions
        from test_weekly_reconciliation_artifacts import BenchmarkArtifactTests
        from foldarium_pipeline.weekly_llm_contract import digest_post_close_benchmark,sha256_hex,build_blindness_attestation
        from test_weekly_llm_score import _benchmark_execution,_build_kit_zip
        c,row,_=BenchmarkArtifactTests().fixture()
        _,kit=_build_kit_zip();execution=_benchmark_execution(attestation=build_blindness_attestation())
        job=row['benchmark_jobs'][0]
        row['automation_policy']={'environment':'preview','blind_manifest_sha256':row['blind_manifest_sha256'],'expected_execution_ids':[job['execution_id']]}
        job['receipt']={'execution_id':job['execution_id'],'execution_sha256':digest_post_close_benchmark(execution,kit=kit),'payload_digest':sha256_hex(execution['payload'])}
        self.assertEqual(len(_validated_executions(c,c,row)),1)
        job['receipt']['execution_sha256']='f'*64
        with self.assertRaisesRegex(HistoricalPreviewError,'artifact differs'):_validated_executions(c,c,row)
        job['receipt']['execution_sha256']=digest_post_close_benchmark(execution,kit=kit)
        job['model_id']='wrong'
        with self.assertRaisesRegex(HistoricalPreviewError,'artifact differs'):_validated_executions(c,c,row)

def database_fixture(*, unscorable=False):
    c=HistoricalCoordinator()
    evaluator=None
    if unscorable:
        from test_reference_disposition import disposition
        from foldarium_pipeline.evaluation import UnscorableReferenceError
        def evaluator(*a,**k):raise UnscorableReferenceError(disposition())
    materialize(c,evaluator)
    return {'round':c.round_record,'scope':c.scope,'descriptor':c.evaluation,
        'public':json.loads(build_historical_publication(c.round_record,c.scope,c.evaluation,c.stored_contents[0],[]))}

if __name__=='__main__':
    import sys
    if len(sys.argv)>2 and sys.argv[1]=='--write-pg-fixture':
        Path(sys.argv[2]).write_text(json.dumps(database_fixture(unscorable='--unscorable' in sys.argv)))
    else:unittest.main()
