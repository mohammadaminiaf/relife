#!/usr/bin/env python3
"""Local Bruno adapter and persistent bulk recovery jobs. Python standard library only."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import csv
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
import zipfile

import ghbook
import ghm_epub
from library_index import LibraryIndex
from app_runtime import node_runtime
from guided_queue import GuidedQueue
from download_diagnostics import describe, response_failure, DownloadFailure, DownloadCoordinator
from transfer_monitor import TransferMonitor

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class Manager:
    def __init__(self, root=None, books=None, data_dir=None):
        self.root = Path(root or ghbook.ROOT)
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else self.root
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.data_dir / "analysis/recovery"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lockfile = (self.state_dir / "service.lock").open("a+")
        try:
            if os.name == "nt":
                if self.lockfile.seek(0, os.SEEK_END) == 0:
                    self.lockfile.write("\0")
                    self.lockfile.flush()
                self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise Conflict("Recovery service is already running; use Bruno or stop it before starting another instance")
        self.books = books if books is not None else ghbook.catalog()
        self.by_id = {int(b["id"]): b for b in self.books}
        self.downloads = self.data_dir / "downloads"
        self.epubs = self.data_dir / "epubs"
        self.guard = threading.RLock()
        self.worker = None
        self.active_job = None
        self.cancel = threading.Event()
        self.monitor = None
        self.coordinator = None
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
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transfer_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    book_id INTEGER NOT NULL, attempt INTEGER NOT NULL, created TEXT NOT NULL,
                    duration REAL NOT NULL, details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transfer_event_job ON transfer_events(job_id, book_id);
                CREATE TABLE IF NOT EXISTS library_state (
                    book_id INTEGER PRIMARY KEY, fingerprint TEXT, compatible INTEGER,
                    checked_at TEXT, details TEXT NOT NULL DEFAULT '{}',
                    action TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
                );
            """)
            for table, name, definition in (("jobs", "telemetry", "TEXT NOT NULL DEFAULT '{}'"),
                                             ("items", "diagnostic", "TEXT NOT NULL DEFAULT '{}'"),
                                             ("items", "duration", "REAL NOT NULL DEFAULT 0")):
                if name not in {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}:
                    db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            db.execute("UPDATE jobs SET state='interrupted', updated=? WHERE state IN ('running','pausing')", (now(),))
            db.execute("UPDATE items SET status='pending' WHERE status='active'")
        self.library = LibraryIndex(self)
        self.guided = GuidedQueue(self)

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

    def settings(self):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='download_workers'").fetchone()
        return {"download_workers": int(row[0]) if row else 8}

    def configure(self, options):
        workers = options.get("download_workers")
        if isinstance(workers, bool) or not isinstance(workers, int) or workers not in (1, 2, 4, 8):
            raise ValueError("download_workers must be 1, 2, 4 or 8")
        with self.db() as db:
            db.execute("INSERT INTO settings VALUES ('download_workers',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(workers),))
        return self.settings()

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
        excluded = options.get("exclude_ids", [])
        if not isinstance(excluded, list) or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in excluded):
            raise ValueError("exclude_ids must contain positive integer IDs")
        if excluded:
            excluded = set(excluded)
            books = [b for b in books if int(b["id"]) not in excluded]
        books = self.library.filtered(books, options)
        if kind in {"convert", "check"}:
            available = []
            for book in books:
                try:
                    self.local_source(book)
                    available.append(book)
                except (FileNotFoundError, ValueError):
                    continue
            books = available
        inventory = None
        for flag in ("skip_converted", "skip_downloaded", "skip_checked"):
            if not isinstance(options.get(flag, False), bool):
                raise ValueError(flag + " must be true or false")
            if options.get(flag):
                inventory = inventory if inventory is not None else self.library.inventory()
                field = {"skip_converted": "epub_available", "skip_downloaded": "downloaded", "skip_checked": "can_convert"}[flag]
                books = [b for b in books if (inventory[int(b["id"])].get(field) is None if flag == "skip_checked" else not inventory[int(b["id"])].get(field))]
        offset = options.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        books = books[offset:]
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
        if kind not in {"download", "convert", "check"}:
            raise ValueError("Invalid job kind")
        books = self.selected(options, kind)
        existing = 0
        missing_bytes = 0
        invalid = []
        for book in books:
            try:
                filename = ghbook.book_filename(book)
                if (self.downloads / filename).exists() or kind in {"convert", "check"} and (self.downloads / "bundled" / filename).exists():
                    existing += 1
                else:
                    missing_bytes += int(book.get("file_size") or 0)
            except ValueError:
                invalid.append(int(book["id"]))
        return {"books": len(books), "estimated_ghm_bytes": sum(int(b.get("file_size") or 0) for b in books),
                "missing_ghm_bytes": missing_bytes, "existing_files_to_verify": existing,
                "disk_free_bytes": shutil.disk_usage(self.data_dir).free,
                "epub_additional_bytes": None, "paid_entries": sum(int(b.get("price") or 0) > 0 for b in books),
                "invalid_filenames": invalid, "downloads_directory": str(self.downloads),
                "epubs_directory": str(self.epubs), "selection": options, "kind": kind,
                "sample": [{"id": int(b["id"]), "title": b["title"]} for b in books[:6]]}

    def create(self, options, kind="download"):
        options = dict(options)
        if kind == "download":
            workers = options.setdefault("download_workers", self.settings()["download_workers"])
            if isinstance(workers, bool) or not isinstance(workers, int) or workers not in (1, 2, 4, 8):
                raise ValueError("download_workers must be 1, 2, 4 or 8")
        if kind not in {"download", "convert", "check"}:
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
        with self.db() as db:
            row = db.execute("SELECT options,kind,telemetry FROM jobs WHERE id=?", (job_id,)).fetchone()
        workers = json.loads(row["options"]).get("download_workers", self.settings()["download_workers"]) if row["kind"] == "download" else 1
        previous = json.loads(row["telemetry"])
        self.coordinator = DownloadCoordinator(workers, self.cancel, previous)
        self.monitor = TransferMonitor(job_id, previous)
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
            active = db.execute("SELECT book_id,book FROM items WHERE job_id=? AND status='active' ORDER BY book_id", (job_id,)).fetchall()
            failures = [dict(r) for r in db.execute("SELECT book_id,error,attempts,diagnostic FROM items WHERE job_id=? AND status='failed' LIMIT 20", (job_id,))]
            saved = db.execute("SELECT book,result FROM items WHERE job_id=? AND status IN ('done','skipped')", (job_id,)).fetchall()
        result = dict(row)
        result["options"] = json.loads(result["options"])
        result["counts"] = {state: counts.get(state, 0) for state in ("pending", "active", "done", "skipped", "failed")}
        result["total"] = sum(counts.values())
        result["active_books"] = [{"id": r["book_id"], "title": json.loads(r["book"])["title"]} for r in active]
        result["active_book"] = result["active_books"][0] if active else None
        telemetry = json.loads(result.pop("telemetry"))
        transferred = sum((json.loads(r["result"]) or {}).get("bytes", 0) for r in saved if r["result"])
        if self.monitor and self.monitor.job_id == job_id:
            telemetry = self.monitor.snapshot()
            telemetry.update(self.coordinator.status())
            if telemetry["active_books"]:
                result["active_books"] = telemetry.pop("active_books")
        if not telemetry.get("measured"):
            span = (datetime.datetime.fromisoformat(result["updated"]) - datetime.datetime.fromisoformat(result["created"])).total_seconds()
            telemetry = {"elapsed_seconds": max(0, span), "network_bytes": transferred, "bytes_per_second": 0, "measured": False}
        telemetry["saved_bytes"] = transferred
        telemetry["average_bytes_per_second"] = telemetry.get("network_bytes", 0) / max(0.001, telemetry.get("elapsed_seconds", 0))
        result["telemetry"] = telemetry
        for failure in failures:
            failure["diagnostic"] = json.loads(failure["diagnostic"]) or describe(ValueError(failure["error"]))
        result["failures"] = failures
        return result

    def jobs(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 100")]
        return [self.status(i) for i in ids]

    def items(self, job_id, status=None, page=1, page_size=100):
        job = self.status(job_id)
        condition = "job_id=?" + (" AND status=?" if status else "")
        parameters = [job_id] + ([status] if status else [])
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM items WHERE " + condition, parameters).fetchone()[0]
            rows = db.execute("SELECT book_id,status,attempts,result,error,diagnostic,duration FROM items WHERE " + condition + " ORDER BY book_id LIMIT ? OFFSET ?",
                              parameters + [page_size, (page - 1) * page_size]).fetchall()
        values = [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None,
                   "diagnostic": json.loads(r["diagnostic"]) or (describe(ValueError(r["error"])) if r["error"] else None)} for r in rows]
        inventory = self.library.inventory()
        for value in values:
            local = inventory.get(value["book_id"], {})
            value["resolved"] = bool(local.get("downloaded" if job["kind"] == "download" else "epub_available" if job["kind"] == "convert" else "can_convert"))
        return {"total": total, "page": page, "page_size": page_size, "items": values}

    def report(self, job_id):
        job = self.status(job_id)
        with self.db() as db:
            events = [dict(row) for row in db.execute("SELECT book_id,attempt,created,duration,details FROM transfer_events WHERE job_id=? ORDER BY id", (job_id,))]
        for event in events:
            event["details"] = json.loads(event["details"])
        return {"job": job, "items": self.items(job_id, page_size=max(1, job["total"]))["items"], "events": events}

    def diagnose(self, job_id):
        job = self.status(job_id)
        if job["kind"] != "download":
            raise ValueError("Server response checks apply to download jobs")
        with self.db() as db:
            rows = db.execute("SELECT book_id,book,error FROM items WHERE job_id=? AND status='failed' ORDER BY book_id LIMIT 50", (job_id,)).fetchall()
        inventory = self.library.inventory(force=True)
        rows = [row for row in rows if not inventory.get(row["book_id"], {}).get("downloaded")]

        def probe(row):
            book = json.loads(row["book"])
            url = ghbook.download_url({**book, "ghm_file_name": ghbook.book_filename(book)})
            started = time.monotonic()
            try:
                with ghbook.request(url, {"Range": "bytes=0-8191"}) as response:
                    mime = response.headers.get_content_type()
                    body = response.read(8192)
                    if mime.startswith("text/") or "json" in mime:
                        detail = response_failure(response.status, response.headers, body, url).details
                    else:
                        detail = {"category": "available_now", "label": "Available for retry", "message": "The server currently returns book data.",
                                  "action": "Retry the download to save and validate the complete file.", "http_status": response.status,
                                  "retryable": True, "server_message": "", "url": url}
            except Exception as error:
                detail = describe(error)
            detail.update({"probe": True, "checked_at": now(), "previous_error": row["error"]})
            with self.db() as db:
                db.execute("UPDATE items SET diagnostic=?,error=? WHERE job_id=? AND book_id=? AND status='failed'",
                           (json.dumps(detail, ensure_ascii=False), detail["message"], job_id, row["book_id"]))
                db.execute("INSERT INTO transfer_events(job_id,book_id,attempt,created,duration,details) VALUES (?,?,?,?,?,?)",
                           (job_id, row["book_id"], 0, now(), time.monotonic() - started, json.dumps(detail, ensure_ascii=False)))
            self.library.record(book, "download", error=DownloadFailure(detail))
            return {"book_id": row["book_id"], "diagnostic": detail}

        with ThreadPoolExecutor(max_workers=2) as pool:
            return {"checked": list(pool.map(probe, rows))}

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
            try:
                def progress(event):
                    if event.get("phase") == "connecting":
                        self.coordinator.wait_ready()
                    self.monitor.update(int(book["id"]), event)
                result = ghbook.download(int(book["id"]), self.downloads, book=book, resume=True, cancel=self.cancel.is_set,
                                        progress=progress, mode="ranges" if options.get("_range_retry") else "auto")
                self.library.record(book, "download", result=result)
            except InterruptedError:
                raise
            except Exception as error:
                self.library.record(book, "download", error=error)
                raise
            source = Path(result["path"])
        else:
            source = self.local_source(book)
            result = {"id": int(book["id"]), "path": str(source), "skipped": True}
        if kind == "check":
            try:
                check = ghm_epub.inspect_book(source)
                self.library.record(book, "check", result=check)
                return {**result, "check": check, "skipped": False}
            except Exception as error:
                self.library.record(book, "check", error=error, incompatible=isinstance(error, ghm_epub.UnsupportedGhm))
                raise
        if kind == "convert" or options.get("convert_epub"):
            if self.cancel.is_set():
                raise InterruptedError("Job paused")
            try:
                converted = ghm_epub.convert(source, self.epubs / (source.stem + ".epub"))
                self.library.record(book, "convert", result=converted)
            except Exception as error:
                unsupported = isinstance(error, (ghm_epub.UnsupportedGhm, zipfile.BadZipFile, ghm_epub.ET.ParseError, UnicodeError, KeyError))
                self.library.record(book, "convert", error=error, incompatible=unsupported)
                raise
            result["epub"] = converted
            result["skipped"] = result["skipped"] and converted["skipped"]
        return result

    def run(self, job_id):
        fatal = ""
        try:
            initial = self.status(job_id)
            claims = threading.Lock()

            def work():
                while not self.cancel.is_set():
                    with claims:
                        if self.cancel.is_set():
                            return
                        with self.db() as db:
                            row = db.execute("SELECT book_id,book FROM items WHERE job_id=? AND status='pending' ORDER BY book_id LIMIT 1", (job_id,)).fetchone()
                            if row is None:
                                return
                            db.execute("UPDATE items SET status='active' WHERE job_id=? AND book_id=?", (job_id, row["book_id"]))
                    self.run_item(job_id, json.loads(row["book"]), initial)

            with ThreadPoolExecutor(max_workers=self.coordinator.configured) as pool:
                futures = [pool.submit(work) for _ in range(self.coordinator.configured)]
                for future in futures:
                    try:
                        future.result()
                    except Exception:
                        self.cancel.set()
                        raise
        except Exception as error:
            fatal = str(error)
        finally:
            self.monitor.finish()
            with self.guard:
                counts = self.status(job_id)["counts"]
                state = "failed" if fatal else "paused" if self.cancel.is_set() else "completed_with_errors" if counts["failed"] else "completed"
                telemetry = self.monitor.snapshot()
                telemetry.update(self.coordinator.status())
                telemetry.pop("active_books", None)
                with self.db() as db:
                    db.execute("UPDATE jobs SET state=?,updated=?,error=?,telemetry=? WHERE id=?", (state, now(), fatal, json.dumps(telemetry), job_id))
                    db.execute("UPDATE items SET status='pending' WHERE job_id=? AND status='active'", (job_id,))

    def run_item(self, job_id, book, initial):
        book_id = int(book["id"])
        self.monitor.begin(book)
        range_retry = False
        try:
            for attempt in range(3):
                started = time.monotonic()
                detail = None
                try:
                    with self.coordinator.slot():
                        self.monitor.update(book_id, {"attempt": attempt + 1, "bytes": 0, "phase": "starting"})
                        with self.db() as db:
                            db.execute("UPDATE items SET attempts=attempts+1 WHERE job_id=? AND book_id=?", (job_id, book_id))
                        try:
                            result = self.execute(book, initial["kind"], {**initial["options"], "_range_retry": range_retry})
                        except Exception as error:
                            if not isinstance(error, InterruptedError):
                                detail = describe(error)
                                if detail["retryable"]:
                                    detail["wait_seconds"] = self.coordinator.backoff(detail, attempt)
                            raise
                    with self.db() as db:
                        db.execute("UPDATE items SET status=?,result=?,error='',diagnostic='{}',duration=duration+? WHERE job_id=? AND book_id=?",
                                   ("skipped" if result.get("skipped") else "done", json.dumps(result, ensure_ascii=False), time.monotonic() - started, job_id, book_id))
                    break
                except InterruptedError:
                    with self.db() as db:
                        db.execute("UPDATE items SET status='pending' WHERE job_id=? AND book_id=?", (job_id, book_id))
                    self.cancel.set()
                    break
                except Exception as error:
                    detail = detail or describe(error)
                    phase = next((b["phase"] for b in self.monitor.snapshot()["active_books"] if b["id"] == book_id), "unknown")
                    detail.update({"attempt": attempt + 1, "phase": phase})
                    duration = time.monotonic() - started
                    with self.db() as db:
                        db.execute("INSERT INTO transfer_events(job_id,book_id,attempt,created,duration,details) VALUES (?,?,?,?,?,?)",
                                   (job_id, book_id, attempt + 1, now(), duration, json.dumps(detail, ensure_ascii=False)))
                        db.execute("UPDATE items SET diagnostic=?,duration=duration+? WHERE job_id=? AND book_id=?",
                                   (json.dumps(detail, ensure_ascii=False), duration, job_id, book_id))
                    if detail["retryable"] and attempt < 2:
                        self.monitor.update(book_id, {"phase": "retry_wait", "reason": detail["label"], "wait_seconds": detail.get("wait_seconds", 2 ** attempt)})
                        if not self.cancel.wait(detail.get("wait_seconds", 2 ** attempt)):
                            range_retry = detail["category"] in {"timeout", "connection_error", "transfer_integrity"}
                            continue
                        with self.db() as db:
                            db.execute("UPDATE items SET status='pending' WHERE job_id=? AND book_id=?", (job_id, book_id))
                        break
                    with self.db() as db:
                        db.execute("UPDATE items SET status='failed',error=? WHERE job_id=? AND book_id=?", (detail["message"], job_id, book_id))
                    break
        finally:
            self.monitor.end(book_id)
            with self.db() as db:
                db.execute("UPDATE jobs SET updated=? WHERE id=?", (now(), job_id))


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

    def send_file(self, target, mime=None, attachment=False):
        if not target.is_file():
            raise FileNotFoundError("This file is not available yet")
        self.send_response(200)
        self.send_header("Content-Type", mime or mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if attachment:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(target.name))
        self.end_headers()
        with target.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

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
        except FileNotFoundError as error:
            self.send_json(404, {"error": str(error)})
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
            if path == "/guided/plan":
                self.send_json(200, manager.guided.plan(body.get("kind"), body.get("retry", False)))
                return
            if path == "/api/settings":
                self.send_json(200, manager.configure(body))
                return
            match = re.fullmatch(r"/jobs/([0-9a-f]{32})/diagnose", path)
            if match:
                self.send_json(200, manager.diagnose(match[1]))
                return
            if path == "/guided/start":
                self.send_json(202, manager.guided.start(body.get("token")))
                return
            if path == "/shutdown":
                token = getattr(self.server, "shutdown_token", None)
                supplied = body.get("token")
                if not token or not isinstance(supplied, str) or not hmac.compare_digest(token, supplied):
                    self.send_json(403, {"error": "A local service token is required"})
                    return
                manager.cancel.set()
                self.send_json(200, {"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/library/plan":
                kind = body.pop("kind", "download")
                self.send_json(200, manager.plan(body, kind))
                return
            if path in {"/jobs", "/conversions", "/checks"}:
                kind = {"/jobs": "download", "/conversions": "convert", "/checks": "check"}[path]
                self.send_json(202, manager.create(body, kind))
                return
            if path == "/open-folder":
                directory = {"downloads": manager.downloads, "epubs": manager.epubs}.get(body.get("folder"))
                if directory is None:
                    raise ValueError("folder must be downloads or epubs")
                directory.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(directory))
                else:
                    command = "open" if sys.platform == "darwin" else "xdg-open"
                    executable = shutil.which(command)
                    if not executable:
                        raise ValueError("No desktop folder opener is installed. Saved files are at " + str(directory))
                    subprocess.Popen([executable, str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.send_json(200, {"path": str(directory)})
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
                    manager.library.rebuild()
                self.send_json(200, {"books": len(manager.books)})
                return
        else:
            assets = {"": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/books.js": ("books.js", "text/javascript; charset=utf-8"),
                      "/transfers.js": ("transfers.js", "text/javascript; charset=utf-8"),
                      "/app.css": ("app.css", "text/css; charset=utf-8"),
                      "/icon.svg": ("icon.svg", "image/svg+xml")}
            for page in ("catalog", "downloaded", "epubs", "downloads", "conversions", "activity", "errors", "settings"):
                assets["/" + page] = ("index.html", "text/html; charset=utf-8")
            if path in assets:
                name, mime = assets[path]
                self.send_file(ghbook.ROOT / "web" / name, mime)
                return
            if path == "/health":
                self.send_json(200, {"status": "ok", "catalog_books": len(manager.books),
                                     "app": "revive-library", "version": 3, "pid": os.getpid(), "platform": sys.platform,
                                     "root": str(manager.root.resolve()), "data_dir": str(manager.data_dir.resolve()),
                                     "conversion_runtime": bool(node_runtime()),
                                     "active_job": manager.active_job if manager.worker and manager.worker.is_alive() else None,
                                     "downloads_directory": str(manager.downloads), "epubs_directory": str(manager.epubs)})
                return
            if path == "/api/settings":
                self.send_json(200, manager.settings())
                return
            match = re.fullmatch(r"/jobs/([0-9a-f]{32})/report", path)
            if match:
                self.send_json(200, manager.report(match[1]))
                return
            if path == "/library/summary":
                self.send_json(200, manager.library.summary())
                return
            if path == "/guided":
                self.send_json(200, manager.guided.status())
                return
            if path == "/categories":
                self.send_json(200, {"categories": manager.library.category_rows()})
                return
            if path == "/library/export":
                inventory = manager.library.inventory()
                books = manager.library.filtered(manager.books, query)
                buffer = io.StringIO()
                fields = ["id", "title", "author", "lang", "category", "status", "can_convert", "downloaded", "epub_available", "error"]
                writer = csv.DictWriter(buffer, fieldnames=fields)
                writer.writeheader()
                for book in books:
                    row = manager.library.annotate(book, inventory)
                    writer.writerow({**{k: book.get(k, "") for k in ("id", "title", "author", "lang")},
                                     "category": " / ".join(c["title"] for c in row["categories"]),
                                     **{k: row["local"].get(k, "") for k in fields[5:]}})
                data = ("\ufeff" + buffer.getvalue()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="revive-library.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/books":
                books = manager.library.filtered(manager.books, query)
                page, size = self.pagination(query, len(books))
                inventory = manager.library.inventory()
                self.send_json(200, {"total": len(books), "page": page, "page_size": size,
                                     "books": [manager.library.annotate(b, inventory) for b in books[(page - 1) * size:page * size]]})
                return
            match = re.fullmatch(r"/books/(\d+)", path)
            if match:
                book = manager.by_id[int(match[1])]
                self.send_json(200, {**manager.library.annotate(book), "download_url": ghbook.download_url({**book, "ghm_file_name": ghbook.book_filename(book)})})
                return
            match = re.fullmatch(r"/receipts/(\d+)\.json", path)
            if match:
                book = manager.by_id[int(match[1])]
                source = manager.local_source(book)
                target = (manager.epubs / (source.stem + ".epub.json")) if query.get("type", "epub") == "epub" else source.with_suffix(".ghm.json")
                self.send_file(target, "application/json; charset=utf-8", attachment=True)
                return
            match = re.fullmatch(r"/covers/(\d+)", path)
            if match:
                book = manager.by_id[int(match[1])]
                filename = ghbook.book_filename(book)
                with zipfile.ZipFile(manager.epubs / (Path(filename).stem + ".epub")) as archive:
                    package = ghm_epub.ET.fromstring(archive.read("OEBPS/content.opf"))
                    cover = next((item for item in package.iter() if "cover-image" in item.get("properties", "").split()), None)
                    if cover is None:
                        raise FileNotFoundError("This book has no bundled cover")
                    href = cover.get("href", "")
                    if ".." in href.split("/") or href.startswith("/"):
                        raise ValueError("Invalid cover path")
                    entry = archive.getinfo("OEBPS/" + href)
                    if entry.file_size > 5 * 1024 * 1024:
                        raise ValueError("Cover is too large")
                    data = archive.read(entry)
                    mime = cover.get("media-type", "application/octet-stream")
                    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                        raise ValueError("Unsupported cover image")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=300")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/downloads/plan":
                options = {"scope": query.get("scope", "all")}
                if query.get("ids"):
                    options = {"scope": "ids", "ids": [int(i) for i in query["ids"].split(",")]}
                for key in ("lang", "limit", "q", "category", "state", "sort"):
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
                self.send_file(target, "application/epub+zip" if match[3] == "epub" else "application/octet-stream", attachment=True)
                return
        self.send_json(404, {"error": "Unknown endpoint"})

    def pagination(self, query, total, allow_all=True):
        page = int(query.get("page", "1"))
        raw_size = query.get("page_size", "100")
        size = max(total, 1) if allow_all and raw_size == "all" else int(raw_size)
        if page < 1 or size < 1 or not (allow_all and raw_size == "all") and size > 1000:
            raise ValueError("page must be positive; page_size must be 1..1000 or all")
        return page, size


def serve(port=8765, data_dir=None):
    manager = Manager(data_dir=data_dir)
    server = None
    control = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.manager = manager
        server.shutdown_token = secrets.token_hex(32)
        control = manager.state_dir / "service-control.json"
        ghbook.save_json(control, {"token": server.shutdown_token, "port": port})
        print(f"GHBook recovery API: http://127.0.0.1:{port} ({len(manager.books)} catalog entries)", flush=True)
        print(f"Library app: http://127.0.0.1:{port} — saved files: {manager.data_dir}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nPausing recovery before shutdown...", flush=True)
    finally:
        try:
            if server:
                server.server_close()
            manager.close()
        finally:
            if control:
                control.unlink(missing_ok=True)
