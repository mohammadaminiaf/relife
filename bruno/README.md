# Ghbook Recovery — Bruno collection

Open this `bruno/` folder with **Open Collection** in Bruno, then choose the
**Local** environment. The full project is a local Git repository; keep the
parent `scripts/` directory alongside this collection.

On a fresh clone, run `./setup.sh` from the project root first. This restores
REA's pinned source submodule and the locally installed tools. See
[setup on another laptop](../README.md#rea-installation) for prerequisites and
instructions for copying existing books and recovery progress.

Start the local recovery service from the project root:

```sh
./start-recovery.sh
```

The service listens on `http://127.0.0.1:8765`. Python 3.9+ handles the catalog
and downloads; Node.js handles GHM decoding for EPUB. Both are already available
in this project. No emulator, Android device, REA session or Bruno Developer
Mode is required for these collection requests.

## Requests to use

| Task | Bruno request |
| --- | --- |
| Return every book as JSON | `01 Catalog / 02 List all books` |
| Browse one page | `01 Catalog / 03 List books page` |
| Search titles, authors, filenames or IDs | `01 Catalog / 04 Search books` |
| Inspect a book and its original download URL | `01 Catalog / 05 Book by ID` |
| Check the full backup's size and local disk space | `01 Catalog / 06 Plan full recovery` |
| Download the `book_id` environment value | `02 Downloads / 01 Download by ID` |
| Download a list of IDs | Edit the JSON in `02 Downloads / 02 Download selected IDs` |
| Start the full catalog backup | **`02 Downloads / 03 Download all books`** |
| Download a smaller language selection | `02 Downloads / 04 Download language batch` |
| Track progress or retrieve per-book results | Requests in `03 Jobs` |
| Convert one downloaded book | `04 EPUB / 01 Convert downloaded book by ID` |
| Convert all locally available catalog books | `04 EPUB / 02 Convert entire downloaded library` |
| Retrieve an already-created EPUB | `04 EPUB / 03 Get EPUB file` |

Send download/conversion actions individually. They return HTTP **202** and a
persistent job ID immediately. The post-response script saves `job_id` in the
selected environment. Send **Job progress** again to see current counts and the
active book. Only one job runs at a time.

## Download the whole library

1. Send **Plan full recovery**. The current snapshot contains **24,646 entries**
   and approximately **62 GB** of GHM originals; EPUBs need additional space.
2. Set `convert_epub` to `true` to create EPUBs as each book finishes, or leave
   it `false` to back up the originals first.
3. Send **Download all books**. This processes the entire catalog, across all
   languages. The language and batch-limit variables only affect the separate
   **Download language batch** request.
4. Use **Job progress**, **Job results** and **Failed books** as needed. Closing
   Bruno does not stop the Python service or its job.

Files are saved to `../downloads/`; EPUBs are saved to `../epubs/`. Each file
gets a JSON receipt with its source and checksum. Existing GHM files are skipped
only after their size and SHA-256 match their receipt and the catalog. Conflicts
are reported without replacing files.

**Pause job** preserves completed work. **Resume or retry job** continues pending
books and retries failures. If the service or computer stops, start the service,
use **List jobs** to recover the job ID, and send **Resume or retry job**.
An interrupted individual book restarts from its beginning; completed verified
books are retained. Progress lives in `../analysis/recovery/jobs.sqlite3`.

Transient network failures get up to three attempts. Unavailable or refused
books are recorded and the remaining books continue. A final
`completed_with_errors` state means **Failed books** still needs attention. The
catalog contains four priced entries; the downloader does not bypass any server
authorization requirement. This catalog is library `1` from the supplied APK,
not a recovered personal account purchase/history list.

## Variables

| Variable | Default / purpose |
| --- | --- |
| `book_id` | `5992`; change it to download or convert another book |
| `convert_epub` | `false`; use literal `true` to convert after downloading |
| `query` | Search term |
| `language` | `fa`; used by search and the language-batch request |
| `page`, `page_size` | Pagination; normal page sizes are 1–1000 |
| `batch_limit` | `3`; positive count for the language-batch request |
| `job_id` | Filled by a job-creation response; can also be copied from List jobs |
| `api_url` | Local service URL; change the port if starting with `--port` |

The **Get downloaded GHM file** and **Get EPUB file** requests return binary
attachments, which can also be saved using Bruno's response-save control. The
job requests have already written these files to disk.

## EPUB fidelity

The converter ports the supplied APK's GHM container reader and builds EPUB 3
with Unicode text, headings, contents navigation, cover images, bundled images,
page labels and RTL direction. The app's pop-up `ref` notes become visible inline
notes. The original GHM file remains unchanged.

Media absent from the GHM archive (including externally downloaded audio) is
listed in the `.epub.json` receipt and shown as a placeholder. It is not fetched
automatically. Unknown GHM variants fail explicitly. EPUB validation does not
prove identical layout or support for every format variant in the full catalog.

The four converted sample books have passed EPUBCheck 5.4.0. See
`../analysis/epubcheck-*.json` for individual reports.

## Direct upstream requests

`05 Remote API` contains bootstrap, metadata, catalog ZIP and download requests
that work without the local service. Send **Metadata by ID** before **Download
GHM** after changing `book_id`; its response supplies the matching filename.
Send **Bootstrap** to refresh the advertised ZIP URL. These are the recovered
upstream endpoints. The local service provides JSON listing, verified on-disk
downloads, persistence and conversion around them.

## CLI checks

The pinned local Bruno CLI is available through `../bru.sh`:

```sh
# From the project root; these requests only read data.
./bru.sh run '01 Catalog' --env Local

# Send one selected download action.
./bru.sh run '02 Downloads/01 Download by ID.bru' --env Local \
  --env-var book_id=5992 --env-var convert_epub=true
```

Bruno's file format and scripting references are available in the
[official documentation](https://docs.usebruno.com/).
