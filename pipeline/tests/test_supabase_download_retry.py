import hashlib
from http.client import IncompleteRead
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublicationError
from test_supabase import FakeResponse

class ImmutableDownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.content=b'immutable exact scientific bytes';self.digest=hashlib.sha256(self.content).hexdigest()
        self.uri=f'supabase://results/sha256/{self.digest[:2]}/{self.digest}'
        self.calls=[];self.sleep=patch('foldarium_pipeline.supabase.time.sleep').start();self.addCleanup(patch.stopall)
    def client(self,operation):
        def opener(request,*,timeout):self.calls.append(request);return operation(request)
        return SupabaseCoordinator('https://example.supabase.co','TEST-SECRET','results',opener=opener)
    def test_transient_get_retries_exact_uri_and_verifies_final_bytes(self):
        for failure in [TimeoutError('TEST-SECRET'),URLError('TEST-SECRET'),ConnectionResetError('TEST-SECRET'),IncompleteRead(b'partial',10),HTTPError('x',520,'TEST-SECRET',{},io.BytesIO(b'TEST-SECRET'))]:
            with self.subTest(type=type(failure).__name__):
                self.calls.clear();self.sleep.reset_mock()
                def request(_):
                    if len(self.calls)==1:raise failure
                    return FakeResponse(self.content)
                self.assertEqual(self.client(request).download_content_object(self.uri,expected_sha256=self.digest),self.content)
                self.assertEqual(len(self.calls),2);self.assertEqual(self.calls[0].full_url,self.calls[1].full_url)
                self.assertTrue(all(r.get_method()=='GET' and r.data is None for r in self.calls));self.sleep.assert_called_once_with(1.0)
    def test_truncated_response_is_retried(self):
        class Partial(FakeResponse):
            def read(self):raise IncompleteRead(b'partial',20)
        c=self.client(lambda r:Partial(b'') if len(self.calls)==1 else FakeResponse(self.content))
        self.assertEqual(c.download_content_object(self.uri),self.content);self.assertEqual(len(self.calls),2)
    def test_permanent_status_and_wrong_digest_never_retry(self):
        for status in [400,401,403,404,413,422]:
            self.calls.clear()
            def request(r):raise HTTPError(r.full_url,status,'TEST-SECRET',{},io.BytesIO(b'TEST-SECRET'))
            with self.assertRaises(SupabasePublicationError):self.client(request).download_content_object(self.uri)
            self.assertEqual(len(self.calls),1)
        self.calls.clear()
        with self.assertRaisesRegex(SupabasePublicationError,'object digest'):self.client(lambda r:FakeResponse(b'wrong')).download_content_object(self.uri)
        self.assertEqual(len(self.calls),1);self.sleep.assert_not_called()
    def test_retry_exhaustion_bounded_sanitized_and_no_cross_uri_fallback(self):
        def request(r):raise TimeoutError('TEST-SECRET')
        with self.assertRaises(SupabasePublicationError) as caught:self.client(request).download_content_object(self.uri)
        self.assertEqual(len(self.calls),4);self.assertEqual(len({r.full_url for r in self.calls}),1)
        self.assertEqual([x.args[0] for x in self.sleep.call_args_list],[1.0,2.0,4.0]);self.assertNotIn('TEST-SECRET',str(caught.exception))
    def test_invalid_content_address_is_rejected_before_network(self):
        for uri in [self.uri.replace('results','other'),self.uri+'?credential=not-allowed','https://example.com/pose']:
            with self.assertRaises(SupabasePublicationError):self.client(lambda r:FakeResponse(self.content)).download_content_object(uri)
        with self.assertRaises(SupabasePublicationError):self.client(lambda r:FakeResponse(self.content)).download_content_object(self.uri,expected_sha256='0'*64)
        self.assertEqual(self.calls,[])
