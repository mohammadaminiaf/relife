"""Confirmation-based, filesystem-aware batches over the persistent job system."""
import datetime
import json
import secrets

import ghbook
from library_index import fingerprint

BATCH_SIZE = 50
DOWNLOAD_BATCH_SIZES = (50, 100, 200, 1000, 5000)


class GuidedQueue:
    def __init__(self, manager):
        self.manager = manager
        with manager.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS batch_previews (
                token TEXT PRIMARY KEY, kind TEXT NOT NULL, retry INTEGER NOT NULL,
                ids TEXT NOT NULL, signature TEXT NOT NULL, created TEXT NOT NULL,
                batch_size INTEGER NOT NULL DEFAULT 50
            )""")
            if "batch_size" not in {row[1] for row in db.execute("PRAGMA table_info(batch_previews)")}:
                db.execute("ALTER TABLE batch_previews ADD COLUMN batch_size INTEGER NOT NULL DEFAULT 50")

    def snapshot(self, force=False):
        manager = self.manager
        inventory = manager.library.inventory(force=force)
        with manager.db() as db:
            facts = {row["book_id"]: dict(row) for row in db.execute("SELECT * FROM library_state")}
            jobs = db.execute("SELECT id,kind,state,options FROM jobs WHERE state IN ('paused','interrupted','failed') ORDER BY created").fetchall()
            pending = {}
            for job in jobs:
                ids = {row[0] for row in db.execute("SELECT book_id FROM items WHERE job_id=? AND status IN ('pending','active','failed')", (job["id"],))}
                if ids:
                    pending[job["id"]] = ids
        result = {}
        download_size = manager.settings()["download_batch_size"]
        for kind in ("download", "convert"):
            batch_size = download_size if kind == "download" else BATCH_SIZE
            reserved, resume = set(), None
            for job in jobs:
                options = json.loads(job["options"])
                applies = job["kind"] == kind or kind == "convert" and job["kind"] == "download" and options.get("convert_epub")
                if applies and job["id"] in pending:
                    reserved.update(pending[job["id"]])
                    if job["kind"] == kind and options.get("guided") and resume is None:
                        resume = manager.status(job["id"])
            ready, retry, attention, saved, waiting, held = [], [], 0, 0, 0, 0
            for book in sorted(manager.books, key=lambda b: int(b["id"])):
                book_id = int(book["id"])
                local = inventory[book_id]
                if local.get("downloaded" if kind == "download" else "epub_available"):
                    saved += 1
                    continue
                if kind == "convert" and not local.get("downloaded"):
                    waiting += 1
                    continue
                try:
                    ghbook.book_filename(book)
                except ValueError:
                    attention += 1
                    continue
                if book_id in reserved:
                    held += 1
                    continue
                fact = facts.get(book_id, {})
                failed = local.get("error") and fact.get("action") in ({"download"} if kind == "download" else {"convert", "check"})
                if failed or kind == "convert" and local.get("can_convert") is False:
                    attention += 1
                    retry.append(book_id)
                else:
                    ready.append(book_id)
            result[kind] = {"kind": kind, "batch_size": batch_size, "total": len(manager.books),
                            "completed": saved, "ready": len(ready), "needs_attention": attention,
                            "retryable": len(retry), "waiting_download": waiting, "reserved": held,
                            "next_count": min(batch_size, len(ready)), "resume_job": resume,
                            "ready_ids": ready, "retry_ids": retry}
        return result

    def status(self):
        with self.manager.guard:
            queues = self.snapshot()
            for queue in queues.values():
                queue.pop("ready_ids")
                queue.pop("retry_ids")
            return {"batch_size": queues["download"]["batch_size"], "batch_size_options": list(DOWNLOAD_BATCH_SIZES), "queues": queues}

    def signature(self, ids, kind):
        values = []
        for book_id in ids:
            book = self.manager.by_id[book_id]
            source = self.manager.local_source(book) if kind == "convert" else None
            values.append([book_id, ghbook.book_filename(book), book.get("version"), book.get("file_size"),
                           fingerprint(source) if source else None])
        return json.dumps(values, sort_keys=True)

    def plan(self, kind, retry=False):
        if kind not in {"download", "convert"} or not isinstance(retry, bool):
            raise ValueError("Choose download or convert, and a boolean retry flag")
        manager = self.manager
        with manager.guard:
            queue = self.snapshot(force=True)[kind]
            if queue["resume_job"]:
                raise ValueError("Resume your unfinished batch before starting another one")
            batch_size = queue["batch_size"]
            ids = queue["retry_ids" if retry else "ready_ids"][:batch_size]
            if not ids:
                raise ValueError("No failed books to retry" if retry else "No books are ready for this batch")
            options = {"scope": "ids", "ids": ids, "guided": True, "batch_size": batch_size}
            plan = manager.plan(options, kind)
            token = secrets.token_hex(24)
            created = datetime.datetime.now(datetime.timezone.utc)
            with manager.db() as db:
                db.execute("DELETE FROM batch_previews WHERE created < ?", ((created - datetime.timedelta(days=1)).isoformat(),))
                db.execute("INSERT INTO batch_previews(token,kind,retry,ids,signature,created,batch_size) VALUES (?,?,?,?,?,?,?)",
                           (token, kind, int(retry), json.dumps(ids), self.signature(ids, kind), created.isoformat(), batch_size))
            return {**plan, "token": token, "retry": retry, "guided": True, "batch_size": batch_size}

    def start(self, token):
        # Import here to avoid a circular dependency with the HTTP adapter.
        from recovery_api import Conflict
        if not isinstance(token, str) or not token:
            raise ValueError("Review a batch before confirming it")
        manager = self.manager
        with manager.guard:
            if manager.worker and manager.worker.is_alive():
                raise Conflict("A batch is already running. Wait for it or pause it first")
            with manager.db() as db:
                preview = db.execute("SELECT * FROM batch_previews WHERE token=?", (token,)).fetchone()
            if preview is None:
                raise Conflict("This preview has expired or was already confirmed. Review the next batch again")
            age = datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(preview["created"])
            ids, kind = json.loads(preview["ids"]), preview["kind"]
            queue = self.snapshot(force=True)[kind]
            batch_size = preview["batch_size"]
            if batch_size not in (DOWNLOAD_BATCH_SIZES if kind == "download" else (BATCH_SIZE,)):
                raise Conflict("This batch size is invalid. Review the next batch again")
            current = queue["retry_ids" if preview["retry"] else "ready_ids"][:batch_size]
            if age > datetime.timedelta(days=1) or queue["resume_job"] or ids != current or self.signature(ids, kind) != preview["signature"]:
                raise Conflict("Your library changed since this preview. Review the next batch again")
            job = manager.create({"scope": "ids", "ids": ids, "guided": True, "batch_size": batch_size}, kind)
            with manager.db() as db:
                db.execute("DELETE FROM batch_previews WHERE token=?", (token,))
            return job
