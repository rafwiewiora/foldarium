from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from foldarium_pipeline.weekly_llm_contract import build_blindness_attestation, digest_post_close_benchmark, sha256_hex
from foldarium_pipeline.weekly_reconciliation_store import submit_expected_benchmark, reveal_evaluated_round
from test_weekly_llm_score import _benchmark_execution, _build_kit_zip


class BenchmarkArtifactTests(unittest.TestCase):
    def fixture(self):
        zipped, kit = _build_kit_zip()
        execution = _benchmark_execution(attestation=build_blindness_attestation())
        artifact=(json.dumps(execution,sort_keys=True,separators=(',', ':'))+'\n').encode()
        digest=hashlib.sha256(artifact).hexdigest()
        job={'execution_id':execution['execution_id'],'driver':execution['provider'],'model_id':execution['model']['requested_id'],'config_sha256':execution['provenance']['config_sha256'],'artifact_uri':'supabase://private/'+digest,'artifact_sha256':digest}
        row={'round_id':kit['round_id'],'environment':kit['environment'],'blind_manifest_sha256':kit['blind_manifest_sha256'],'kit':{'kit_sha256':kit['kit_sha256'],'storage_path':'public/kit.zip'},'benchmark_jobs':[job]}
        params={k:row[k] for k in ('round_id','environment','blind_manifest_sha256')}
        params.update(execution_id=execution['execution_id'],artifact_sha256=digest)
        class Coordinator:
            receipt_override=None
            def __init__(self): self.calls=[]
            def weekly_quiz_round(self,rid): return deepcopy(row)
            def download_content_object(self,uri,expected_sha256=None):
                if uri=='supabase://public/kit.zip': return zipped
                if expected_sha256!=hashlib.sha256(artifact).hexdigest(): raise ValueError('bad artifact digest')
                return artifact
            def _rpc(self,name,payload):
                self.calls.append((name,payload))
                if name=='weekly_automation_snapshot_v1': return {'rounds':[deepcopy(row)]}
                if name=='verify_weekly_automation_benchmark_v1': return {'verified':True}
                if name=='register_weekly_selector_benchmark_v1':
                    return [self.receipt_override or {'execution_id':execution['execution_id'],'round_id':row['round_id'],'environment':row['environment'],'execution_sha256':digest_post_close_benchmark(execution,kit=kit),'payload_digest':sha256_hex(execution['payload'])}]
                raise AssertionError(name)
        return Coordinator(),row,params

    def test_real_kit_and_envelope_validation_precedes_ingest_and_receipt_verification(self):
        coordinator,row,params=self.fixture()
        receipt=submit_expected_benchmark(coordinator,coordinator,params)
        self.assertEqual(receipt['execution_id'],params['execution_id'])
        self.assertEqual([name for name,_ in coordinator.calls],['weekly_automation_snapshot_v1','register_weekly_selector_benchmark_v1','verify_weekly_automation_benchmark_v1'])
        self.assertEqual(coordinator.calls[-1][1]['p_artifact_sha256'],params['artifact_sha256'])

    def test_frozen_model_mismatch_prevents_any_ingest(self):
        coordinator,row,params=self.fixture();row['benchmark_jobs'][0]['model_id']='wrong'
        with self.assertRaisesRegex(ValueError,'frozen expectation'):
            submit_expected_benchmark(coordinator,coordinator,params)
        self.assertEqual([name for name,_ in coordinator.calls],['weekly_automation_snapshot_v1'])

    def test_wrong_receipt_never_marks_artifact_verified(self):
        coordinator,row,params=self.fixture();coordinator.receipt_override={'execution_id':'wrong'}
        with self.assertRaisesRegex(ValueError,'receipt mismatch'):
            submit_expected_benchmark(coordinator,coordinator,params)
        self.assertNotIn('verify_weekly_automation_benchmark_v1',[name for name,_ in coordinator.calls])

    def test_environment_rebinding_rejected_before_artifact_download(self):
        coordinator,row,params=self.fixture();params['environment']='production'
        with self.assertRaisesRegex(ValueError,'environment binding changed'):
            submit_expected_benchmark(coordinator,coordinator,params)
        self.assertEqual(coordinator.calls,[])

    def test_missing_frozen_policy_prevents_reveal_before_reading_evaluation(self):
        coordinator,row,params=self.fixture()
        with self.assertRaisesRegex(ValueError,'required benchmark receipts'):
            reveal_evaluated_round(coordinator,params)
        self.assertEqual([name for name,_ in coordinator.calls],['weekly_automation_snapshot_v1'])
