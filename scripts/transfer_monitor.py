"""Thread-safe live progress; SQLite receives summaries rather than chunk writes."""
from collections import deque
import threading
import time


class TransferMonitor:
    def __init__(self, job_id, previous=None):
        self.job_id = job_id
        self.previous = previous or {}
        self.started = time.monotonic()
        self.ended = None
        self.lock = threading.Lock()
        self.books = {}
        self.samples = deque()
        self.bytes = 0
        self.requests = 0

    def begin(self, book):
        with self.lock:
            self.books[int(book["id"])] = {"id": int(book["id"]), "title": book["title"], "bytes": 0,
                "total": int(book.get("file_size") or 0), "phase": "waiting", "attempt": 1}

    def update(self, book_id, event):
        with self.lock:
            item = self.books.get(book_id)
            if item is None:
                return
            item.update({k: v for k, v in event.items() if k != "delta" and v is not None})
            if event.get("phase") == "connecting":
                self.requests += 1
            delta = event.get("delta", 0)
            if delta:
                self.bytes += delta
                now = time.monotonic()
                self.samples.append((now, delta))
                while self.samples and self.samples[0][0] < now - 10:
                    self.samples.popleft()

    def end(self, book_id):
        with self.lock:
            self.books.pop(book_id, None)

    def finish(self):
        with self.lock:
            self.ended = time.monotonic()

    def snapshot(self):
        with self.lock:
            now = self.ended or time.monotonic()
            elapsed = now - self.started
            recent = sum(n for stamp, n in self.samples if stamp >= now - 10)
            return {"elapsed_seconds": self.previous.get("elapsed_seconds", 0) + elapsed,
                    "network_bytes": self.previous.get("network_bytes", 0) + self.bytes,
                    "requests": self.previous.get("requests", 0) + self.requests,
                    "bytes_per_second": recent / max(0.25, min(10, elapsed)) if self.ended is None else 0,
                    "active_books": [dict(item) for item in self.books.values()], "measured": True}
