from collections import Counter
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ghbook
from recovery_api import Conflict, Handler, Manager


def sample_books(count=123):
    return [{"id": str(i), "title": f"Book {i}", "author": "Test author", "lang": "fa",
             "file_name": f"{i}-fa-test.ghm", "file_size": "400", "price": "0", "version": "1"}
            for i in range(1, count + 1)]


def sample_download(book_id, output, book, resume=True, cancel=None):
    if cancel and cancel():
        raise InterruptedError("Job paused")
    target = Path(output) / book["file_name"]
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("bookinfo.xml", f"<bookinfo><title>کتاب {book_id}</title><booknumber>{book_id}</booknumber><language>FA</language></bookinfo>")
        archive.writestr("out.xml", "#عنوان\nمتن کتاب\n")
    receipt = {"id": book_id, "path": str(target), "bytes": target.stat().st_size,
               "mtime_ns": target.stat().st_mtime_ns, "sha256": ghbook.file_hash(target), "skipped": False}
    ghbook.save_json(target.with_suffix(".ghm.json"), receipt)
    return receipt


class GuidedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.books = sample_books()
        self.manager = Manager(self.root, list(reversed(self.books)))

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def restart(self):
        self.manager.close()
        self.manager = Manager(self.root, list(reversed(self.books)))

    def confirm(self, kind, retry=False):
        plan = self.manager.guided.plan(kind, retry)
        job = self.manager.guided.start(plan["token"])
        self.manager.wait()
        return plan, self.manager.status(job["id"])

    def test_consecutive_downloads_are_50_50_23_and_each_needs_confirmation(self):
        with patch.object(ghbook, "download", side_effect=sample_download):
            first = self.manager.guided.plan("download")
            self.assertEqual(first["selection"]["ids"], list(range(1, 51)))
            self.assertEqual(self.manager.jobs(), [])
            job = self.manager.guided.start(first["token"])
            self.manager.wait()
            self.assertEqual(self.manager.status(job["id"])["counts"]["done"], 50)
            self.assertEqual(len(self.manager.jobs()), 1)
            with self.assertRaises(Conflict):
                self.manager.guided.start(first["token"])
            second = self.manager.guided.plan("download")
            self.assertEqual(second["selection"]["ids"], list(range(51, 101)))
            self.restart()  # A confirmed preview can also survive a service restart.
            self.manager.guided.start(second["token"])
            self.manager.wait()
            last, job = self.confirm("download")
            self.assertEqual(last["books"], 23)
            self.assertEqual(job["state"], "completed")
            self.assertEqual(len(self.manager.jobs()), 3)
            self.assertEqual(self.manager.guided.status()["queues"]["download"]["completed"], 123)
            with self.assertRaises(ValueError):
                self.manager.guided.plan("download")
            (self.manager.downloads / self.books[4]["file_name"]).unlink()
            self.assertEqual(self.manager.guided.plan("download")["selection"]["ids"], [5])

    def test_download_failures_do_not_block_later_batches_and_retry_is_explicit(self):
        def download(book_id, *args, **kwargs):
            if book_id == 1:
                raise ValueError("Server refused this book")
            return sample_download(book_id, *args, **kwargs)
        with patch.object(ghbook, "download", side_effect=download):
            _, job = self.confirm("download")
        self.assertEqual(job["state"], "completed_with_errors")
        self.restart()
        queue = self.manager.guided.status()["queues"]["download"]
        self.assertEqual(queue["needs_attention"], 1)
        self.assertEqual(queue["completed"], 49)
        self.assertEqual(self.manager.guided.plan("download")["selection"]["ids"], list(range(51, 101)))
        with patch.object(ghbook, "download", side_effect=sample_download):
            retry, job = self.confirm("download", retry=True)
        self.assertEqual(retry["selection"]["ids"], [1])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(self.manager.guided.status()["queues"]["download"]["needs_attention"], 0)

    def test_conversion_batches_only_use_saved_unconverted_books_and_detect_repairs(self):
        for book in self.books[:75]:
            sample_download(int(book["id"]), self.manager.downloads, book)
        source = self.manager.downloads / self.books[0]["file_name"]
        source.write_bytes(b"broken book")
        plan, job = self.confirm("convert")
        self.assertEqual(plan["books"], 50)
        self.assertEqual(job["counts"]["done"], 49)
        self.assertEqual(job["counts"]["failed"], 1)
        self.restart()
        plan, job = self.confirm("convert")
        self.assertEqual(plan["selection"]["ids"], list(range(51, 76)))
        self.assertEqual(job["counts"]["done"], 25)
        queue = self.manager.guided.status()["queues"]["convert"]
        self.assertEqual(queue["completed"], 74)
        self.assertEqual(queue["waiting_download"], 48)
        self.assertEqual(queue["needs_attention"], 1)
        self.assertEqual(queue["next_count"], 0)
        sample_download(1, self.manager.downloads, self.books[0])
        plan, job = self.confirm("convert")
        self.assertEqual(plan["selection"]["ids"], [1])
        self.assertEqual(job["state"], "completed")

    def test_pause_restart_resumes_original_batch_without_repeating_completed_books(self):
        started = threading.Event()
        calls = Counter()
        def download(book_id, *args, **kwargs):
            calls[book_id] += 1
            if book_id == 2 and calls[book_id] == 1:
                started.set()
                if not self.manager.cancel.wait(5):
                    raise RuntimeError("Test failed to pause")
                raise InterruptedError("Paused")
            return sample_download(book_id, *args, **kwargs)
        with patch.object(ghbook, "download", side_effect=download):
            plan = self.manager.guided.plan("download")
            job = self.manager.guided.start(plan["token"])
            self.assertTrue(started.wait(5))
            self.manager.pause(job["id"])
            self.manager.wait()
            self.restart()
            queue = self.manager.guided.status()["queues"]["download"]
            self.assertEqual(queue["reserved"], 49)
            self.assertEqual(queue["resume_job"]["id"], job["id"])
            with self.assertRaises(ValueError):
                self.manager.guided.plan("download")
            self.manager.resume(job["id"])
            self.manager.wait()
        self.assertEqual(calls[1], 1)
        self.assertEqual(calls[2], 2)
        self.assertEqual(self.manager.guided.plan("download")["selection"]["ids"], list(range(51, 101)))

    def test_stale_expired_and_duplicate_previews_cannot_start_a_different_batch(self):
        plan = self.manager.guided.plan("download")
        sample_download(1, self.manager.downloads, self.books[0])
        with self.assertRaises(Conflict):
            self.manager.guided.start(plan["token"])
        self.assertEqual(self.manager.jobs(), [])
        plan = self.manager.guided.plan("download")
        with self.manager.db() as db:
            db.execute("UPDATE batch_previews SET created='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(Conflict):
            self.manager.guided.start(plan["token"])
        self.assertEqual(self.manager.jobs(), [])
        plan = self.manager.guided.plan("download")
        with patch.object(ghbook, "download", side_effect=sample_download):
            self.manager.guided.start(plan["token"])
            with self.assertRaises(Conflict):
                self.manager.guided.start(plan["token"])
            self.manager.wait()
        self.assertEqual(len(self.manager.jobs()), 1)

    def test_manual_files_and_unfinished_manual_jobs_are_respected(self):
        sample_download(9, self.manager.downloads, self.books[8])
        with self.manager.db() as db:
            db.execute("INSERT INTO jobs(id,state,kind,options,created,updated) VALUES ('manual','paused','download','{}','today','today')")
            db.execute("INSERT INTO items(job_id,book_id,status,book) VALUES ('manual',1,'pending',?)", (json.dumps(self.books[0]),))
        plan = self.manager.guided.plan("download")
        self.assertNotIn(1, plan["selection"]["ids"])
        self.assertNotIn(9, plan["selection"]["ids"])
        self.assertEqual(plan["books"], 50)
        queue = self.manager.guided.status()["queues"]["download"]
        self.assertEqual(queue["completed"], 1)
        self.assertEqual(queue["reserved"], 1)
        self.assertIsNone(queue["resume_job"])

    def test_guided_http_confirmation_and_validation(self):
        class QuietHandler(Handler):
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.manager = self.manager
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                                         data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as response:
                return json.load(response)
        try:
            self.assertEqual(request('/guided')["queues"]["download"]["next_count"], 50)
            for body in ({"kind": "check"}, {"kind": "download", "retry": "true"}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    request('/guided/plan', body)
                self.assertEqual(error.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as error:
                request('/guided/start', {"token": "unreviewed"})
            self.assertEqual(error.exception.code, 409)
            plan = request('/guided/plan', {"kind": "download"})
            self.assertEqual(self.manager.jobs(), [])
            with patch.object(ghbook, "download", side_effect=sample_download):
                job = request('/guided/start', {"token": plan["token"]})
                self.manager.wait()
            self.assertEqual(job["total"], 50)
            self.assertTrue(job["options"]["guided"])
            self.assertEqual(request('/guided')["queues"]["download"]["next_count"], 50)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
