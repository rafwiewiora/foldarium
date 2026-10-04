"""Real HTTP boundary tests: only immutable Storage writes may repeat."""
import hashlib
from http.client import IncompleteRead
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from foldarium_pipeline.supabase import SupabaseCoordinator, SupabasePublisher, SupabasePublicationError
from test_supabase import FakeResponse, successful_result


class StorageRetryTests(unittest.TestCase):
    def setUp(self):
        self.content = b"immutable scientific artifact"
        self.calls = []
        self.sleep = patch("foldarium_pipeline.supabase.time.sleep").start()
        self.addCleanup(patch.stopall)

    def publisher(self, opener, cls=SupabaseCoordinator):
        def record(request, *, timeout):
            self.calls.append(request)
            return opener(request)
        return cls("https://example.supabase.co", "TEST-SECRET", "results", opener=record)

    def http_error(self, request, status):
        return HTTPError(request.full_url, status, "TEST-SECRET", {}, io.BytesIO(b"TEST-SECRET"))

    def test_transient_http_codes_retry_same_bytes_without_upsert(self):
        for status in (408, 429, 500, 502, 503, 504, 520, 522, 524):
            with self.subTest(status=status):
                self.calls.clear()
                self.sleep.reset_mock()
                def open_request(request):
                    if len(self.calls) == 1:
                        raise self.http_error(request, status)
                    return FakeResponse(b"stored")
                stored = self.publisher(open_request).store_bytes(self.content, "application/octet-stream", cache_control="public, max-age=10")
                self.assertEqual(stored["sha256"], hashlib.sha256(self.content).hexdigest())
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.calls[0].full_url, self.calls[1].full_url)
                for request in self.calls:
                    self.assertEqual(request.data, self.content)
                    self.assertEqual(request.get_header("X-upsert"), "false")
                    self.assertEqual(request.get_header("Cache-control"), "public, max-age=10")
                self.sleep.assert_called_once_with(1.0)

    def test_non_exception_http_failure_also_retries(self):
        publisher = self.publisher(lambda request: FakeResponse(b"unavailable", 503) if len(self.calls) == 1 else FakeResponse(b"stored"))
        publisher.store_bytes(self.content, "application/json")
        self.assertEqual(len(self.calls), 2)

    def test_connection_and_response_loss_retry_then_verify_ambiguous_success(self):
        for failure in (TimeoutError("TEST-SECRET"), URLError("TEST-SECRET"), ConnectionResetError("TEST-SECRET"), IncompleteRead(b"partial", 10)):
            with self.subTest(failure=type(failure).__name__):
                self.calls.clear()
                self.sleep.reset_mock()
                class LostResponse(FakeResponse):
                    def read(inner):
                        raise failure
                def open_request(request):
                    if len(self.calls) == 1:
                        return LostResponse(b"")  # Object stored before response disappears.
                    if len(self.calls) == 2:
                        raise self.http_error(request, 409)
                    return FakeResponse(self.content)
                publisher = self.publisher(open_request)
                publisher.store_bytes(self.content, "application/json")
                self.assertEqual([r.get_method() for r in self.calls], ["POST", "POST", "GET"])
                self.sleep.assert_called_once_with(1.0)

    def test_connect_timeout_before_response_retries(self):
        def open_request(request):
            if len(self.calls) == 1:
                raise TimeoutError("TEST-SECRET")
            return FakeResponse(b"stored")
        self.publisher(open_request).store_bytes(self.content, "application/json")
        self.assertEqual(len(self.calls), 2)

    def test_conflict_verification_transient_failure_retries_but_wrong_bytes_do_not(self):
        def open_request(request):
            if request.get_method() == "POST":
                raise self.http_error(request, 409)
            if len(self.calls) == 2:
                raise self.http_error(request, 520)
            return FakeResponse(b"wrong bytes")
        with self.assertRaisesRegex(SupabasePublicationError, "does not match"):
            self.publisher(open_request).store_bytes(self.content, "application/json")
        self.assertEqual(len(self.calls), 4)
        self.sleep.assert_called_once_with(1.0)

    def test_retry_exhaustion_is_bounded_and_sanitized(self):
        def open_request(request):
            raise self.http_error(request, 520)
        with self.assertRaises(SupabasePublicationError) as raised:
            self.publisher(open_request).store_bytes(self.content, "application/json")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.0, 2.0, 4.0])
        self.assertEqual(raised.exception.http_status, 520)
        self.assertNotIn("TEST-SECRET", str(raised.exception))

    def test_permanent_errors_and_rpc_mutations_are_never_retried(self):
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.calls.clear()
                def open_request(request):
                    raise self.http_error(request, status)
                with self.assertRaises(SupabasePublicationError):
                    self.publisher(open_request).store_bytes(self.content, "application/json")
                self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        def transient_rpc(request):
            raise self.http_error(request, 520)
        with self.assertRaises(SupabasePublicationError):
            self.publisher(transient_rpc)._rpc("finish_prediction_run", {"id": "run"})
        self.assertEqual(len(self.calls), 1)
        self.sleep.assert_not_called()

    def test_prediction_upload_retries_without_repeating_finish_rpc(self):
        post_count = 0
        def open_request(request):
            nonlocal post_count
            if "/storage/" in request.full_url:
                post_count += 1
                if post_count == 1:
                    raise self.http_error(request, 520)
                return FakeResponse(b"stored")
            return FakeResponse(b'{"status":"succeeded"}')
        publisher = self.publisher(open_request, SupabasePublisher)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pose.pdb"
            path.write_bytes(self.content)
            publisher.publish_result(successful_result("pose.pdb", self.content), path.parent, "worker-1")
        self.assertEqual(post_count, 2)
        self.assertEqual(len([r for r in self.calls if "/rest/v1/rpc/" in r.full_url]), 1)

    def test_mismatched_content_address_never_dispatches(self):
        publisher = self.publisher(lambda request: FakeResponse(b"stored"))
        with self.assertRaisesRegex(SupabasePublicationError, "content digest"):
            publisher._store_immutable_bytes(self.content, "0" * 64, "application/json", operation="test")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
