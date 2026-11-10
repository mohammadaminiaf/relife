# Ghbook APK recovery

REA 6.8.0 is installed locally. Its source checkout is in `rea/`; the runnable
npm release, Node 24.22.1 and verified JADX engine are in `.tools/`.

```sh
./rea.sh --version
./rea.sh doctor --provider jadx --json
./rea.sh inspect-android-method "$PWD/Myghbook.apk" com.ghbook.reader.gui.logic.r o --json
```

`rea.sh` selects the project runtime and the existing JDK 21. The Android CLI
works immediately. Run `./rea.sh mcp` to serve the same tools over stdio. No
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
the source URL, byte count and SHA-256. These are not PDF/EPUB conversions. The
client preserves existing files and reports server errors. It uses the same
public requests as the APK; account-specific purchases and authentication have
not been tested.

Live downloads of books `5992` and `12881` are already in `downloads/`. Two
additional original copies bundled with the APK are in `downloads/bundled/`.

The cached catalog contains 24,646 entries. Run
`python3 scripts/ghbook.py catalog --refresh` to replace it with the latest
catalog advertised by the server. `search` automatically downloads a catalog
if the local cache is absent.

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
