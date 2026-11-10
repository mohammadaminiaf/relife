"""Catalog taxonomy, local file inventory and evidence-backed conversion status."""
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import threading
import time
import unicodedata

import ghbook


def search_key(text):
    text = unicodedata.normalize("NFKD", str(text)).casefold().translate(str.maketrans({"ك": "ک", "ي": "ی", "ى": "ی", "\u200c": " "}))
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").split())


def fingerprint(path):
    stat = Path(path).stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


class LibraryIndex:
    def __init__(self, manager):
        self.manager = manager
        self.guard = threading.RLock()
        self.json_cache = {}
        self.hash_cache = {}
        self.snapshot = {}
        self.expires = 0
        self.rebuild()

    def rebuild(self):
        self.categories = {c["id"]: dict(c) for c in ghbook.category_cache(self.manager.root)}
        self.search = {}
        self.children = defaultdict(set)
        for category in self.categories.values():
            self.children[category["parent_id"]].add(category["id"])
        counts = defaultdict(set)
        for book in self.manager.books:
            book_id = int(book["id"])
            self.search[book_id] = search_key(" ".join(str(book.get(key, "")) for key in ("id", "title", "author", "publisher", "file_name")))
            for key in ("subject", "subject_1", "subject_2"):
                category_id = str(book.get(key, "-1"))
                if category_id in {"", "-1"}:
                    continue
                if category_id not in self.categories:
                    self.categories[category_id] = {"id": category_id, "parent_id": "-1", "title_en": "Category " + category_id, "title_fa": ""}
                seen = set()
                while category_id in self.categories and category_id not in seen:
                    seen.add(category_id)
                    counts[category_id].add(book_id)
                    category_id = self.categories[category_id]["parent_id"]
        for category in self.categories.values():
            category["book_count"] = len(counts[category["id"]])
            category["title"] = category.get("title_en") or category.get("title_fa") or category["id"]
        self.invalidate()

    def invalidate(self):
        with self.guard:
            self.expires = 0

    def category_rows(self):
        return sorted((c for c in self.categories.values() if c["book_count"]), key=lambda c: (search_key(c["title"]), c["id"]))

    def category_ids(self, value):
        value = str(value)
        if value not in self.categories:
            term = search_key(value)
            exact = [c["id"] for c in self.categories.values() if any(search_key(c.get("title_" + lang, "")) == term for lang in ("en", "fa", "ar"))]
            matches = exact or [c["id"] for c in self.categories.values() if term in search_key(c["title"] + " " + c.get("title_fa", "") + " " + c.get("title_ar", ""))]
            if len(matches) != 1:
                raise ValueError("Category is unknown or ambiguous; select its ID from the category list")
            value = matches[0]
        result, pending = set(), [value]
        while pending:
            current = pending.pop()
            if current not in result:
                result.add(current)
                pending.extend(self.children[current] - result)
        return result

    def read_json(self, path):
        try:
            stamp = fingerprint(path)
            key = str(path)
            cached = self.json_cache.get(key)
            if not cached or cached[0] != stamp:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.json_cache[key] = (stamp, value if isinstance(value, dict) else {})
            return self.json_cache[key][1]
        except (OSError, ValueError):
            return {}

    def checked_hash(self, path, receipt):
        stat = path.stat()
        if receipt.get("bytes") == stat.st_size and receipt.get("mtime_ns") == stat.st_mtime_ns:
            return receipt.get("sha256")
        key = (str(path), f"{stat.st_size}:{stat.st_mtime_ns}")
        if key not in self.hash_cache:
            self.hash_cache[key] = ghbook.file_hash(path)
        return self.hash_cache[key]

    def inventory(self, force=False):
        with self.guard:
            if not force and time.monotonic() < self.expires:
                return self.snapshot
            manager = self.manager
            sources = {}
            for directory in (manager.downloads / "bundled", manager.downloads):
                if directory.is_dir():
                    for entry in os.scandir(directory):
                        if entry.name.lower().endswith(".ghm") and entry.is_file():
                            sources[entry.name] = Path(entry.path)
            with manager.db() as db:
                facts = {r["book_id"]: dict(r) for r in db.execute("SELECT * FROM library_state")}
            result = {}
            for book in manager.books:
                book_id = int(book["id"])
                fact = facts.get(book_id, {})
                try:
                    filename = ghbook.book_filename(book)
                except ValueError:
                    result[book_id] = {"status": "unsupported", "downloaded": False, "epub_available": False,
                                       "can_convert": None, "error": "Catalog filename is invalid"}
                    continue
                source = sources.get(filename)
                source_stat = None
                if source:
                    try:
                        source_stat = source.stat()
                        stamp = f"{source_stat.st_size}:{source_stat.st_mtime_ns}"
                    except FileNotFoundError:
                        source = None
                current = bool(source and fact.get("fingerprint") == stamp)
                compatible = bool(fact["compatible"]) if current and fact.get("compatible") is not None else None
                epub = manager.epubs / (Path(filename).stem + ".epub")
                available, epub_bytes, warning_count, has_cover = False, 0, 0, False
                if source and epub.is_file():
                    receipt = self.read_json(epub.with_suffix(".epub.json"))
                    download_receipt = self.read_json(source.with_suffix(".ghm.json"))
                    source_hash = self.checked_hash(source, download_receipt) if receipt.get("source_sha256") else None
                    epub_bytes = epub.stat().st_size
                    available = bool(receipt.get("source_sha256") == source_hash and source_hash
                                     and receipt.get("bytes") == epub_bytes and str(receipt.get("id")) == str(book_id)
                                     and receipt.get("sha256") == self.checked_hash(epub, receipt))
                    warning_count = len(receipt.get("missing_media", []))
                    has_cover = bool(receipt.get("cover"))
                if available:
                    state, compatible = "converted", True
                elif not source:
                    state = "not_downloaded"
                elif compatible is True:
                    state = "convertible"
                elif compatible is False:
                    state = "unsupported"
                else:
                    state = "unchecked"
                # Remote errors remain actionable; stale format errors do not label a changed source.
                error = fact.get("error", "") if current or not source else ""
                result[book_id] = {"status": state, "downloaded": bool(source), "ghm_bytes": source_stat.st_size if source else 0,
                                   "epub_available": available, "epub_bytes": epub_bytes,
                                   "can_convert": compatible, "checked_at": fact.get("checked_at") if current else None,
                                   "error": error, "missing_media_count": warning_count,
                                   "has_cover": has_cover,
                                   "ghm_url": f"/files/{book_id}.ghm" if source else None,
                                   "epub_url": f"/epubs/{book_id}.epub" if available else None}
            self.snapshot = result
            self.expires = time.monotonic() + 4
            return result

    def annotate(self, book, inventory=None):
        categories = []
        for key in ("subject", "subject_1", "subject_2"):
            category = self.categories.get(str(book.get(key, "")))
            if category and category not in categories:
                categories.append(category)
        return {**book, "categories": categories,
                "local": (inventory if inventory is not None else self.inventory()).get(int(book["id"]), {})}

    def filtered(self, books, options):
        terms = search_key(options.get("q", "")).split()
        if terms:
            books = [b for b in books if all(term in self.search[int(b["id"])] for term in terms)]
        if options.get("lang"):
            books = [b for b in books if b.get("lang") == options["lang"]]
        if options.get("category"):
            ids = self.category_ids(options["category"])
            books = [b for b in books if any(str(b.get(k, "")) in ids for k in ("subject", "subject_1", "subject_2"))]
        state = options.get("state")
        if state and state != "all":
            if state not in {"converted", "convertible", "unchecked", "not_downloaded", "unsupported", "downloaded", "failed", "needs_conversion", "conversion_failed"}:
                raise ValueError("Unknown library status")
            inventory = self.inventory()
            books = [b for b in books if (inventory[int(b["id"])].get("downloaded") if state == "downloaded" else
                                         inventory[int(b["id"])].get("downloaded") and not inventory[int(b["id"])].get("epub_available") if state == "needs_conversion" else
                                         inventory[int(b["id"])].get("downloaded") and bool(inventory[int(b["id"])].get("error")) if state == "conversion_failed" else
                                         bool(inventory[int(b["id"])].get("error")) if state == "failed" else
                                         inventory[int(b["id"])]["status"] == state)]
        sort = options.get("sort", "id")
        if sort not in {"id", "title", "size"}:
            raise ValueError("sort must be id, title or size")
        key = (lambda b: search_key(b.get("title", ""))) if sort == "title" else (lambda b: int(b.get("file_size") or 0)) if sort == "size" else (lambda b: int(b["id"]))
        return sorted(books, key=key)

    def summary(self):
        inventory = self.inventory()
        counts = Counter(local["status"] for local in inventory.values())
        return {"total": len(self.manager.books), "downloaded": sum(x.get("downloaded", False) for x in inventory.values()),
                "converted": counts["converted"], "convertible": counts["convertible"], "unchecked": counts["unchecked"],
                "unsupported": counts["unsupported"], "not_downloaded": counts["not_downloaded"],
                "failed": sum(bool(x.get("error")) for x in inventory.values()),
                "original_bytes": sum(x.get("ghm_bytes", 0) for x in inventory.values()),
                "epub_bytes": sum(x.get("epub_bytes", 0) for x in inventory.values()),
                "catalog_bytes": sum(int(b.get("file_size") or 0) for b in self.manager.books),
                "disk_free_bytes": shutil_disk(self.manager.data_dir),
                "languages": dict(Counter(b.get("lang") or "und" for b in self.manager.books)),
                "downloads_directory": str(self.manager.downloads), "epubs_directory": str(self.manager.epubs)}

    def record(self, book, action, result=None, error=None, incompatible=False):
        book_id = int(book["id"])
        try:
            source = self.manager.local_source(book)
            stamp = fingerprint(source)
        except (FileNotFoundError, ValueError):
            stamp = None
        with self.manager.db() as db:
            old = db.execute("SELECT * FROM library_state WHERE book_id=?", (book_id,)).fetchone()
            current = old and old["fingerprint"] == stamp
            compatible = old["compatible"] if current else None
            checked_at = old["checked_at"] if current else None
            details = json.loads(old["details"]) if current else {}
            if incompatible:
                compatible, checked_at = 0, utc_now()
            if result is not None:
                proof = result.get("epub", result)
                if action in {"convert", "check"} or "epub" in result:
                    compatible, checked_at = 1, utc_now()
                    details = {k: proof[k] for k in ("title", "language", "characters", "source_sha256") if k in proof}
                    details["missing_media_count"] = len(proof.get("missing_media", []))
            db.execute("""INSERT INTO library_state(book_id,fingerprint,compatible,checked_at,details,action,error)
                          VALUES(?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                          fingerprint=excluded.fingerprint,compatible=excluded.compatible,checked_at=excluded.checked_at,
                          details=excluded.details,action=excluded.action,error=excluded.error""",
                       (book_id, stamp, compatible, checked_at, json.dumps(details), action, str(error)[:3000] if error else ""))
        self.invalidate()


def utc_now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def shutil_disk(path):
    import shutil
    return shutil.disk_usage(path).free
