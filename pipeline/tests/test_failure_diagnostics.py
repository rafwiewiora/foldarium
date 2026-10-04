import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from foldarium_pipeline import failure_diagnostics as d
from foldarium_pipeline.supabase import SupabasePublisher, SupabasePublicationError
from test_contracts import make_task
from test_supabase import FakeResponse


class FailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'logs').mkdir(); (self.root / 'output').mkdir()
        self.task = make_task('boltz2', {'msa_mode':'empty','seed':0})
        self.result = {'status':'failed','error_code':'output_validation_failed','task_id':self.task['task_id']}
        self.claim = {'run_id':self.task['task_id'],'attempt_count':2,'lease_owner':'worker', 'task_sha256':'a'*64}

    def build(self, **kwargs):
        return d.build_failure_diagnostics(self.task,self.result,self.root,self.claim,**kwargs)

    def test_alternate_native_names_preserved_exactly_and_logs_redacted(self):
        native=b'data_native\nATOM 1 2 3\n'
        (self.root/'output'/'alternate_name.cif').write_bytes(native)
        (self.root/'output'/'confidence_model.json').write_text('{"confidence":0.6}')
        (self.root/'output'/'checkpoint.pt').write_bytes(b'not allowed')
        (self.root/'output'/'input.json').write_text('{"secret":"excluded"}')
        (self.root/'logs'/'stderr.log').write_text('Traceback\nSECRET-VALUE-123\nBearer hidden-token\nhttps://store/x?signature=topsecret\nValueError: bad input\n')
        descriptor,files=self.build(secrets=['SECRET-VALUE-123'])
        self.assertEqual(next(body for r,body in files if r['relative_path'].endswith('.cif')),native)
        body=b'\n'.join(body for _,body in files)
        for secret in (b'SECRET-VALUE-123',b'hidden-token',b'topsecret',b'not allowed',b'excluded'):
            self.assertNotIn(secret,body)
        self.assertIn(b'ValueError: bad input',body)
        self.assertEqual(descriptor['attempt_count'],2)
        self.assertEqual(descriptor['effective_task_sha256'],hashlib.sha256(d.canonical_bytes(self.task)).hexdigest())
        self.assertEqual(len(files),3)

    def test_jwt_basic_auth_and_standalone_query_tokens_redacted(self):
        text='Authorization: Basic encoded-private\n?token=query-private\n'+ 'eyJhbGciOiJub25lIn0.eyJzdWIiOiJhIn0.signature'
        clean=d.redact(text)
        for value in ('encoded-private','query-private','eyJhbGci'):
            self.assertNotIn(value,clean)

    def test_only_credential_named_env_values_enter_redaction(self):
        values=d.credential_values({'TOKENIZERS_PARALLELISM':'false','GITHUB_TOKEN':'synthetic-gh','SUPABASE_SERVICE_ROLE_KEY':'synthetic-sb','AWS_SECRET_ACCESS_KEY':'synthetic-aws','MODAL_TOKEN_ID':'synthetic-modal','KEYBOARD_LAYOUT':'us'})
        self.assertEqual(set(values),{'synthetic-gh','synthetic-sb','synthetic-aws','synthetic-modal'})
        self.assertNotIn('false',values)

    def test_escaped_credentials_are_redacted_and_native_files_omitted(self):
        examples = [
            r'https:\/\/storage.example\/download\/SYNTHETIC-SIGNED-PATH',
            r'abc\/SYNTHETIC-CREDENTIAL',
            'password="synthetic multiple word secret"',
            'password="synthetic multiple word secret',
            "password='synthetic multiple word secret",
            '{"password":"synthetic multiple word secret',
            'password="synthetic multiple word secret' + chr(92),
            "password='synthetic multiple word secret" + chr(92),
            'password="synthetic multiple word secret\rnext diagnostic line',
            'Basic U1lOVEhFVElDOnNlY3JldA==',
            r'password=\"synthetic multiple word secret\"',
            r'\u0061\u0062\u0063/SYNTHETIC-CREDENTIAL',
        ]
        for index, value in enumerate(examples):
            (self.root/'output'/f'confidence_{index}.json').write_text(value)
        (self.root/'logs'/'stderr.log').write_text('\n'.join(examples))
        descriptor,files=self.build(secrets=['abc/SYNTHETIC-CREDENTIAL'])
        self.assertEqual(len(files),1)
        body=files[0][1].decode()
        for value in ('SYNTHETIC-SIGNED-PATH','SYNTHETIC-CREDENTIAL','multiple word secret','U1lOVEhFVElDOnNlY3JldA=='):
            self.assertNotIn(value,body)
        self.assertEqual(sum(r['status']=='sensitive_native_omitted' for r in descriptor['files']),len(examples))

    def test_discovery_counts_irrelevant_entries_without_reading_them(self):
        for i in range(100): (self.root/'output'/f'irrelevant_{i}.txt').write_text('no')
        with patch.object(d,'MAX_INVENTORY',7),patch.object(d,'_read_regular') as read:
            descriptor,files=self.build()
        self.assertEqual(descriptor['scanned_entries'],7)
        self.assertTrue(descriptor['inventory_limited'])
        self.assertEqual(files,[]);read.assert_not_called()

    def test_skipped_output_symlinks_are_not_complete_native_free_evidence(self):
        outside=self.root/'outside';outside.mkdir();(outside/'model.cif').write_text('native')
        (self.root/'output'/'unknown_directory_name').symlink_to(outside,target_is_directory=True)
        descriptor,files=self.build()
        self.assertTrue(descriptor['inventory_limited']);self.assertEqual(files,[])
        (self.root/'output'/'unknown_directory_name').unlink();(self.root/'output').rmdir()
        (self.root/'output').symlink_to(outside,target_is_directory=True)
        descriptor,files=self.build()
        self.assertTrue(descriptor['inventory_limited']);self.assertEqual(files,[])

    def test_caps_prevent_excess_file_reads(self):
        for i in range(6): (self.root/'output'/f'model{i}.cif').write_text('x'*20)
        real=d._read_regular
        with patch.object(d,'MAX_NATIVE_FILES',2),patch.object(d,'_read_regular',wraps=real) as read:
            self.build()
        self.assertEqual(read.call_count,2)
        with patch.object(d,'MAX_TOTAL_BYTES',20),patch.object(d,'_read_regular',wraps=real) as read:
            self.build()
        self.assertEqual(read.call_count,1)

    def test_expired_collection_deadline_stops_discovery_and_redaction(self):
        with patch.object(d.time,'monotonic',return_value=100),patch.object(d.os,'scandir') as scan:
            descriptor,files=self.build(deadline=99)
        scan.assert_not_called();self.assertTrue(descriptor['collection_deadline_reached'])
        self.assertEqual(files,[])
        with patch.object(d.time,'monotonic',side_effect=[1,2,3]):
            with self.assertRaises(TimeoutError):d.redact('large',['secret'],deadline=2)

    def test_symlink_escape_and_sensitive_native_omitted(self):
        outside=self.root/'outside.cif'; outside.write_text('private-secret-123')
        (self.root/'output'/'linked.cif').symlink_to(outside)
        (self.root/'output'/'sensitive.pdb').write_text('private-secret-123')
        (self.root/'output'/'nested').symlink_to(self.root,target_is_directory=True)
        descriptor,files=self.build(secrets=['private-secret-123'])
        self.assertEqual(files,[])
        self.assertEqual({r['status'] for r in descriptor['files']},{'symlink_omitted','sensitive_native_omitted'})

    def test_intermediate_symlink_and_hardlink_cannot_escape_reader(self):
        outside=self.root/'outside';outside.mkdir()
        secret=outside/'secret.cif';secret.write_text('private')
        (self.root/'output'/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):
            d._read_regular(self.root/'output'/'escape'/'secret.cif',100,root=self.root)
        import os
        os.link(secret,self.root/'output'/'hardlink.cif')
        descriptor,files=self.build()
        self.assertEqual(files,[])
        self.assertEqual(descriptor['files'][0]['status'],'unreadable_or_nonregular')

    def test_file_count_size_and_log_head_tail_bounds(self):
        (self.root/'logs'/'stderr.log').write_text('START\n'+('a\n'*200000)+'END\n')
        for n in range(4): (self.root/'output'/f'n{n}.cif').write_text('data_native')
        (self.root/'output'/'oversized.pdb').write_text('x'*60)
        with patch.object(d,'MAX_NATIVE_FILES',2),patch.object(d,'MAX_FILE_BYTES',50):
            descriptor,files=self.build()
        log=next(body for r,body in files if r['role']=='log')
        self.assertIn(b'START',log);self.assertIn(b'END',log);self.assertLessEqual(len(log),d.MAX_LOG_BYTES+20)
        self.assertTrue(next(r for r in descriptor['files'] if r['role']=='log')['truncated'])
        self.assertEqual(sum(r['status']=='included' and r['role']=='native_output' for r in descriptor['files']),2)
        self.assertIn('file_count_limit_omitted',{r['status'] for r in descriptor['files']})

    def test_total_byte_limit_and_nontext_native_are_explicit(self):
        (self.root/'output'/'a.cif').write_bytes(b'a'*20)
        (self.root/'output'/'b.cif').write_bytes(b'b'*20)
        (self.root/'output'/'c.cif').write_bytes(b'\xff')
        with patch.object(d,'MAX_TOTAL_BYTES',25):
            descriptor,files=self.build()
        self.assertEqual(sum(len(body) for _,body in files),20)
        self.assertEqual({r['status'] for r in descriptor['files']},{'included','total_size_limit_omitted','nontext_native_omitted'})

    def test_no_symlink_root_or_wrong_attempt_identity(self):
        self.claim['attempt_count']=True
        with self.assertRaises(ValueError):self.build()
        self.claim['attempt_count']=2
        self.claim['run_id']='other'
        with self.assertRaises(ValueError):self.build()

    def publisher(self, public=False, upload_failure=False):
        calls=[]
        def opener(request,*,timeout):
            calls.append(request)
            self.assertLessEqual(timeout,5)
            if '/rest/v1/prediction_runs?' in request.full_url:return FakeResponse(json.dumps([self.claim]).encode())
            if '/storage/v1/bucket/' in request.full_url:return FakeResponse(json.dumps({'id':'private-fixture','public':public}).encode())
            if '/rest/v1/rpc/' in request.full_url:return FakeResponse(b'{"status":"registered"}')
            if upload_failure and request.data != b'' and not request.data.startswith(b'{'):raise ValueError('SECRET-ERROR')
            return FakeResponse(b'{}')
        return SupabasePublisher('https://example.supabase.co','service-secret-123','private-fixture',opener=opener),calls

    def test_private_storage_descriptor_and_attempt_catalog_binding(self):
        (self.root/'output'/'nonstandard.cif').write_text('data_exact')
        publisher,calls=self.publisher()
        result=publisher.preserve_failure_diagnostics(self.task,self.result,self.root,'worker')
        self.assertEqual(result['status'],'preserved')
        rpc=json.loads(calls[-1].data)
        self.assertEqual(rpc['p_attempt_count'],2)
        self.assertEqual(rpc['p_registered_task_sha256'],'a'*64)
        descriptor=json.loads(calls[-2].data)
        self.assertEqual(hashlib.sha256(calls[-2].data).hexdigest(),rpc['p_descriptor_sha256'])
        self.assertTrue(descriptor['files'][0]['object_uri'].startswith('supabase://private-fixture/sha256/'))
        self.assertIsNone(publisher._diagnostic_deadline)
        self.assertTrue(all(r.get_header('X-upsert')=='false' for r in calls if '/storage/v1/object/' in r.full_url))

    def test_public_bucket_rejected_before_upload(self):
        publisher,calls=self.publisher(public=True)
        with self.assertRaisesRegex(SupabasePublicationError,'private storage'):publisher.preserve_failure_diagnostics(self.task,self.result,self.root,'worker')
        self.assertEqual(len(calls),2)

    def test_partial_upload_failure_keeps_sanitized_descriptor(self):
        (self.root/'output'/'nonstandard.cif').write_text('data_exact')
        publisher,calls=self.publisher(upload_failure=True)
        publisher.preserve_failure_diagnostics(self.task,self.result,self.root,'worker')
        descriptor=json.loads(calls[-2].data)
        self.assertEqual(descriptor['files'][0]['status'],'upload_failed')
        self.assertNotIn('object_uri',descriptor['files'][0])
        self.assertNotIn(b'SECRET-ERROR',calls[-2].data)

    def test_request_deadline_rejects_without_network_and_restores_finish_timeout(self):
        publisher,calls=self.publisher()
        publisher._diagnostic_deadline=0
        with self.assertRaisesRegex(SupabasePublicationError,'time limit'):
            publisher._request('/storage/v1/bucket/private-fixture',None,operation='diagnostic',method='GET')
        self.assertEqual(calls,[])
        publisher._diagnostic_deadline=None
        # The ordinary finish RPC still uses its configured timeout, not the
        # short evidence timeout, even after an archive exception.
        publisher._opener=lambda request,timeout: (self.assertEqual(timeout,60.0) or FakeResponse(b'{}'))
        publisher.publish_result(self.result,self.root/'output','worker')

    def test_diagnostic_retry_never_sleeps_across_deadline(self):
        publisher,_=self.publisher()
        publisher._opener=lambda request,timeout:FakeResponse(b'unavailable',503)
        publisher._diagnostic_deadline=100.5
        with patch('foldarium_pipeline.supabase.time.monotonic',return_value=100),patch('foldarium_pipeline.supabase.time.sleep') as sleep:
            with self.assertRaisesRegex(SupabasePublicationError,'retry deadline'):
                publisher._store_immutable_bytes(b'evidence',hashlib.sha256(b'evidence').hexdigest(),'text/plain',operation='diagnostic')
        sleep.assert_not_called()

    def test_file_upload_reserves_final_catalog_phase(self):
        (self.root/'output'/'alternate.cif').write_text('data_exact')
        publisher,_=self.publisher()
        deadlines=[]
        def store(*args,**kwargs):deadlines.append(publisher._diagnostic_deadline)
        with patch('foldarium_pipeline.supabase.time.monotonic',return_value=100),patch.object(SupabasePublisher,'_store_immutable_bytes',side_effect=store):
            publisher.preserve_failure_diagnostics(self.task,self.result,self.root,'worker')
        self.assertEqual(deadlines,[160,190])



if __name__=='__main__':unittest.main()
