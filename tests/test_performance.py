from collections import Counter
import datetime
from email.message import Message
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ghbook
from download_diagnostics import describe, response_failure, retry_seconds, DownloadCoordinator
from recovery_api import Manager
from test_recovery import Response
from test_guided import sample_books, sample_download


class ErrorEvidenceTests(unittest.TestCase):
    def test_publisher_removal_is_not_rate_limiting(self):
        notice = '<h3>فایل مورد نظر به درخواست ناشرو مولف محترم از سایت برداشته شده است!</h3>'
        detail = response_failure(200, {"Content-Type": "text/html"}, notice).details
        self.assertEqual(detail["category"], "publisher_removed")
        self.assertFalse(detail["retryable"])
        self.assertIn('ناشرو', detail["server_message"])
        generic = response_failure(200, {}, '<html>Unexpected page</html>').details
        self.assertEqual(generic["category"], "invalid_response")
        self.assertFalse(generic["retryable"])

    def test_retry_after_and_status_codes_have_specific_reasons(self):
        for status, category in ((429, 'rate_limit'), (403, 'access_denied'), (404, 'not_found'), (503, 'server_error')):
            with self.subTest(status=status):
                detail = response_failure(status, {"Retry-After": "17"}, 'Server message').details
                self.assertEqual(detail["category"], category)
                self.assertEqual(detail["retry_after_seconds"], 17)
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)
        self.assertGreater(retry_seconds(format_datetime(future)), 58)
        self.assertIsNone(retry_seconds('invalid'))
        self.assertEqual(describe(TimeoutError('stalled'))['category'], 'timeout')

    def test_rate_limit_cooldown_is_shared_and_cancelable(self):
        cancel = threading.Event()
        coordinator = DownloadCoordinator(8, cancel)
        coordinator.backoff(response_failure(429, {"Retry-After": "30"}).details, 0)
        self.assertEqual(coordinator.status()['effective_workers'], 4)
        self.assertGreater(coordinator.status()['cooldown_seconds'], 29)
        cancel.set()
        with self.assertRaises(InterruptedError):
            with coordinator.slot():
                self.fail('No request should start during a canceled cooldown')

    def test_server_cooldown_and_reduced_parallelism_survive_restart(self):
        coordinator = DownloadCoordinator(8, threading.Event())
        coordinator.backoff(response_failure(429, {"Retry-After": "60"}).details, 0)
        restored = DownloadCoordinator(8, threading.Event(), coordinator.status())
        self.assertEqual(restored.status()['effective_workers'], 4)
        self.assertEqual(restored.status()['rate_limit_events'], 1)
        self.assertGreater(restored.status()['cooldown_seconds'], 58)

    def test_streaming_uses_one_request_and_preserves_bytes_and_progress(self):
        content = bytes(range(256)) * 12000
        events, metrics = [], {}
        with patch.object(ghbook, 'request', return_value=Response(content, 200, Content_Type='application/octet-stream', Content_Length=str(len(content)))) as request:
            target = io.BytesIO()
            count, digest = ghbook.transfer('https://example.test/book', target, book=True, progress=events.append, metrics=metrics)
        self.assertEqual(target.getvalue(), content)
        self.assertEqual(count, len(content))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1], {})
        self.assertEqual(metrics['requests'], 1)
        self.assertEqual(metrics['mode'], 'stream')
        self.assertEqual(sum(event.get('delta', 0) for event in events), len(content))


class ConcurrentJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Manager(Path(self.temp.name), sample_books(12))

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def test_eight_books_overlap_and_each_is_claimed_once(self):
        barrier = threading.Barrier(8)
        calls, lock = Counter(), threading.Lock()
        def download(book_id, output, book, **kwargs):
            with lock:
                calls[book_id] += 1
            if book_id <= 8:
                barrier.wait(5)
            kwargs['progress']({'phase': 'downloading', 'delta': 123, 'bytes': 123})
            return sample_download(book_id, output, book)
        with patch.object(ghbook, 'download', side_effect=download):
            job = self.manager.create({'scope': 'all'})
            self.manager.wait()
        status = self.manager.status(job['id'])
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['counts']['done'], 12)
        self.assertEqual(calls, Counter(range(1, 13)))
        self.assertEqual(status['telemetry']['configured_workers'], 8)
        self.assertEqual(status['telemetry']['network_bytes'], 123 * 12)
        self.assertTrue(status['telemetry']['measured'])

    def test_rate_limited_job_retries_records_evidence_and_reduces_parallelism(self):
        calls = Counter()
        def download(book_id, output, book, **kwargs):
            calls[book_id] += 1
            if book_id == 1 and calls[book_id] == 1:
                headers = Message()
                headers['Retry-After'] = '0'
                raise urllib.error.HTTPError('https://example.test/book', 429, 'Too many requests', headers, io.BytesIO(b'Too many requests'))
            if book_id == 2:
                raise response_failure(200, {}, 'Removed at the request of the publisher')
            return sample_download(book_id, output, book)
        with patch.object(ghbook, 'download', side_effect=download):
            job = self.manager.create({'scope': 'ids', 'ids': [1, 2]})
            self.manager.wait()
        report = self.manager.report(job['id'])
        self.assertEqual(report['job']['state'], 'completed_with_errors')
        self.assertEqual(calls[1], 2)
        self.assertEqual(calls[2], 1)
        self.assertEqual(report['job']['telemetry']['effective_workers'], 4)
        reasons = {event['details']['category'] for event in report['events']}
        self.assertEqual(reasons, {'rate_limit', 'publisher_removed'})
        self.assertEqual(report['job']['failures'][0]['diagnostic']['http_status'], 200)

    def test_parallel_pause_returns_all_inflight_items_to_pending(self):
        barrier = threading.Barrier(8)
        entered = threading.Event()
        def download(book_id, *args, **kwargs):
            barrier.wait(5)
            entered.set()
            self.manager.cancel.wait(5)
            raise InterruptedError('Paused')
        with patch.object(ghbook, 'download', side_effect=download):
            job = self.manager.create({'scope': 'all'})
            self.assertTrue(entered.wait(5))
            self.manager.pause(job['id'])
            self.manager.wait()
        status = self.manager.status(job['id'])
        self.assertEqual(status['state'], 'paused')
        self.assertEqual(status['counts']['pending'], 12)
        self.assertEqual(status['counts']['active'], 0)

    def test_legacy_jobs_keep_working_and_failure_probe_is_persisted(self):
        with patch.object(ghbook, 'download', side_effect=ValueError('Server returned text/html instead of a book')):
            job = self.manager.create({'scope': 'ids', 'ids': [1]})
            self.manager.wait()
        notice = 'فایل مورد نظر به درخواست ناشرو مولف محترم از سایت برداشته شده است!'.encode()
        with patch.object(ghbook, 'request', return_value=Response(notice, 200, Content_Type='text/html')):
            checked = self.manager.diagnose(job['id'])
        self.assertEqual(checked['checked'][0]['diagnostic']['category'], 'publisher_removed')
        self.assertEqual(self.manager.status(job['id'])['failures'][0]['diagnostic']['category'], 'publisher_removed')
        self.assertEqual(self.manager.report(job['id'])['events'][-1]['attempt'], 0)


if __name__ == '__main__':
    unittest.main()
