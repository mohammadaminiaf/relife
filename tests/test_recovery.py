import email.message
from http.server import ThreadingHTTPServer
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
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ghbook
import ghm_epub
from recovery_api import Manager, Handler, Conflict


class Response(io.BytesIO):
    def __init__(self, data, status, **headers):
        super().__init__(data)
        self.status = status
        self.headers = email.message.Message()
        for key, value in headers.items():
            self.headers[key.replace("_", "-")] = value


class TransferTests(unittest.TestCase):
    def test_ranges_and_streaming_preserve_binary_bytes(self):
        content = bytes(range(256)) * 2400

        def response(url, headers):
            start, end = map(int, headers["Range"][6:].split("-"))
            end = min(end, len(content) - 1)
            return Response(content[start:end + 1], 206, Content_Type="application/octet-stream",
                            Content_Range=f"bytes {start}-{end}/{len(content)}")

        with patch.object(ghbook, "request", response):
            output = io.BytesIO()
            size, _ = ghbook.transfer("example", output, book=True, mode="ranges")
            self.assertEqual(size, len(content))
            self.assertEqual(output.getvalue(), content)
        with patch.object(ghbook, "request", return_value=Response(content, 200, Content_Length=str(len(content)))):
            output = io.BytesIO()
            ghbook.transfer("example", output)
            self.assertEqual(output.getvalue(), content)

    def test_invalid_payloads_are_rejected(self):
        for response in [Response(b"error", 200, Content_Type="text/html"),
                         Response(b"x", 206, Content_Range="bytes 0-131071/262144")]:
            with self.subTest(response=response), patch.object(ghbook, "request", return_value=response):
                with self.assertRaises(ValueError):
                    ghbook.transfer("example", io.BytesIO(), book=True)

    def test_resume_checks_hash_and_never_overwrites_corruption(self):
        book = {"id": "1", "title": "One", "file_name": "1-fa-book.ghm", "file_size": "4"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / book["file_name"]
            path.write_bytes(b"good")
            ghbook.save_json(path.with_suffix(".ghm.json"), {
                "id": 1, "bytes": 4, "sha256": ghbook.file_hash(path), "path": "/previous-laptop/book.ghm",
            })
            result = ghbook.download(1, Path(directory), book=book, resume=True)
            self.assertTrue(result["skipped"])
            self.assertEqual(result["path"], str(path.resolve()))
            path.write_bytes(b"evil")
            with self.assertRaises(FileExistsError):
                ghbook.download(1, Path(directory), book=book, resume=True)
            self.assertEqual(path.read_bytes(), b"evil")

    def test_catalog_quoted_fields_and_validation(self):
        row = '1,"A, title",Author,1,2,3,2026,1-fa-book.ghm,pic.png,0,4,0,1,1,publisher,2\n'
        book = ghbook.parse_catalog(row)[0]
        self.assertEqual(book["title"], "A, title")
        self.assertEqual(book["lang"], "fa")
        with self.assertRaises(ValueError):
            ghbook.parse_catalog("broken,row\n")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.books = [{"id": str(i), "title": str(i), "file_name": f"{i}-fa-book.ghm",
                       "file_size": "4", "lang": "fa", "price": "0"} for i in range(1, 4)]
        self.manager = Manager(self.root, self.books)
        self.manager.configure({"download_workers": 1})

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def result(self, book_id, *args, **kwargs):
        return {"id": book_id, "path": str(self.root / f"{book_id}.ghm"), "skipped": False}

    def test_bulk_continues_after_failure_and_resume_only_retries_failures(self):
        calls = []

        def download(book_id, *args, **kwargs):
            calls.append(book_id)
            if book_id == 2:
                raise ValueError("Unavailable book")
            return self.result(book_id)

        with patch.object(ghbook, "download", download):
            job = self.manager.create({"scope": "all"})
            self.manager.wait()
        status = self.manager.status(job["id"])
        self.assertEqual(status["state"], "completed_with_errors")
        self.assertEqual(status["counts"]["done"], 2)
        self.assertEqual(calls, [1, 2, 3])
        with patch.object(ghbook, "download", side_effect=self.result) as download:
            self.manager.resume(job["id"])
            self.manager.wait()
            self.assertEqual(download.call_count, 1)
            self.assertEqual(download.call_args.args[0], 2)
        self.assertEqual(self.manager.status(job["id"])["counts"]["done"], 3)
        self.manager.close()
        self.manager = Manager(self.root, self.books)
        self.assertEqual(self.manager.status(job["id"])["state"], "completed")

    def test_pause_keeps_interrupted_book_pending(self):
        entered = threading.Event()
        released = threading.Event()

        def download(book_id, *args, **kwargs):
            entered.set()
            released.wait(3)
            if kwargs["cancel"]():
                raise InterruptedError("Paused")
            return self.result(book_id)

        with patch.object(ghbook, "download", download):
            job = self.manager.create({"scope": "all"})
            self.assertTrue(entered.wait(3))
            self.manager.pause(job["id"])
            released.set()
            self.manager.wait()
        status = self.manager.status(job["id"])
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["counts"]["pending"], 3)
        with patch.object(ghbook, "download", side_effect=self.result):
            self.manager.resume(job["id"])
            self.manager.wait()
        self.assertEqual(self.manager.status(job["id"])["counts"]["done"], 3)

    def test_single_service_owner_and_interrupted_job_recovery(self):
        with self.assertRaises(Conflict):
            Manager(self.root, self.books)
        with patch.object(ghbook, "download", side_effect=self.result):
            job = self.manager.create({"scope": "all"})
            self.manager.wait()
        with self.manager.db() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            db.execute("UPDATE items SET status='active' WHERE job_id=? AND book_id=2", (job["id"],))
        self.manager.close()
        self.manager = Manager(self.root, self.books)
        status = self.manager.status(job["id"])
        self.assertEqual(status["state"], "interrupted")
        self.assertEqual(status["counts"]["pending"], 1)

    def test_convert_all_selects_only_available_downloads(self):
        folder = self.root / "downloads/bundled"
        folder.mkdir(parents=True)
        (folder / "2-fa-book.ghm").write_bytes(b"book")
        self.assertEqual([b["id"] for b in self.manager.selected({"scope": "all"}, "convert")], ["2"])

    def test_http_listing_validation_and_job_creation(self):
        class QuietHandler(Handler):
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        server.manager = self.manager
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/books?page_size=all") as response:
                self.assertEqual(len(json.load(response)["books"]), 3)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/books?page=0")
            self.assertEqual(error.exception.code, 400)
            request = urllib.request.Request(base + "/jobs", data=b'{"ids":[1]}', headers={"Content-Type": "application/json"})
            with patch.object(ghbook, "download", side_effect=self.result):
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                    job = json.load(response)
                self.manager.wait()
            self.assertEqual(self.manager.status(job["id"])["state"], "completed")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class EpubTests(unittest.TestCase):
    def test_unicode_notes_images_direction_and_idempotent_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "1-fa-test.ghm"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("bookinfo.xml", "<bookinfo><title>عنوان</title><author>نویسنده</author><booknumber>1</booknumber><language>FA</language></bookinfo>")
                archive.writestr("out.xml", '#عنوان\nمتن <meta type="ref" info="شرح مهم">[1]</meta>\n![image](resources\\p.jpg)\n![audio](resources\\missing.mp3)\n![pagebreak](صفحه 1)\n')
                archive.writestr("resources\\p.jpg", b"\xff\xd8\xfftest")
            output = root / "test.epub"
            result = ghm_epub.convert(source, output)
            ghm_epub.validate(output)
            self.assertEqual(result["missing_media"], ["resources/missing.mp3"])
            with zipfile.ZipFile(output) as epub:
                chapter = ET.fromstring(epub.read("OEBPS/chapter-0001.xhtml"))
                self.assertEqual(chapter.attrib["dir"], "rtl")
                content = "".join(chapter.itertext())
                self.assertIn("شرح مهم", content)
                self.assertIn("صفحه 1", content)
                images = chapter.findall(".//{" + ghm_epub.XHTML + "}img")
                self.assertEqual(len(images), 1)
                self.assertIn("OEBPS/" + images[0].attrib["src"], epub.namelist())
            receipt = output.with_suffix(".epub.json")
            previous = json.loads(receipt.read_text())
            previous.update({"path": "/previous-laptop/book.epub", "source": "/previous-laptop/book.ghm"})
            receipt.write_text(json.dumps(previous))
            resumed = ghm_epub.convert(source, output)
            self.assertTrue(resumed["skipped"])
            self.assertEqual(resumed["path"], str(output.resolve()))
            self.assertEqual(resumed["source"], str(source.resolve()))
            output.write_bytes(b"changed")
            with self.assertRaises(FileExistsError):
                ghm_epub.convert(source, output)


if __name__ == "__main__":
    unittest.main()
