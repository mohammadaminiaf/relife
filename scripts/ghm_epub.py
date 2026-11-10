#!/usr/bin/env python3
"""Convert the supplied app's GHM container to a reflowable EPUB 3 publication."""
import argparse
from contextlib import contextmanager
import datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from app_runtime import node_runtime

ROOT = Path(__file__).resolve().parents[1]
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
MEDIA = re.compile(r"!\[(pagebreak|image|audio|video)\]\(([^)]*)\)")
HEADING = re.compile(r"^(#{1,6})\s*(.+)$")


def clean(text):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]", "", text)


def add_text(node, text):
    if len(node):
        node[-1].tail = (node[-1].tail or "") + text
    else:
        node.text = (node.text or "") + text


class Inline(HTMLParser):
    """Keep basic formatting and make the app's pop-up reference text visible."""
    allowed = {"b", "strong", "i", "em", "u", "s", "sup", "sub", "span", "br", "img", "audio", "video"}
    void = {"br", "img"}

    def __init__(self, parent):
        super().__init__(convert_charrefs=True)
        self.stack = [("root", parent, None)]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in self.allowed and tag != "meta":
            return
        note = attrs.get("info") if tag == "meta" and attrs.get("type") == "ref" else None
        safe = {}
        if tag in {"img", "audio", "video"}:
            src = attrs.get("src", "")
            if not src.startswith("assets/"):
                return
            safe["src"] = src
            if tag == "img":
                safe["alt"] = attrs.get("alt", "Image")
            else:
                safe["controls"] = "controls"
        if attrs.get("class") in {"page-marker", "missing-media", "annotation"}:
            safe["class"] = attrs["class"]
        node = ET.SubElement(self.stack[-1][1], "span" if tag == "meta" else tag, safe)
        if tag not in self.void:
            self.stack.append((tag, node, note))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == tag:
                for _, node, note in self.stack[index:]:
                    if note:
                        ET.SubElement(node, "span", {"class": "note"}).text = " (" + clean(note) + ")"
                del self.stack[index:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_data(self, data):
        add_text(self.stack[-1][1], clean(data))


def document(title, lang, direction):
    root = ET.Element("html", {"xmlns": XHTML, "xmlns:epub": EPUB, "lang": lang,
                              "{http://www.w3.org/XML/1998/namespace}lang": lang, "dir": direction})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "link", {"rel": "stylesheet", "type": "text/css", "href": "style.css"})
    return root, ET.SubElement(root, "body")


def xml(node):
    return ET.tostring(node, encoding="utf-8", xml_declaration=True)


def asset_type(name, data):
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    mime = mimetypes.guess_type(name)[0]
    # Foreign media need EPUB fallbacks; report them instead of producing invalid packages.
    if mime in {"image/jpeg", "image/png", "image/gif", "audio/mpeg", "audio/mp4"}:
        return Path(name).suffix.lower(), mime
    return None


def validate(path):
    with zipfile.ZipFile(path) as archive:
        first = archive.infolist()[0]
        if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED or archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype entry")
        if archive.testzip() is not None:
            raise ValueError("EPUB ZIP integrity failure")
        for name in archive.namelist():
            if name.endswith((".xml", ".opf", ".xhtml")):
                ET.fromstring(archive.read(name))
        package = ET.fromstring(archive.read("OEBPS/content.opf"))
        manifest = package.find("{" + OPF + "}manifest")
        entries = {item.attrib["id"]: item for item in manifest}
        for item in manifest:
            if "OEBPS/" + item.attrib["href"] not in archive.namelist():
                raise ValueError("Missing EPUB resource: " + item.attrib["href"])
        for item in package.find("{" + OPF + "}spine"):
            if item.attrib["idref"] not in entries:
                raise ValueError("Missing EPUB spine item")


class UnsupportedGhm(ValueError):
    """The local container cannot be read by this version of the recovered reader."""


@contextmanager
def opened_ghm(source):
    runtime = node_runtime()
    if not runtime:
        raise RuntimeError("Node.js is required to decode GHM; use the project installation")
    with tempfile.TemporaryDirectory(prefix="ghm-read-") as temporary:
        decoded = Path(temporary) / "book.zip"
        if zipfile.is_zipfile(source):
            shutil.copyfile(source, decoded)
        else:
            result = subprocess.run([runtime, str(ROOT / "scripts/decode_ghm.mjs"), str(source), str(decoded)],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode:
                raise UnsupportedGhm("Cannot decode this GHM version: " + result.stderr[-1200:])
        with zipfile.ZipFile(decoded) as archive:
            names = {}
            for original in archive.namelist():
                name = original.replace("\\", "/")
                if name.startswith("/") or ".." in PurePosixPath(name).parts:
                    raise UnsupportedGhm("Unsafe archive member path")
                if name in names:
                    raise UnsupportedGhm("Duplicate archive member: " + name)
                names[name] = original
            info = ET.fromstring(archive.read(names["bookinfo.xml"]))
            if info.tag != "bookinfo":
                info = info.find(".//bookinfo")
            if info is None:
                raise UnsupportedGhm("Missing GHM book metadata")
            meta = {item.tag: clean(item.text or "").strip() for item in info}
            text = clean(archive.read(names["out.xml"]).decode("utf-8-sig"))
            if not text.strip():
                raise UnsupportedGhm("Book has no text content")
            yield archive, names, meta, text


def inspect_book(source):
    source = Path(source).resolve()
    try:
        with opened_ghm(source) as (archive, names, meta, text):
            bad = archive.testzip()
            if bad:
                raise UnsupportedGhm("Archive integrity check failed: " + bad)
            missing = sorted({name.replace("\\", "/").split("|", 1)[0].strip()
                              for kind, name in MEDIA.findall(text)
                              if kind != "pagebreak" and name.replace("\\", "/").split("|", 1)[0].strip() not in names})
            return {"compatible": True, "id": meta.get("booknumber"), "title": meta.get("title"),
                    "language": meta.get("language", "und").lower(), "characters": len(text),
                    "members": len(names), "missing_media": missing,
                    "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    except (zipfile.BadZipFile, ET.ParseError, UnicodeError, KeyError) as error:
        raise UnsupportedGhm(str(error)) from error


def convert(source, output=None):
    source = Path(source).resolve()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    source_hash = digest.hexdigest()
    output = Path(output) if output else ROOT / "epubs" / (source.stem + ".epub")
    if output.exists():
        receipt = output.with_suffix(".epub.json")
        if receipt.exists():
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            if previous.get("source_sha256") == source_hash and previous.get("sha256") == hashlib.sha256(output.read_bytes()).hexdigest():
                validate(output)
                return {**previous, "source": str(source), "path": str(output.resolve()), "skipped": True}
        raise FileExistsError(f"Existing EPUB does not match this source: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ghm-epub-") as temporary:
        with opened_ghm(source) as (archive, names, meta, text):
            lang = meta.get("language", "").lower() or "und"
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", lang):
                lang = "und"
            direction = "rtl" if lang.split("-")[0] in {"ar", "fa", "ur", "he"} else "ltr"
            title = meta.get("title") or source.stem
            assets, media_map, missing = {}, {}, set()
            cover = None
            for name, original in names.items():
                if name == "pic" or name.startswith("resources/") and not name.endswith("/"):
                    data = archive.read(original)
                    kind = asset_type(name, data)
                    if kind:
                        suffix, mime = kind
                        path = "assets/" + hashlib.sha256(data).hexdigest()[:24] + suffix
                        assets[path] = (data, mime)
                        media_map[name] = path
                        if name == "pic":
                            cover = path

            def media(match):
                kind, name = match.groups()
                if kind == "pagebreak":
                    return '<span class="page-marker">' + html.escape(name.strip()) + '</span>'
                normalized = name.replace("\\", "/").split("|", 1)[0].strip()
                path = media_map.get(normalized)
                if not path:
                    missing.add(normalized)
                    return '<span class="missing-media">[' + html.escape(kind + ": " + normalized) + ']</span>'
                if kind == "image":
                    return '<img src="' + path + '" alt="' + html.escape(normalized, quote=True) + '"/>'
                return '<' + kind + ' src="' + path + '" controls="controls"></' + kind + '>'

            sections = []
            current = {"title": title, "level": 1, "lines": [], "length": 0}
            for line in text.splitlines():
                heading = HEADING.match(line)
                if heading or current["length"] > 120000:
                    if current["lines"]:
                        sections.append(current)
                    current = {"title": heading.group(2).strip() if heading else current["title"] + " (continued)",
                               "level": len(heading.group(1)) if heading else current["level"], "lines": [], "length": 0}
                current["lines"].append(line)
                current["length"] += len(line)
            if current["lines"]:
                sections.append(current)
            if not sections:
                raise ValueError("Book has no text content")
            documents = {}
            if cover:
                root, body = document(title + " — Cover", lang, direction)
                ET.SubElement(body, "img", {"src": cover, "alt": title})
                documents["cover.xhtml"] = xml(root)
            for index, section in enumerate(sections, 1):
                root, body = document(section["title"], lang, direction)
                for line in section["lines"]:
                    if not line.strip():
                        continue
                    heading = HEADING.match(line)
                    node = ET.SubElement(body, "h" + str(len(heading.group(1))) if heading else "p")
                    inline = Inline(node)
                    inline.feed(MEDIA.sub(media, heading.group(2) if heading else line))
                    inline.close()
                section["href"] = f"chapter-{index:04d}.xhtml"
                documents[section["href"]] = xml(root)
            root, body = document("Contents", lang, direction)
            nav = ET.SubElement(body, "nav", {"epub:type": "toc", "id": "toc"})
            ET.SubElement(nav, "h1").text = "Contents"
            listing = ET.SubElement(nav, "ol")
            for section in sections:
                ET.SubElement(ET.SubElement(listing, "li"), "a", {"href": section["href"]}).text = section["title"]
            documents["nav.xhtml"] = xml(root)
            package = ET.Element("package", {"xmlns": OPF, "xmlns:dc": DC, "version": "3.0", "unique-identifier": "book-id"})
            metadata = ET.SubElement(package, "metadata")
            ET.SubElement(metadata, "dc:identifier", {"id": "book-id"}).text = "urn:ghbook:" + (meta.get("booknumber") or source_hash)
            for key, value in [("title", title), ("language", lang), ("creator", meta.get("author", "")), ("description", meta.get("description", ""))]:
                if value:
                    ET.SubElement(metadata, "dc:" + key).text = value
            ET.SubElement(metadata, "meta", {"property": "dcterms:modified"}).text = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            manifest = ET.SubElement(package, "manifest")
            ET.SubElement(manifest, "item", {"id": "style", "href": "style.css", "media-type": "text/css"})
            for index, (name, data) in enumerate(documents.items()):
                attrs = {"id": "doc-" + str(index), "href": name, "media-type": "application/xhtml+xml"}
                if name == "nav.xhtml":
                    attrs["properties"] = "nav"
                ET.SubElement(manifest, "item", attrs)
            for index, (name, (_, mime)) in enumerate(assets.items()):
                attrs = {"id": "asset-" + str(index), "href": name, "media-type": mime}
                if name == cover:
                    attrs["properties"] = "cover-image"
                ET.SubElement(manifest, "item", attrs)
            spine = ET.SubElement(package, "spine", {"page-progression-direction": direction})
            for index, name in enumerate(documents):
                if name != "nav.xhtml":
                    ET.SubElement(spine, "itemref", {"idref": "doc-" + str(index)})
            result_path = Path(temporary) / "output.epub"
            with zipfile.ZipFile(result_path, "w", zipfile.ZIP_DEFLATED) as epub:
                epub.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                epub.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
                epub.writestr("OEBPS/content.opf", xml(package))
                epub.writestr("OEBPS/style.css", "body { line-height: 1.7; margin: 1em; } p { white-space: pre-wrap; } img { max-width: 100%; height: auto; } .note { font-size: 0.9em; } .page-marker { color: #666; font-size: 0.8em; } .missing-media { color: #777; }")
                for name, data in documents.items():
                    epub.writestr("OEBPS/" + name, data)
                for name, (data, _) in assets.items():
                    epub.writestr("OEBPS/" + name, data)
            validate(result_path)
            # Stage in the destination filesystem so publishing works across mount points.
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".epub.part", delete=False) as staging:
                stage = Path(staging.name)
                with result_path.open("rb") as data:
                    shutil.copyfileobj(data, staging)
            try:
                os.link(stage, output)
            finally:
                stage.unlink(missing_ok=True)
    receipt = {"id": meta.get("booknumber"), "title": title, "language": lang,
               "source": str(source), "source_sha256": source_hash, "path": str(output.resolve()),
               "bytes": output.stat().st_size, "mtime_ns": output.stat().st_mtime_ns,
               "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
               "chapters": len(sections), "assets": len(assets), "cover": bool(cover), "missing_media": sorted(missing),
               "format": "EPUB 3", "skipped": False}
    output.with_suffix(".epub.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "epubs")
    args = parser.parse_args()
    for source in args.sources:
        print(json.dumps(convert(source, args.output_dir / (source.stem + ".epub")), ensure_ascii=False, indent=2))
