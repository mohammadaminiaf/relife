"""Serve 105 local test books for browser QA; never contacts the book server."""
import argparse
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_guided import sample_books, sample_download
import ghbook
from recovery_api import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    args = parser.parse_args()
    books = sample_books(105)
    ghbook.catalog = lambda refresh=False: books

    def download(book_id, output, book, resume=True, cancel=None):
        for _ in range(8):
            if cancel and cancel():
                raise InterruptedError("Job paused")
            time.sleep(0.015)
        return sample_download(book_id, output, book, resume, cancel)

    ghbook.download = download
    with tempfile.TemporaryDirectory(prefix="revive-guided-qa-") as directory:
        serve(args.port, directory)


if __name__ == "__main__":
    main()
