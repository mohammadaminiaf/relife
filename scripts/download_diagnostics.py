"""Plain-language, evidence-based transfer errors and shared server backoff."""
from contextlib import contextmanager
import datetime
from email.utils import parsedate_to_datetime
import errno
import html
import http.client
import re
import ssl
import threading
import time
import urllib.error


def retry_seconds(value):
    if not value:
        return None
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            return max(0, (stamp - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def plain_text(body):
    value = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    value = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value, flags=re.I | re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value)).split())[:1000]


class DownloadFailure(ValueError):
    def __init__(self, details):
        self.details = details
        super().__init__(details["message"])


def response_failure(status, headers, body=b"", url=""):
    text = plain_text(body)
    lower = text.casefold()
    after = retry_seconds(headers.get("Retry-After"))
    category, label, message, retryable, action = (
        "invalid_response", "Unexpected server response", "The server sent a page instead of book data.", False,
        "Review the server message below. You can retry later.")
    if status == 429 or any(word in lower for word in ("too many requests", "rate limit exceeded", "rate limit reached")):
        category, label, message, retryable, action = "rate_limit", "Rate limited", "The server is limiting download requests.", True, "The app will wait and reduce simultaneous downloads."
    elif any(word in lower for word in ("درخواست ناشر", "درخواست صاحب", "publisher request", "removed at the request", "حذف گردید", "حذف شده")):
        category, label, message, action = "publisher_removed", "Removed by publisher", "The server says this file was removed at the publisher or author's request.", "This is unavailable on the server. Retrying the same file is unlikely to help."
    elif status == 404 or "file not found" in lower or "فایل یافت نشد" in lower:
        category, label, message, action = "not_found", "File unavailable", "The server could not find this book file.", "Refresh the catalog or retry later."
    elif status in {401, 403}:
        category, label, message, action = "access_denied", "Access denied", "The server refused access to this book.", "This may require server-side permission or an account."
    elif status in {408, 504}:
        category, label, message, retryable, action = "timeout", "Server timed out", "The server did not respond in time.", True, "The app will retry this transfer."
    elif status >= 500:
        category, label, message, retryable, action = "server_error", "Server error", f"The book server returned HTTP {status}.", True, "The app will retry after a short wait."
    elif status >= 400:
        category, label, message = "http_error", "Request rejected", f"The server rejected the download (HTTP {status})."
    return DownloadFailure({"category": category, "label": label, "message": message, "retryable": retryable,
                            "http_status": status, "retry_after_seconds": after, "server_message": text,
                            "url": url, "action": action,
                            "headers": {key: headers[key] for key in ("Content-Type", "Content-Range", "Retry-After", "Server") if headers.get(key)}})


def describe(error):
    if isinstance(error, DownloadFailure):
        return dict(error.details)
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read(8192)
        except OSError:
            body = b""
        finally:
            error.close()
        return response_failure(error.code, error.headers or {}, body, error.url).details
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    category, label, retryable, action = "unexpected_error", "Operation failed", False, "Review the details and retry if appropriate."
    if isinstance(reason, (TimeoutError,)):
        category, label, retryable, action = "timeout", "Connection timed out", True, "The app will retry; the server or connection stalled."
    elif isinstance(reason, ssl.SSLError):
        category, label, action = "tls_error", "Secure connection failed", "Check your system clock and network connection."
    elif isinstance(error, (urllib.error.URLError, ConnectionError, http.client.IncompleteRead)):
        category, label, retryable, action = "connection_error", "Connection failed", True, "Check connectivity. The app will retry transient connection errors."
    elif isinstance(error, FileExistsError):
        category, label, action = "existing_file", "Existing file needs review", "The existing file was preserved. Check its receipt before replacing it."
    elif isinstance(error, PermissionError):
        category, label, action = "permission_denied", "Storage permission denied", "Choose a writable storage folder."
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        category, label, action = "disk_full", "Storage is full", "Free space or choose another drive before retrying."
    elif isinstance(error, ValueError):
        category, label, action = "invalid_data", "File validation failed", "The original was preserved. Check the server response or format details."
        if any(word in str(error).lower() for word in ("incomplete", "unexpected byte range", "server changed")):
            category, label, retryable, action = "transfer_integrity", "Incomplete or changed transfer", True, "The app will retry and validate the complete response."
    text = str(error)[:2000]
    if "text/html instead of a book" in text:
        label, category, action = "Server returned a web page", "invalid_response", "This older failure did not save the server's message. Retry to collect a precise diagnosis."
    return {"category": category, "label": label, "message": text, "retryable": retryable,
            "http_status": None, "retry_after_seconds": None, "server_message": "", "action": action}


class DownloadCoordinator:
    """Limit simultaneous books, and respect a server-wide Retry-After window."""
    def __init__(self, workers, cancel, previous=None):
        previous = previous or {}
        self.configured = workers
        self.limit = min(workers, previous.get("effective_workers", workers))
        self.cancel = cancel
        self.condition = threading.Condition()
        self.active = 0
        self.until = 0
        self.until_wall = 0
        self.rate_limits = previous.get("rate_limit_events", 0)
        if previous.get("cooldown_until"):
            try:
                self.until_wall = datetime.datetime.fromisoformat(previous["cooldown_until"]).timestamp()
                self.until = time.monotonic() + max(0, self.until_wall - time.time())
            except (ValueError, TypeError):
                self.until_wall = 0

    def wait_ready(self):
        while True:
            if self.cancel.is_set():
                raise InterruptedError("Job paused")
            with self.condition:
                delay = self.until - time.monotonic()
            if delay <= 0:
                return
            self.cancel.wait(min(delay, 0.2))

    @contextmanager
    def slot(self):
        while True:
            self.wait_ready()
            with self.condition:
                if self.active < self.limit and time.monotonic() >= self.until:
                    self.active += 1
                    break
                self.condition.wait(0.1)
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def backoff(self, detail, attempt):
        delay = detail.get("retry_after_seconds")
        delay = delay if delay is not None else (15 * (attempt + 1) if detail["category"] == "rate_limit" else 2 ** attempt)
        if detail["category"] == "rate_limit" or detail.get("retry_after_seconds") is not None:
            with self.condition:
                proposed = time.monotonic() + delay
                if proposed > self.until:
                    self.until = proposed
                    self.until_wall = time.time() + delay
                if detail["category"] == "rate_limit":
                    self.limit = max(1, self.limit // 2)
                    self.rate_limits += 1
        return delay

    def status(self):
        with self.condition:
            return {"configured_workers": self.configured, "effective_workers": self.limit,
                    "rate_limit_events": self.rate_limits, "cooldown_seconds": max(0, self.until - time.monotonic()),
                    "cooldown_until": datetime.datetime.fromtimestamp(self.until_wall, datetime.timezone.utc).isoformat() if self.until_wall > time.time() else None}
