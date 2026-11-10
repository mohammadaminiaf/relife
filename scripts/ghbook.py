#!/usr/bin/env python3
"""Read the catalog and download books using requests recovered from Myghbook.apk."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from download_diagnostics import response_failure

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "analysis"
BOOTSTRAP = "https://www.ghbook.ir/api/GetBaseUrl.php?id=1"
METADATA = "https://www.ghbook.ir/api/book_meta_info.php"
DOWNLOAD = "https://download.ghbook.ir/downloads.php"
COLUMNS = ("id", "title", "author", "subject", "subject_1", "subject_2", "year",
           "file_name", "pic_name", "download_count", "file_size", "price",
           "publish", "publisher_id", "publisher", "version", "link_id")


def https(url):
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(scheme="https")) if parts.scheme == "http" else url


def request(url, headers=None):
    return urllib.request.urlopen(urllib.request.Request(https(url), headers={
        "User-Agent": "GHBook-Recovery/1.0",
        **(headers or {}),
    }), timeout=30)


def fetch_json(url):
    with request(url) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def transfer(url, stream, book=False, cancel=None, progress=None, metrics=None, mode="auto"):
    """Stream books with one request; use bounded ranges as a retry fallback."""
    block = 1024 * 1024
    read_size = 64 * 1024
    digest = hashlib.sha256()
    size = 0
    started = time.monotonic()
    metrics = metrics if metrics is not None else {}
    metrics.update({"requests": 0, "mode": "stream" if book and mode != "ranges" else "ranges", "time_to_headers_seconds": None})

    def report(**event):
        if progress:
            progress(event)

    def open_response(headers):
        if cancel and cancel():
            raise InterruptedError("Job paused")
        report(phase="connecting")
        metrics["requests"] += 1
        response = request(url, headers)
        if metrics["time_to_headers_seconds"] is None:
            metrics["time_to_headers_seconds"] = time.monotonic() - started
        report(phase="downloading", http_status=response.status)
        mime = response.headers.get_content_type()
        if book and (mime.startswith("text/") or mime in ("application/json", "application/problem+json")):
            with response:
                raise response_failure(response.status, response.headers, response.read(8192), url)
        return response

    def write(data):
        nonlocal size
        if cancel and cancel():
            raise InterruptedError("Job paused")
        stream.write(data)
        digest.update(data)
        size += len(data)
        report(phase="downloading", delta=len(data), bytes=size)

    headers = {} if book and mode != "ranges" else {"Range": f"bytes=0-{block - 1}"}
    with open_response(headers) as response:
        if response.status == 200:
            expected = response.headers.get("Content-Length")
            report(total=int(expected) if expected else None)
            while True:
                if cancel and cancel():
                    raise InterruptedError("Job paused")
                data = response.read(read_size)
                if not data:
                    break
                write(data)
            if size == 0 or (expected is not None and size != int(expected)):
                raise ValueError("Incomplete download")
            metrics.update({"seconds": time.monotonic() - started, "bytes": size})
            return size, digest.hexdigest()
        match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if response.status != 206 or not match:
            raise ValueError("Invalid initial HTTP range response")
        end, total = map(int, match.groups())
        report(total=total)
        if end != min(block, total) - 1 or total <= 0:
            raise ValueError("Unexpected initial HTTP byte range")
        data = response.read()
        if len(data) != end + 1:
            raise ValueError("Incomplete initial HTTP byte range")
        write(data)
        validator = response.headers.get("ETag") or response.headers.get("Last-Modified")

    def part(start):
        if cancel and cancel():
            raise InterruptedError("Job paused")
        end = min(start + block, total) - 1
        headers = {"Range": f"bytes={start}-{end}"}
        if validator:
            headers["If-Range"] = validator
        with open_response(headers) as response:
            if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}":
                raise ValueError("Server changed the file or returned an unexpected byte range")
            data = response.read()
        if len(data) != end - start + 1:
            raise ValueError("Incomplete HTTP byte range")
        return data

    with ThreadPoolExecutor(max_workers=2) as pool:
        offsets = iter(range(size, total, block))
        pending = deque()
        for _ in range(2):
            offset = next(offsets, None)
            if offset is not None:
                pending.append(pool.submit(part, offset))
        while pending:
            write(pending.popleft().result())
            offset = next(offsets, None)
            if offset is not None:
                pending.append(pool.submit(part, offset))
    if size != total:
        raise ValueError("Incomplete download")
    metrics.update({"seconds": time.monotonic() - started, "bytes": size})
    return size, digest.hexdigest()


def parse_catalog(text):
    books = []
    for line, row in enumerate(csv.reader(io.StringIO(text.lstrip("\ufeff"))), 1):
        if not row or not any(row):
            continue
        if len(row) < 14 or not row[0].isdigit():
            raise ValueError(f"Unexpected catalog row {line}; expected at least 14 fields and a numeric ID")
        book = dict(zip(COLUMNS, row))
        filename = book["file_name"].split("-", 2)
        book["lang"] = filename[1] if len(filename) >= 3 else ""
        books.append(book)
    return books


def parse_categories(text):
    categories = []
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if not row or not any(row):
            continue
        if len(row) < 3 or not row[0].isdigit():
            raise ValueError("Unexpected category row in subjects.txt")
        category = {"id": row[0], "parent_id": row[1]}
        for language, index in [("fa", 2), ("ar", 4), ("en", 5), ("az", 6), ("id", 7), ("ur", 8), ("zh", 9)]:
            category["title_" + language] = html.unescape(row[index]).strip() if len(row) > index else ""
        categories.append(category)
    return categories


def category_cache(root=ROOT):
    cache = Path(root) / "analysis"
    path = cache / "categories.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    archive = cache / "catalog.zip"
    if archive.exists():
        with zipfile.ZipFile(archive) as source:
            names = [n for n in source.namelist() if Path(n).name == "subjects.txt"]
            if len(names) == 1:
                result = parse_categories(source.read(names[0]).decode("utf-8-sig"))
                save_json(path, result)
                return result
    return []


def catalog(refresh=False):
    path = CACHE / "catalog.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    config = fetch_json(BOOTSTRAP)
    save_json(CACHE / "bootstrap.json", config)
    buffer = io.BytesIO()
    transfer(config["BookList_Zip"], buffer)
    data = buffer.getvalue()
    CACHE.mkdir(exist_ok=True)
    (CACHE / "catalog.zip").write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        candidates = [n for n in archive.namelist() if Path(n).name == "books.txt"]
        if len(candidates) != 1:
            raise ValueError("Catalog must contain exactly one books.txt")
        books = parse_catalog(archive.read(candidates[0]).decode("utf-8-sig"))
        subjects = [n for n in archive.namelist() if Path(n).name == "subjects.txt"]
        if len(subjects) == 1:
            save_json(CACHE / "categories.json", parse_categories(archive.read(subjects[0]).decode("utf-8-sig")))
    save_json(path, books)
    return books


def metadata(book_id):
    data = fetch_json(METADATA + "?" + urllib.parse.urlencode({"desc": 1, "id": book_id}))
    if not isinstance(data, dict) or str(data.get("id")) != str(book_id):
        raise ValueError(f"No valid metadata returned for book {book_id}")
    return data


def download_url(book):
    return DOWNLOAD + "?" + urllib.parse.urlencode({
        "id": book["id"], "file": book["ghm_file_name"], "type": "ghm",
        "platform": "android", "packagename": "ir.ghbook.reader", "libraryid": 1,
    })


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def book_filename(book):
    filename = book.get("ghm_file_name") or book.get("file_name") or ""
    if not filename or "/" in filename or "\\" in filename or not filename.lower().endswith(".ghm"):
        raise ValueError("Server returned an unexpected book filename")
    return filename


def download(book_id, output, book=None, resume=False, cancel=None, progress=None, mode="auto"):
    book = dict(book) if book is not None else metadata(book_id)
    book["ghm_file_name"] = book_filename(book)
    filename = book["ghm_file_name"]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / filename
    if target.exists():
        receipt = target.with_suffix(".ghm.json")
        if resume and receipt.exists():
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            expected = int(book.get("file_size") or 0)
            if (str(previous.get("id")) == str(book_id) and previous.get("bytes") == target.stat().st_size
                    and (not expected or expected == target.stat().st_size)
                    and previous.get("sha256") == file_hash(target)):
                return {**previous, "path": str(target.resolve()), "mtime_ns": target.stat().st_mtime_ns, "skipped": True}
        reason = "Existing file could not be verified against its receipt and catalog; kept unchanged" if resume else "Already exists"
        raise FileExistsError(f"{reason}: {target}")
    url = download_url(book)
    temporary = None
    metrics = {}
    try:
        with tempfile.NamedTemporaryFile(dir=output, prefix=".ghbook-", suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            size, digest = transfer(url, stream, book=True, cancel=cancel, progress=progress, metrics=metrics, mode=mode)
        os.link(temporary, target)  # Publish without replacing any existing file.
        result = {"id": book_id, "title": book["title"], "path": str(target.resolve()),
                  "bytes": size, "sha256": digest, "url": url, "skipped": False,
                  "mtime_ns": target.stat().st_mtime_ns,
                  "version": book.get("version"), "transfer": metrics}
        save_json(target.with_suffix(".ghm.json"), result)
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="Fetch the current library bootstrap configuration")
    cat = commands.add_parser("catalog", help="Cache the complete book catalog")
    cat.add_argument("--refresh", action="store_true")
    search = commands.add_parser("search", help="Search cached titles, authors, filenames and IDs")
    search.add_argument("query")
    search.add_argument("--lang", help="Filename language code, e.g. fa, ar, en")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--refresh", action="store_true")
    for name in ("info", "url"):
        sub = commands.add_parser(name)
        sub.add_argument("id", type=int)
    dl = commands.add_parser("download", help="Download one or more books in the original .ghm format")
    dl.add_argument("ids", nargs="+", type=int)
    dl.add_argument("--output", type=Path, default=ROOT / "downloads")
    dl.add_argument("--resume", action="store_true", help="Skip existing files only after verifying their receipts and SHA-256")
    bulk = commands.add_parser("download-all", help="Download the catalog with persistent progress and retry support")
    bulk.add_argument("--lang")
    bulk.add_argument("--query", help="Search title, author, filename or ID (Arabic/Persian spelling variants are normalized)")
    bulk.add_argument("--category", help="Category ID or an unambiguous category name; includes its subcategories")
    bulk.add_argument("--limit", type=int)
    bulk.add_argument("--offset", type=int, default=0)
    bulk.add_argument("--skip-completed", action="store_true", help="Skip already converted books, or already downloaded originals without --with-epub")
    bulk.add_argument("--with-epub", action="store_true")
    bulk.add_argument("--plan", action="store_true", help="Show counts and storage estimate without downloading")
    server = commands.add_parser("serve", help="Start the local API used by the Bruno collection")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--data-dir", type=Path, help="Store books and job state in this folder, e.g. on an external drive")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            from recovery_api import serve
            serve(args.port, args.data_dir)
            return 0
        elif args.command == "download-all":
            from recovery_api import Manager
            manager = Manager()
            options = {"scope": "all", "lang": args.lang, "q": args.query or "", "category": args.category,
                       "limit": args.limit, "offset": args.offset, "convert_epub": args.with_epub,
                       "skip_converted": args.skip_completed and args.with_epub,
                       "skip_downloaded": args.skip_completed and not args.with_epub}
            try:
                if args.plan:
                    result = manager.plan(options)
                else:
                    result = manager.create(options)
                    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                    manager.wait()
                    result = manager.status(result["id"])
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 1 if result["counts"].get("failed") else 0
            finally:
                manager.close()
        elif args.command == "config":
            result = fetch_json(BOOTSTRAP)
        elif args.command == "catalog":
            result = {"books": len(catalog(args.refresh)), "path": str(CACHE / "catalog.json")}
        elif args.command == "search":
            if args.limit < 1:
                raise ValueError("--limit must be positive")
            query = args.query.casefold()
            matches = [b for b in catalog(args.refresh)
                       if (not args.lang or b["lang"] == args.lang)
                       and any(query in b.get(k, "").casefold() for k in ("title", "author", "file_name", "id"))]
            result = {"total_matches": len(matches), "books": matches[:args.limit]}
        elif args.command in ("info", "url"):
            book = metadata(args.id)
            result = book if args.command == "info" else {"id": args.id, "url": download_url(book)}
        else:
            # Print each successful receipt immediately if a later book fails.
            for book_id in args.ids:
                print(json.dumps(download(book_id, args.output, resume=args.resume), ensure_ascii=False, indent=2), flush=True)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, urllib.error.URLError) as error:
        print(f"ghbook: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
