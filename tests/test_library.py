from http.server import ThreadingHTTPServer
import csv
import io
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
import ghm_epub
from recovery_api import Manager, Handler


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        categories = [
            {"id": "10", "parent_id": "-1", "title_en": "Literature", "title_fa": "ادبیات"},
            {"id": "11", "parent_id": "10", "title_en": "Poetry", "title_fa": "شعر"},
            {"id": "12", "parent_id": "-1", "title_en": "History", "title_fa": "تاریخ"},
        ]
        ghbook.save_json(self.root / "analysis/categories.json", categories)
        self.books = [
            {"id": "1", "title": "كتابِ علی", "author": "Author", "lang": "fa", "subject": "11", "subject_1": "10", "file_name": "1-fa-one.ghm", "file_size": "4", "price": "0"},
            {"id": "2", "title": "History, Volume Two", "author": "Writer", "lang": "en", "subject": "12", "file_name": "2-en-two.ghm", "file_size": "4", "price": "0"},
            {"id": "3", "title": "More Poetry", "author": "Writer", "lang": "en", "subject": "11", "file_name": "3-en-three.ghm", "file_size": "4", "price": "0"},
        ]
        self.manager = Manager(self.root, self.books)

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def original(self, book_id=1):
        book = self.books[book_id - 1]
        path = self.manager.downloads / book["file_name"]
        path.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("bookinfo.xml", f"<bookinfo><title>Test</title><author>Author</author><booknumber>{book_id}</booknumber><language>FA</language></bookinfo>")
            archive.writestr("out.xml", '#عنوان\nمتن با <meta info="شرح" type="ref">[1]</meta>\n![audio](resources\\missing.mp3)\n')
        self.manager.library.invalidate()
        return path

    def test_category_hierarchy_counts_and_normalized_name_search(self):
        rows = {c["id"]: c for c in self.manager.library.category_rows()}
        self.assertEqual(rows["10"]["book_count"], 2)  # Book 1 counts once despite two matching subjects.
        result = self.manager.selected({"scope": "all", "category": "Literature"})
        self.assertEqual([b["id"] for b in result], ["1", "3"])
        result = self.manager.selected({"scope": "all", "q": "کتاب علي"})
        self.assertEqual([b["id"] for b in result], ["1"])
        with self.assertRaises(ValueError):
            self.manager.selected({"scope": "all", "category": "Not a category"})

    def test_bounded_filtered_batches_exclusions_and_skip_downloaded(self):
        self.original()
        selection = self.manager.selected({"scope": "all", "category": "10", "exclude_ids": [1], "limit": 1})
        self.assertEqual([b["id"] for b in selection], ["3"])
        selection = self.manager.selected({"scope": "all", "skip_downloaded": True, "limit": 1})
        self.assertEqual([b["id"] for b in selection], ["2"])
        selection = self.manager.selected({"scope": "all", "offset": 1, "limit": 1})
        self.assertEqual([b["id"] for b in selection], ["2"])
        with self.assertRaises(ValueError):
            self.manager.selected({"scope": "all", "skip_checked": "true"})

    def test_compatibility_is_unknown_until_checked_and_invalidates_when_source_changes(self):
        source = self.original()
        ghbook.save_json(source.with_suffix(".ghm.json"), {"id": 1, "bytes": source.stat().st_size,
                           "mtime_ns": source.stat().st_mtime_ns, "sha256": ghbook.file_hash(source)})
        local = self.manager.library.inventory(force=True)
        self.assertEqual(local[1]["status"], "unchecked")
        self.assertIsNone(local[1]["can_convert"])
        self.assertIsNone(local[2]["can_convert"])
        job = self.manager.create({"scope": "ids", "ids": [1]}, "check")
        self.manager.wait()
        self.assertEqual(self.manager.status(job["id"])["state"], "completed")
        self.assertEqual(self.manager.library.inventory(force=True)[1]["status"], "convertible")
        job = self.manager.create({"scope": "ids", "ids": [1]}, "convert")
        self.manager.wait()
        self.assertEqual(self.manager.library.inventory(force=True)[1]["status"], "converted")
        result = self.manager.items(job["id"])["items"][0]["result"]
        ghm_epub.validate(Path(result["epub"]["path"]))
        self.assertEqual(result["epub"]["missing_media"], ["resources/missing.mp3"])
        damaged = b"x" * source.stat().st_size
        source.write_bytes(damaged)  # Same-size corruption must not reuse the old receipt's hash.
        local = self.manager.library.inventory(force=True)[1]
        self.assertEqual(local["status"], "unchecked")
        self.assertIsNone(local["can_convert"])
        job = self.manager.create({"scope": "ids", "ids": [1]}, "check")
        self.manager.wait()
        self.assertEqual(self.manager.status(job["id"])["state"], "completed_with_errors")
        local = self.manager.library.inventory(force=True)[1]
        self.assertEqual(local["status"], "unsupported")
        self.assertFalse(local["can_convert"])
        self.assertEqual(source.read_bytes(), damaged)

    def test_missing_runtime_is_not_reported_as_incompatible_format(self):
        self.original()
        with patch.object(ghm_epub, "inspect_book", side_effect=RuntimeError("Node.js is missing")):
            job = self.manager.create({"scope": "ids", "ids": [1]}, "check")
            self.manager.wait()
        local = self.manager.library.inventory(force=True)[1]
        self.assertIsNone(local["can_convert"])
        self.assertEqual(local["status"], "unchecked")
        self.assertIn("Node.js", local["error"])
        self.assertEqual(self.manager.status(job["id"])["counts"]["failed"], 1)

    def test_parse_real_category_columns_and_quoted_titles(self):
        result = ghbook.parse_categories('10,-1,"ادب, شعر",2,"الأدب",Literature &amp; Poetry\n')
        self.assertEqual(result[0]["title_en"], "Literature & Poetry")
        self.assertEqual(result[0]["title_fa"], "ادب, شعر")
        self.assertEqual(result[0]["parent_id"], "-1")

    def test_external_storage_keeps_books_and_state_together(self):
        self.manager.close()
        data = (self.root / "External drive").resolve()
        self.manager = Manager(self.root, self.books, data_dir=data)
        self.assertEqual(self.manager.downloads, data / "downloads")
        self.assertTrue((data / "analysis/recovery/jobs.sqlite3").exists())
        self.assertEqual(len(self.manager.library.category_rows()), 3)

    def test_web_api_filters_plans_exports_and_serves_app(self):
        self.original()

        class QuietHandler(Handler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.manager = self.manager
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/") as response:
                self.assertIn(b"Revive", response.read())
                self.assertIn("text/html", response.headers.get("Content-Type"))
            with urllib.request.urlopen(base + "/books?category=10&lang=en") as response:
                result = json.load(response)
                self.assertEqual([b["id"] for b in result["books"]], ["3"])
                self.assertEqual(result["books"][0]["local"]["status"], "not_downloaded")
            request = urllib.request.Request(base + "/library/plan", data=b'{"scope":"all","category":"10","limit":1,"kind":"check"}', headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                plan = json.load(response)
                self.assertEqual(plan["books"], 1)
                self.assertEqual(plan["kind"], "check")
            with urllib.request.urlopen(base + "/library/export?category=12") as response:
                rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["title"], "History, Volume Two")
            with urllib.request.urlopen(base + "/library/summary") as response:
                summary = json.load(response)
                self.assertEqual(summary["unchecked"], 1)
                self.assertEqual(summary["not_downloaded"], 2)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
