# Recovery findings — 2026-11-10

The requested installation, APK decompilation and download API recovery work.
The recovered Python client supports catalog refresh, local search, metadata,
download URL generation and book downloads. It downloads selected IDs, not the
entire library automatically.

## Identity and installation

- Input: `Myghbook.apk`, 24,446,045 bytes.
- APK SHA-256: `95a3838f2a8ad5d7bf425f5fd73b1f7734d66710f7f81cbf4bc6c24e35d64cc2`.
- Manifest: `ir.ghbook.reader`, version name `11.02`, version code `98`,
  minimum SDK 16, target SDK 31.
- Source: <https://github.com/morluto/rea>, checkout
  `c52a9b9d79021d4799c478d73a3fd3ed795b2318` in `rea/`.
- Runnable npm package: `rea-agents@6.8.0`, pinned in `.tools/package-lock.json`.
- Project runtime: Node `24.22.1`; existing Java `21.0.12`.
- Engine: `jadx-headless-mcp-0.7.1-all.jar`, SHA-256
  `6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75`.
  Downloaded from the release linked in REA's Android guide and verified against
  its pinned digest before execution.
- `./rea.sh doctor --provider jadx --json` reports `healthy: true`.
- Optional Frida was omitted. No global MCP/agent configuration was changed.

## What the APK establishes

1. `analysis/decompiled/resources/res/values/strings.xml`, `base_url`, specifies
   `http://www.ghbook.ir/api/GetBaseUrl.php?id=1`.
2. `p0.a.c()` appends a timestamp; `p0.a.i()` extracts the library ID from that
   URL, which is `1` for this APK.
3. `p0.a.b.g()` consumes `BookList_Zip`, `Topics_Url`, `Download_Url` and image
   base URLs from the bootstrap JSON.
4. `com.ghbook.reader.gui.logic.r.o(String filename, String id)` builds:

   ```text
   https://download.ghbook.ir/downloads.php
     ?id=BOOK_ID
     &file=FILENAME.ghm
     &type=ghm
     &platform=android
     &packagename=ir.ghbook.reader
     &libraryid=1
   ```

5. `ir.ghbook.reader.ScrollingActivity.x()` requests
   `https://www.ghbook.ir/api/book_meta_info.php?desc=1&id=BOOK_ID`.
   The returned `ghm_file_name` supplies the filename for downloading.
6. `com.ghbook.reader.gui.logic.r.s()` imports `books.txt` as CSV, establishing
   the column mapping used in the Python client. Title, author, filename,
   filesize, language, price and publisher information are available offline.

REA package evidence:
`ev_c99efa57d17f1a1021b856aa6d35e64269d1d62d86e474151f3dcff9bc78a38a`
in `analysis/rea-package.json`.

REA download-method evidence:
`ev_6fb7bf603cf5511dc11a6f8c66f8eeac5546a9a5da16278f626b245cca84bcc0`
in `analysis/rea-download-method.json`. Its Java body has complete coverage,
no fallback and no missing-body markers.

## Live observations

- HTTPS bootstrap: HTTP 200, version `423`.
- Advertised catalog ZIP: HTTP 200/206; 1,450,735 compressed bytes, valid ZIP
  containing `books.txt`, `subjects.txt`, `baseurl.txt`; 24,646 book rows parsed.
- Book `12881` metadata: HTTP 200 and valid JSON, including filename
  `12881-fa-quran-sureh-tarjomeh-makarem.ghm`, price `0`, size `3412144`.
- The ID-only download request returned HTTP 400. Supplying the parameters
  recovered from `r.o()` returned HTTP 200 with an octet-stream book response;
  explicit byte ranges returned HTTP 206 with correct `Content-Range` values.
- The Python client downloaded free book `5992` completely: 14,256 bytes,
  SHA-256 `e12e4f02d34c21a74782e943f6553a3dbbed6b0c4f969630f0c34414735d3709`.
  Its receipt is next to the book under `downloads/`.
- The final range-enabled client also downloaded book `12881` completely:
  3,412,144 bytes, matching the metadata and HTTP byte-range total. Its SHA-256
  is `8daa26778d4e5f8f919a2b26f7360b96fe24e1b8da2043c3fbc1d8e15d19b0f6`.
- Final-client catalog refresh succeeded and parsed all 24,646 entries. Local
  Persian and English title searches returned results. Checks also verified
  quoted CSV fields, malformed-row rejection, multipart byte identity,
  streaming fallback, HTML-response rejection and truncated-range rejection.
- Single large response bodies stalled during probes. The client uses up to
  four bounded 128 KiB range requests, validates their offsets and lengths,
  and assembles them in order. It falls back to streaming if a server ignores
  `Range` and returns HTTP 200.

The two default APK assets were also extracted unchanged to `downloads/bundled/`:
book `12881` (3,412,352 bytes) and book `13714` (7,023,040 bytes).
The bundled copy of `12881` differs in size from the current server version;
they are kept separately. See `analysis/bundled-books.json` for their hashes.

## Scope and limits

The full export attempted all classes and resources and wrote 3,670 Java source
files. JADX recorded 20 errors and 34 warnings. Thus this is a useful recovered
source tree, not an assertion that every method decompiled successfully or that
the original app can be rebuilt. The relevant download method was independently
recovered through REA without such errors. Obfuscated names remain where no
original mapping file was supplied.

The client preserves `.ghm` files as received. It does not convert or decrypt
them to PDF, EPUB or plain text. No APK was launched and no emulator or device
was used. The APK's signature authenticity has not been verified.

The tested catalog, metadata and free downloads did not require credentials.
Paid-book/account authorization and the state-changing login, purchase, notes,
review and account APIs were not exercised. Static URLs for those functions
remain visible in the decompiled source; their present behavior is unknown.
