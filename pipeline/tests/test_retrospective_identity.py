from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from foldarium_pipeline.retrospective_identity import validate_benchmark_authorization
from foldarium_pipeline.retrospective_archive import (
    build_retrospective_source_snapshot, encode_retrospective_source_snapshot,
    build_retrospective_artifacts, RetrospectiveArchiveError,
)
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from test_retrospective_archive import (
    source_rows, ROUND_ID, BENCHMARK_ID, post_close_benchmark_row,
    benchmark_payload, evaluation_descriptor, blind_manifest, reveal_manifest, round_record,
)


def proof():
    return dict(policy='foldarium.frozen-benchmark-identity/v1', round_id=ROUND_ID,
        environment='production', execution_id=BENCHMARK_ID, provider='anthropic-api',
        model_id='fixture-model', config_sha256='c'*64, blind_manifest_sha256='a'*64,
        execution_sha256='d'*64, payload_digest='e'*64, artifact_sha256='f'*64)


def dynamic_source():
    rows=source_rows()
    rows['post_close_benchmarks']=[dict(post_close_benchmark_row(display_name='fixture-model'), benchmark_authorization=proof())]
    return build_retrospective_source_snapshot(ROUND_ID,item_count=1,**rows)


class RetrospectiveIdentityTests(unittest.TestCase):
    def test_legacy_source_bytes_unchanged(self):
        source=build_retrospective_source_snapshot(ROUND_ID,**source_rows())
        self.assertEqual(source['format_version'],'foldarium.weekly-retrospective-source/v1')
        self.assertEqual(hashlib.sha256(encode_retrospective_source_snapshot(source)).hexdigest(),
            'ef8db76158cdb16f5e7278f3450a7b5b5d8798ac2816388a6dba6bebbd6e60e1')

    def test_dynamic_identity_requires_exact_versioned_proof(self):
        source=dynamic_source()
        self.assertEqual(source['format_version'],'foldarium.weekly-retrospective-source/v2')
        for field,value in [('policy','unknown'),('provider','cursor'),('environment','preview'),
            ('round_id','other'),('execution_id','00000000-0000-4000-8000-000000000001'),
            ('model_id','Human Name'),('config_sha256','invalid'),('artifact_sha256',None)]:
            invalid=proof();invalid[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_benchmark_authorization(invalid,round_id=ROUND_ID,execution_id=BENCHMARK_ID,display_name='fixture-model')
        invalid=proof();invalid['unapproved']='anything'
        with self.assertRaises(ValueError):
            validate_benchmark_authorization(invalid,round_id=ROUND_ID,execution_id=BENCHMARK_ID,display_name='fixture-model')
        for version in ['foldarium.weekly-retrospective-source/v1','anything']:
            with self.assertRaises(RetrospectiveArchiveError):
                encode_retrospective_source_snapshot(dict(source,format_version=version))

    def test_arbitrary_display_name_and_api_legacy_label_without_proof_fail(self):
        for name,provider in [('Human Name','anthropic-api'),('Claude Opus','anthropic-api')]:
            rows=source_rows();rows['post_close_benchmarks']=[dict(post_close_benchmark_row(display_name=name),provider=provider)]
            with self.subTest(name=name),self.assertRaisesRegex(RetrospectiveArchiveError,'receipt-authorized'):
                build_retrospective_source_snapshot(ROUND_ID,item_count=1,**rows)

    def test_artifacts_project_label_but_never_private_proof(self):
        evaluation=evaluation_descriptor()
        with patch('foldarium_pipeline.retrospective_archive._verify_evaluation',return_value=(evaluation,{'blind_manifest':blind_manifest(),'reveal_manifest':reveal_manifest()})):
            public,admin,_=build_retrospective_artifacts(round_record(),evaluation,b'evaluation',dynamic_source())
            text=(public+admin).decode()
            self.assertIn('fixture-model',text)
            for forbidden in ['benchmark_authorization','execution_id','config_sha256',BENCHMARK_ID,'payload_digest','artifact_sha256']:
                self.assertNotIn(forbidden,text)
            source=dynamic_source();source['participants'][0]['benchmark_authorization']=proof()
            with self.assertRaisesRegex(RetrospectiveArchiveError,'human participant'):
                build_retrospective_artifacts(round_record(),evaluation,b'evaluation',source)

    def test_coordinator_attaches_only_service_derived_exact_receipt_proof(self):
        row=dict(post_close_benchmark_row(display_name='fixture-model'),provider='anthropic-api',requested_model_id='fixture-model',config_sha256='c'*64,execution_sha256='d'*64)
        coordinator=SupabaseCoordinator('https://fixture.invalid','fixture-key','private')
        def rpc(name,params):
            return [row] if name=='get_weekly_selector_benchmarks_v1' else [proof()]
        with patch.object(SupabaseCoordinator,'_rpc',side_effect=rpc),patch.object(SupabaseCoordinator,'_get_all_json_rows',return_value=[]):
            result=coordinator.weekly_retrospective_source_rows(ROUND_ID)
            self.assertEqual(result['post_close_benchmarks'][0]['benchmark_authorization'],proof())
            row['config_sha256']='9'*64
            with self.assertRaisesRegex(SupabasePublicationError,'exact receipt'):
                coordinator.weekly_retrospective_source_rows(ROUND_ID)

    def test_source_v2_golden_fixture(self):
        fixture=Path(__file__).resolve().parents[2]/'tests/fixtures/retrospective-source-v2-authorized.golden.json'
        self.assertEqual(json.loads(fixture.read_text()),dynamic_source())
