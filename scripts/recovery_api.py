#!/usr/bin/env python3
"""Local Bruno adapter and persistent bulk recovery jobs. Python standard library only."""
from contextlib import contextmanager
import datetime
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import urllib.error
import urllib.parse
import uuid

import ghbook
import ghm_epub


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Manager:
    def __init__(self, root=None, books=None):
        self.root = Path(root or ghbook.ROOT)
        self.state_dir = self.root / "analysis/recovery"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lockfile = (self.state_dir / "service.lock").open("a+")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise Conflict("Recovery service is already running; use Bruno or stop it before starting another instance")
        self.books = books if books is not None else ghbook.catalog()
        self.by_id = {int(b["id"]): b for b in self.books}
        self.downloads = self.root / "downloads"
        self.epubs = self.root / "epubs"
        self.guard = threading.RLock()
        self.worker = None
        self.active_job = None
        self.cancel = threading.Event()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, kind TEXT NOT NULL,
                    options TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS items (
                    job_id TEXT NOT NULL, book_id INTEGER NOT NULL, status TEXT NOT NULL,
                    book TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id, book_id)
                );
                CREATE INDEX IF NOT EXISTS item_status ON items(job_id, status, book_id);
            """)
            db.execute("UPDATE jobs SET state='interrupted', updated=? WHERE state IN ('running','pausing')", (now(),))
            db.execute("UPDATE items SET status='pending' WHERE status='active'")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.state_dir / "jobs.sqlite3", timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self):
        self.cancel.set()
        self.wait()
        self.lockfile.close()

    def wait(self):
        if self.worker:
            self.worker.join()

    def selected(self, options, kind="download"):
        scope = options.get("scope", "ids")
        if scope == "all":
            if options.get("ids"):
                raise ValueError("Choose either scope=all or ids, not both")
            books = list(self.books)
        elif scope == "ids":
            ids = options.get("ids")
            if not isinstance(ids, list) or not ids:
                raise ValueError("Provide a nonempty ids array or scope=all")
            if any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in ids):
                raise ValueError("Book IDs must be positive integers")
            missing = sorted(set(ids) - self.by_id.keys())
            if missing:
                raise ValueError("IDs absent from this catalog: " + ", ".join(map(str, missing[:20])))
            books = [self.by_id[i] for i in dict.fromkeys(ids)]
        else:
            raise ValueError("scope must be all or ids")
        if kind == "convert" and scope == "all":
            available = []
            for book in books:
                try:
                    self.local_source(book)
                    available.append(book)
                except (FileNotFoundError, ValueError):
                    continue
            books = available
        lang = options.get("lang")
        if lang:
            if not isinstance(lang, str):
                raise ValueError("lang must be a language code")
            books = [b for b in books if b.get("lang") == lang]
        limit = options.get("limit")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("limit must be a positive integer")
            books = books[:limit]
        if not books:
            raise ValueError("No books match the selection")
        if not isinstance(options.get("convert_epub", False), bool):
            raise ValueError("convert_epub must be true or false")
        return books

    def plan(self, options, kind="download"):
        books = self.selected(options, kind)
        existing = 0
        missing_bytes = 0
        invalid = []
        for book in books:
            try:
                filename = ghbook.book_filename(book)
                if (self.downloads / filename).exists() or kind == "convert" and (self.downloads / "bundled" / filename).exists():
                    existing += 1
                else:
                    missing_bytes += int(book.get("file_size") or 0)
            except ValueError:
                invalid.append(int(book["id"]))
        return {"books": len(books), "estimated_ghm_bytes": sum(int(b.get("file_size") or 0) for b in books),
                "missing_ghm_bytes": missing_bytes, "existing_files_to_verify": existing,
                "disk_free_bytes": shutil.disk_usage(self.root).free,
                "epub_additional_bytes": None, "paid_entries": sum(int(b.get("price") or 0) > 0 for b in books),
                "invalid_filenames": invalid, "downloads_directory": str(self.downloads),
                "epubs_directory": str(self.epubs), "selection": options}

    def create(self, options, kind="download"):
        if kind not in {"download", "convert"}:
            raise ValueError("Invalid job kind")
        books = self.selected(options, kind)
        plan = self.plan(options, kind)
        if kind == "download" and plan["missing_ghm_bytes"] > plan["disk_free_bytes"]:
            raise ValueError("Not enough free disk space for the selected GHM files")
        with self.guard:
            if self.worker and self.worker.is_alive():
                raise Conflict("A job is already running: " + self.active_job)
            job_id = uuid.uuid4().hex
            timestamp = now()
            with self.db() as db:
                db.execute("INSERT INTO jobs(id,state,kind,options,created,updated) VALUES (?,?,?,?,?,?)",
                           (job_id, "running", kind, json.dumps(options), timestamp, timestamp))
                db.executemany("INSERT INTO items(job_id,book_id,status,book) VALUES (?,?,?,?)",
                               [(job_id, int(b["id"]), "pending", json.dumps(b, ensure_ascii=False)) for b in books])
            self.launch(job_id)
        return {**self.status(job_id), "plan": plan}

    def launch(self, job_id):
        self.cancel.clear()
        self.active_job = job_id
        self.worker = threading.Thread(target=self.run, args=(job_id,), name="ghbook-" + job_id[:8], daemon=True)
        self.worker.start()

    def pause(self, job_id):
        with self.guard:
            if self.active_job != job_id or not self.worker or not self.worker.is_alive():
                raise Conflict("This job is not running")
            self.cancel.set()
            with self.db() as db:
                db.execute("UPDATE jobs SET state='pausing',updated=? WHERE id=?", (now(), job_id))
        return self.status(job_id)

    def resume(self, job_id):
        with self.guard:
            if self.worker and self.worker.is_alive():
                raise Conflict("Wait for the current job to finish pausing")
            self.status(job_id)
            with self.db() as db:
                db.execute("UPDATE items SET status='pending',error='' WHERE job_id=? AND status IN ('active','failed')", (job_id,))
                db.execute("UPDATE jobs SET state='running',updated=?,error='' WHERE id=?", (now(), job_id))
            self.launch(job_id)
        return self.status(job_id)

    def status(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown job")
            counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM items WHERE job_id=? GROUP BY status", (job_id,))}
            active = db.execute("SELECT book_id,book FROM items WHERE job_id=? AND status='active' LIMIT 1", (job_id,)).fetchone()
            failures = [dict(r) for r in db.execute("SELECT book_id,error,attempts FROM items WHERE job_id=? AND status='failed' LIMIT 20", (job_id,))]
        result = dict(row)
        result["options"] = json.loads(result["options"])
        result["counts"] = {state: counts.get(state, 0) for state in ("pending", "active", "done", "skipped", "failed")}
        result["total"] = sum(counts.values())
        result["active_book"] = {"id": active["book_id"], "title": json.loads(active["book"])["title"]} if active else None
        result["failures"] = failures
        return result

    def jobs(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 100")]
        return [self.status(i) for i in ids]

    def items(self, job_id, status=None, page=1, page_size=100):
        self.status(job_id)
        condition = "job_id=?" + (" AND status=?" if status else "")
        parameters = [job_id] + ([status] if status else [])
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM items WHERE " + condition, parameters).fetchone()[0]
            rows = db.execute("SELECT book_id,status,attempts,result,error FROM items WHERE " + condition + " ORDER BY book_id LIMIT ? OFFSET ?",
                              parameters + [page_size, (page - 1) * page_size]).fetchall()
        values = [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]
        return {"total": total, "page": page, "page_size": page_size, "items": values}

    def local_source(self, book):
        filename = ghbook.book_filename(book)
        for path in [self.downloads / filename, self.downloads / "bundled" / filename]:
            if path.is_file():
                return path
        raise FileNotFoundError("Book is not downloaded: " + filename)

    def execute(self, book, kind, options):
        if self.cancel.is_set():
            raise InterruptedError("Job paused")
        if kind == "download":
            result = ghbook.download(int(book["id"]), self.downloads, book=book, resume=True, cancel=self.cancel.is_set)
            source = Path(result["path"])
        else:
            source = self.local_source(book)
            result = {"id": int(book["id"]), "path": str(source), "skipped": True}
        if kind == "convert" or options.get("convert_epub"):
            if self.cancel.is_set():
                raise InterruptedError("Job paused")
            converted = ghm_epub.convert(source, self.epubs / (source.stem + ".epub"))
            result["epub"] = converted
            result["skipped"] = result["skipped"] and converted["skipped"]
        return result

    def run(self, job_id):
        fatal = ""
        try:
            initial = self.status(job_id)
            while not self.cancel.is_set():
                with self.db() as db:
                    row = db.execute("SELECT book_id,book FROM items WHERE job_id=? AND status='pending' ORDER BY book_id LIMIT 1", (job_id,)).fetchone()
                if row is None:
                    break
                book_id, raw = row
                book = json.loads(raw)
                for attempt in range(3):
                    with self.db() as db:
                        db.execute("UPDATE items SET status='active',attempts=attempts+1 WHERE job_id=? AND book_id=?", (job_id, book_id))
                    try:
                        result = self.execute(book, initial["kind"], initial["options"])
                        with self.db() as db:
                            db.execute("UPDATE items SET status=?,result=?,error='' WHERE job_id=? AND book_id=?",
                                       ("skipped" if result.get("skipped") else "done", json.dumps(result, ensure_ascii=False), job_id, book_id))
                        break
                    except InterruptedError:
                        with self.db() as db:
                            db.execute("UPDATE items SET status='pending' WHERE job_id=? AND book_id=?", (job_id, book_id))
                        self.cancel.set()
                        break
                    except Exception as error:
                        transient = isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
                        if isinstance(error, urllib.error.HTTPError):
                            transient = error.code in {408, 429, 500, 502, 503, 504}
                        if transient and attempt < 2 and not self.cancel.wait(2 ** attempt):
                            continue
                        with self.db() as db:
                            db.execute("UPDATE items SET status='failed',error=? WHERE job_id=? AND book_id=?", (str(error)[:3000], job_id, book_id))
                        break
                with self.db() as db:
                    db.execute("UPDATE jobs SET updated=? WHERE id=?", (now(), job_id))
        except Exception as error:
            fatal = str(error)
        finally:
            with self.guard:
                counts = self.status(job_id)["counts"]
                state = "failed" if fatal else "paused" if self.cancel.is_set() else "completed_with_errors" if counts["failed"] else "completed"
                with self.db() as db:
                    db.execute("UPDATE jobs SET state=?,updated=?,error=? WHERE id=?", (state, now(), fatal, job_id))
                    if fatal:
                        db.execute("UPDATE items SET status='pending' WHERE job_id=? AND status='active'", (job_id,))


class Handler(BaseHTTPRequestHandler):
    server_version = "GHBookRecovery/1.0"

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        try:
            self.route(post)
        except KeyError as error:
            self.send_json(404, {"error": str(error)})
        except Conflict as error:
            self.send_json(409, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self.send_json(502, {"error": str(error)})

    def route(self, post):
        manager = self.server.manager
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path.rstrip("/")
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                self.send_json(403, {"error": "Cross-origin mutations are not accepted"})
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("POST requests require application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 1024 * 1024:
                raise ValueError("Request body is too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object")
            if path in {"/jobs", "/conversions"}:
                self.send_json(202, manager.create(body, "convert" if path == "/conversions" else "download"))
                return
            match = re.fullmatch(r"/jobs/([0-9a-f]{32})/(pause|resume)", path)
            if match:
                self.send_json(200, getattr(manager, match[2])(match[1]))
                return
            if path == "/catalog/refresh":
                with manager.guard:
                    if manager.worker and manager.worker.is_alive():
                        raise Conflict("Pause the current job before refreshing the catalog")
                    manager.books = ghbook.catalog(refresh=True)
                    manager.by_id = {int(b["id"]): b for b in manager.books}
                self.send_json(200, {"books": len(manager.books)})
                return
        else:
            if path == "/health":
                self.send_json(200, {"status": "ok", "catalog_books": len(manager.books),
                                     "active_job": manager.active_job if manager.worker and manager.worker.is_alive() else None,
                                     "downloads_directory": str(manager.downloads), "epubs_directory": str(manager.epubs)})
                return
            if path == "/books":
                books = manager.books
                if query.get("lang"):
                    books = [b for b in books if b.get("lang") == query["lang"]]
                if query.get("q"):
                    term = query["q"].casefold()
                    books = [b for b in books if any(term in str(b.get(k, "")).casefold() for k in ("id", "title", "author", "file_name"))]
                page, size = self.pagination(query, len(books))
                self.send_json(200, {"total": len(books), "page": page, "page_size": size,
                                     "books": books[(page - 1) * size:page * size]})
                return
            match = re.fullmatch(r"/books/(\d+)", path)
            if match:
                book = manager.by_id[int(match[1])]
                self.send_json(200, {**book, "download_url": ghbook.download_url({**book, "ghm_file_name": ghbook.book_filename(book)})})
                return
            if path == "/downloads/plan":
                options = {"scope": query.get("scope", "all")}
                if query.get("ids"):
                    options = {"scope": "ids", "ids": [int(i) for i in query["ids"].split(",")]}
                for key in ("lang", "limit"):
                    if query.get(key):
                        options[key] = int(query[key]) if key == "limit" else query[key]
                self.send_json(200, manager.plan(options))
                return
            if path == "/jobs":
                self.send_json(200, {"jobs": manager.jobs()})
                return
            match = re.fullmatch(r"/jobs/([0-9a-f]{32})(/items)?", path)
            if match:
                if match[2]:
                    page, size = self.pagination(query, 100, allow_all=False)
                    result = manager.items(match[1], query.get("status"), page, size)
                else:
                    result = manager.status(match[1])
                self.send_json(200, result)
                return
            match = re.fullmatch(r"/(files|epubs)/(\d+)\.(ghm|epub)", path)
            if match and (match[1], match[3]) in {("files", "ghm"), ("epubs", "epub")}:
                book = manager.by_id[int(match[2])]
                source = manager.local_source(book)
                target = source if match[3] == "ghm" else manager.epubs / (source.stem + ".epub")
                if not target.is_file():
                    raise KeyError("Requested file has not been created")
                self.send_response(200)
                self.send_header("Content-Type", "application/epub+zip" if match[3] == "epub" else "application/octet-stream")
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name))
                self.end_headers()
                with target.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile)
                return
        self.send_json(404, {"error": "Unknown endpoint"})

    def pagination(self, query, total, allow_all=True):
        page = int(query.get("page", "1"))
        raw_size = query.get("page_size", "100")
        size = max(total, 1) if allow_all and raw_size == "all" else int(raw_size)
        if page < 1 or size < 1 or not (allow_all and raw_size == "all") and size > 1000:
            raise ValueError("page must be positive; page_size must be 1..1000 or all")
        return page, size


def serve(port=8765):
    manager = Manager()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.manager = manager
        print(f"GHBook recovery API: http://127.0.0.1:{port} ({len(manager.books)} catalog entries)", flush=True)
        print("Open the bruno/ collection in Bruno and select the Local environment.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nPausing recovery before shutdown...", flush=True)
        finally:
            server.server_close()
    finally:
        manager.close()
