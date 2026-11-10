# Ghbook APK recovery

## Start on Windows

This is a local book recovery library: a Python HTTP service, a browser UI,
persistent SQLite jobs, and a Node.js GHM-to-EPUB decoder. The web app runs
without the original APK, REA, Java, Bruno, a database server, or Python packages.

With Python 3.9+ and Node.js installed, open PowerShell in this folder:

```powershell
.\setup.cmd
.\library.cmd
```

The launcher opens <http://127.0.0.1:8765> and keeps the service running in the
background. You can also double-click `library.cmd`. First launch downloads
the catalog, so it needs internet access. Downloading originals needs access
to the book server. Browsing the cached catalog and converting saved books
work offline.

```powershell
.\library.cmd --status
.\library.cmd --stop
.\library.cmd --data-dir 'D:\Books'
```

Open **Downloads** and choose **Review next 50 downloads** to start guided
recovery, or select books in **Catalog** to download them manually. The full library is
about 62 GB before EPUBs; it is not downloaded automatically. Files default to
`downloads/` and `epubs/`; job progress is in `analysis/recovery/`. To resume
work from another laptop, stop its service and copy those three directories.
Sample books and analysis mentioned below describe the original laptop and
are not included in a fresh clone.

Windows setup checks the available Node runtime and its cryptography support.
Full APK analysis tools still use macOS, Linux or WSL and the setup below.

Main source files: `scripts/recovery_api.py` (HTTP and jobs),
`scripts/library_index.py` (search, categories and local status),
`scripts/ghbook.py` (catalog and downloads), `scripts/ghm_epub.py` and
`scripts/decode_ghm.mjs` (conversion), and `web/` (browser interface).

## Library app

### Dedicated library pages

- **Catalog** (`/catalog`): search the complete catalog, filter by language or
  category, and download one book, selected books, or a matching selection.
- **Downloaded** (`/downloaded`): browse originals as cards or a list. Convert a
  single book directly from its card, or select several and choose **Convert
  selected**. The default home page is this saved library.
- **EPUBs** (`/epubs`): browse completed EPUBs as cards and save individual copies.
  Newly generated EPUBs use their bundled cover when one is available.
- **Downloads** (`/downloads`): choose a 50/100/200/1,000/5,000-book batch, choose download
  concurrency, and watch live speed, bytes, active books, elapsed time and waits.
- **Convert to EPUB** (`/conversions`): see downloaded originals that need EPUBs,
  choose single or selected books, or confirm the next group of up to 50.
- **Activity** (`/activity`): pause/resume jobs, inspect results and save reports.
- **Issues** (`/errors`): understand failures, read the original server notice,
  inspect current server responses, or review a retry batch.
- **Settings & storage** (`/settings`): change parallel download preferences and
  open the actual originals/EPUB folders. Advanced full-catalog manual recovery
  and catalog refresh are available here.

All pages have direct URLs, browser back/forward navigation, and mobile layouts.
Manual selected downloads, download-plus-EPUB, conversion and compatibility
checks remain available alongside guided recovery.

### Guided recovery and download performance

Choose **50, 100, 200, 1,000, or 5,000 books per download batch** on Downloads or
Settings, then review and confirm the next group. The preference is saved for
future batches; existing confirmed/paused batches keep their original selection.
Conversion batches continue to use up to 50 books. The app detects originals
already on disk and completed EPUBs with matching receipts, then chooses the
next books automatically. The final group can be smaller. Guided batches use
the whole catalog; search and selection apply to manual actions.

One batch runs at a time. Download batches stream **eight books concurrently**
by default, with 1/2/4/8 options under Downloads or Settings. The preference
applies to the next batch. There is no application bandwidth cap. Actual speed
also depends on the remote server and the network path. Stalled or incomplete
streams retry with bounded 1 MB ranges, with at most two range requests per
book. Every published original retains its byte count and SHA-256 receipt.
Batch size controls how many books you confirm together, while simultaneous
downloads separately control how many transfer at once. Conversion-only batches
process local originals without network downloads.

Files and SQLite state survive browser refreshes and service restarts. Use
**Resume unfinished batch** to confirm continuation of remaining work. Deleting
a saved original or EPUB makes that work eligible again; keep originals and
receipts so completed EPUBs can be verified. Storage is the same on Windows
and macOS; **Settings & storage** shows its paths and the launcher's
`--data-dir` option chooses another drive.

### Failure diagnosis

Temporary network/server failures retry up to three times. Explicit HTTP 429
or a clear rate-limit notice is reported as **Rate limited**. The downloader
respects `Retry-After` (seconds or HTTP date), applies a shared cooldown and
reduces concurrency. It does not treat every HTML response as rate limiting.

For example, several catalog entries return HTTP 200 with a Persian notice
saying the publisher/author requested removal. These are reported as **Removed
by publisher**, with the original notice, and are not automatically retried.
Unavailability, denied access, timeouts, connection/TLS errors, full disks and
conflicting existing files have separate reasons and suggested actions.

On Issues, **Inspect server responses** makes bounded response checks for up
to 50 failed items in that job. It saves the latest HTTP evidence without
saving or replacing a book. **Save report** exports per-attempt diagnostics,
request/byte/timing totals and results. Old jobs keep their results; their
recorded span is labeled separately from newly measured active transfer time.
Failures stay aside while later guided batches continue. Explicit retry batches
and per-job retry controls remain available.

To benchmark one to eight explicit books in isolated storage:

```sh
python scripts/benchmark_downloads.py --ids 2 3 4 --workers 8
```

This downloads the selected samples into `analysis/benchmarks/`, compares their
checksums with existing library copies when available, and saves `report.json`.
It leaves the main library and jobs untouched. See `FINDINGS.md` for the measured
streaming/concurrency comparison and its limits.

Launch the browser library with `./library.sh`, or double-click
`Library.command` on macOS. It supports catalog search, category and language
filters, local download/conversion status, format checks, batch previews,
downloads and EPUB export. Jobs and completed files persist across restarts.

For the library alone, run `./setup.sh --app-only` after cloning (requires
Python 3.9+ and Node.js with npm). Use `./library.sh --data-dir /path/to/storage`
to keep books and progress on another drive. Use `./library.sh --status` to
inspect the service and `./library.sh --stop` to pause work and stop it.
If an older recovery service occupies port 8765, stop that service first or
choose another port with `./library.sh --port 8767`.

Checkpoint on November 10, 2026: the library interface, persistent jobs,
guided download/conversion queues, conversion checks and setup scripts are
implemented; all 37 Python tests pass. Browser QA covers dedicated pages,
card libraries, both guided queues, parallel downloads, rate limiting, failure
inspection, confirmation, pause/resume, manual controls and mobile layouts. Books and local job databases
are excluded from Git as described below. No full-catalog job starts on launch.

## Bruno API

The [Bruno collection](bruno/README.md) lists the full catalog, downloads by ID,
starts a full backup, tracks persistent jobs, and converts recovered GHM files
to EPUB 3.

```sh
./start-recovery.sh
```

Open `bruno/` with **Open Collection** in Bruno and select **Local**. Send
**Catalog / List all books**, then **Downloads / Download all books** to begin
the full backup. The catalog has 24,646 entries totaling about 62 GB. Set
`convert_epub=true` to create EPUBs alongside the originals.

The service is local and must stay running during a job. Progress survives a
restart; use **Jobs / Resume or retry job** to continue. See the collection's
README for all requests, filters, result states and storage behavior.

## EPUB conversion

The converter reads the GHM container using the algorithm recovered from the
APK. It preserves Unicode text, RTL direction, headings, navigation, covers,
bundled images and visible annotations. Missing external media is reported.
Original GHM files are retained.

```sh
python3 scripts/ghm_epub.py downloads/5992-ar-menhaj-alimam-alhossein.ghm
python3 scripts/ghm_epub.py downloads/*.ghm --output-dir epubs
```

Four sample EPUBs are already in `epubs/`, each validated with EPUBCheck 5.4.0
with zero errors and warnings. Their receipts record source hashes and missing
media. Validation of these samples does not establish support for every book
variant or identical rendering in the updated application.

For a standalone bulk download with the local service stopped:

```sh
python3 scripts/ghbook.py download-all --plan
python3 scripts/ghbook.py download-all --with-epub
python3 scripts/ghbook.py download-all --lang en --limit 10
```

## REA installation

REA's source is tracked in `rea/` as a Git submodule, pinned to the revision
used for this recovery. GitHub shows it as a link to that upstream revision.
The project also tracks lockfiles for the runnable REA release and Bruno CLI.

On another macOS or Linux laptop, install Git, Python 3.9+, Node.js with npm,
and a full JDK 17 or newer. Then run:

```sh
git clone https://github.com/mohammadaminiaf/relife.git
cd relife
./setup.sh
./rea.sh doctor --provider jadx --json
./start-recovery.sh
```

`setup.sh` initializes the pinned REA submodule, installs the locked Node
24.22.1/REA 6.8.0 and Bruno packages locally, and downloads the matching JADX
engine with SHA-256 verification. It leaves global agent configuration alone.
It initializes only REA itself; the optional upstream provider submodules are
not required for this APK workflow. Set `JAVA_HOME` to select an existing JDK,
especially on Linux. The setup does not install a system JDK or Node.js.

For an existing clone:

```sh
git pull
./setup.sh
```

Use `./setup.sh --check` to verify an installation without reinstalling it.
To fetch just REA's source, run `git submodule update --init -- rea`.

Git does not transfer the APK, downloaded books, EPUBs or `analysis/` job state.
Copy `Myghbook.apk`, `downloads/`, `epubs/` and `analysis/` separately if you want
to resume this laptop's existing recovery on another machine. Stop the local
service before copying its SQLite job database. Without those files, the
scripts can fetch a new catalog and start a new download job.

```sh
./rea.sh --version
./rea.sh doctor --provider jadx --json
./rea.sh inspect-android-method "$PWD/Myghbook.apk" com.ghbook.reader.gui.logic.r o --json
```

`rea.sh` selects the project runtime and an existing JDK (preferring JDK 21 on
macOS when `JAVA_HOME` is unset). Run `./rea.sh mcp` to serve the same tools over stdio. No
global agent configuration was changed. Optional Frida was omitted; static APK
analysis does not need it.

## Find and download books

The recovered client needs only Python 3's standard library:

```sh
python3 scripts/ghbook.py catalog
python3 scripts/ghbook.py search "قرآن" --lang fa --limit 10
python3 scripts/ghbook.py search "Quran" --lang en
python3 scripts/ghbook.py info 12881
python3 scripts/ghbook.py url 12881
python3 scripts/ghbook.py download 4696
python3 scripts/ghbook.py download 13714 4977 --output ./my-books
```

Downloads keep the app's original `.ghm` format and include a JSON receipt with
the source URL, byte count and SHA-256. Use `ghm_epub.py` or Bruno to create EPUBs. The
client preserves existing files and reports server errors. It uses the same
public requests as the APK; account-specific purchases and authentication have
not been tested.

Live downloads of books `5992` and `12881` are already in `downloads/`. Two
additional original copies bundled with the APK are in `downloads/bundled/`.

The cached catalog contains 24,646 entries. Run
`python3 scripts/ghbook.py catalog --refresh` to replace it with the latest
catalog advertised by the server. `search` automatically downloads a catalog
if the local cache is absent.

The default book-download command preserves existing files. Add `--resume` to
skip files after verifying their receipt and SHA-256. Bulk jobs do this
automatically and record failures for later retries.

## Verification

```sh
python3 -m unittest discover -s tests -v
./bru.sh run '01 Catalog' --env Local
```

For repeatable browser QA of the guided flow, with the local Playwright tools
installed, run `python tests/serve_guided_fixture.py --errors`, then in another terminal
run `node tests/guided-browser.mjs`. This uses 105 generated local books and
simulated rate limits/publisher removal, exercises both queues, page navigation,
card libraries and pause/resume, and does not download real books.
`node tests/library-browser.mjs` runs read-only browser smoke checks against the
full local library. Screenshots and benchmark results stay under `analysis/`.

The Python tests cover transfer integrity, corrupt-file preservation, bulk
failure handling, pause/resume, restart recovery, HTTP endpoints and EPUB content.
Live Bruno tests and EPUBCheck reports are retained under `analysis/`.

Books, APKs, installed tools, caches, job state and generated EPUBs are ignored
by Git. REA's source revision is tracked through `.gitmodules` and the `rea`
submodule; `setup.sh` restores the installed tools from their pins.

## Recovered endpoints

| Purpose | Request |
| --- | --- |
| Bootstrap / current catalog URL | `GET https://www.ghbook.ir/api/GetBaseUrl.php?id=1` |
| Catalog observed during recovery | `GET https://www.ghbook.ir/api/BookListFiles/bookfile_1.zip?v=423` |
| Book metadata, including filename | `GET https://www.ghbook.ir/api/book_meta_info.php?desc=1&id=12881` |
| Download | `GET https://download.ghbook.ir/downloads.php?id=12881&file=12881-fa-quran-sureh-tarjomeh-makarem.ghm&type=ghm&platform=android&packagename=ir.ghbook.reader&libraryid=1` |

The bootstrap response contains `BookList_Zip`, `Topics_Url`, `Download_Url`,
`Download_New_Url` and cover-image URLs. Its old HTTP links also work over HTTPS;
the client uses HTTPS. Catalog ZIP members are `books.txt` (CSV), `subjects.txt`
and `baseurl.txt`.

## Decompiled code and evidence

- `analysis/decompiled/sources/`: 3,670 exported Java files.
- `analysis/decompiled/resources/`: decoded manifest and Android resources.
- `analysis/decompile.log`: export diagnostics; JADX reported 20 errors and 34
  warnings, so some unrelated methods need further work.
- `analysis/rea-package.json`: REA package inspection with artifact identity.
- `analysis/rea-download-method.json`: complete recovered download method.
- `analysis/bootstrap.json`, `analysis/catalog.zip`, `analysis/catalog.json`:
  live configuration and catalog snapshots.
- `downloads/bundled/`: both original books extracted from APK assets.
- `FINDINGS.md`: provenance, validation and limits.

The APK is `ir.ghbook.reader`, version 11.02 (98). Decompiled Java is a recovered
representation, not the original source project or a guaranteed buildable app.

To repeat the full Java/resource export:

```sh
"$(/usr/libexec/java_home -v 21)/bin/java" -Xmx3g -XX:ActiveProcessorCount=4 \
  -cp .tools/jadx-headless-mcp-0.7.1-all.jar \
  scripts/ExportApk.java Myghbook.apk analysis/decompiled-new
```

For batch-size browser QA, run `python tests/serve_guided_fixture.py --count 5205`,
then `node tests/batch-size-browser.mjs`. It previews the larger options and
confirms a generated 100-book batch without downloading real books.
