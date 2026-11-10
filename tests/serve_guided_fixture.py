"""Serve 105 local test books for browser QA; never contacts the book server."""
import argparse
from pathlib import Path
import sys
import tempfile
import time
from collections import Counter
import io
import urllib.error
from email.message import Message

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_guided import sample_books, sample_download
import ghbook
from recovery_api import serve
from download_diagnostics import response_failure
from test_recovery import Response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--errors", action="store_true", help="Simulate publisher removal and one rate limit")
    args = parser.parse_args()
    books = sample_books(105)
    ghbook.catalog = lambda refresh=False: books
    attempts = Counter()

    def download(book_id, output, book, resume=True, cancel=None, **kwargs):
        attempts[book_id] += 1
        if args.errors and book_id == 7:
            raise response_failure(200, {"Content-Type": "text/html"}, 'فایل مورد نظر به درخواست ناشرو مولف محترم از سایت برداشته شده است!')
        if args.errors and book_id == 8 and attempts[book_id] == 1:
            headers = Message()
            headers['Retry-After'] = '3'
            raise urllib.error.HTTPError('https://example.test/book', 429, 'Too many requests', headers, io.BytesIO(b'Too many requests'))
        progress = kwargs.get('progress')
        if progress:
            progress({'phase': 'connecting'})
        for _ in range(8):
            if cancel and cancel():
                raise InterruptedError("Job paused")
            time.sleep(0.015)
            if progress:
                progress({'phase': 'downloading', 'bytes': (_ + 1) * 50, 'total': 400, 'delta': 50})
        return sample_download(book_id, output, book, resume, cancel)

    ghbook.download = download
    if args.errors:
        ghbook.request = lambda url, headers=None: Response('فایل مورد نظر به درخواست ناشرو مولف محترم از سایت برداشته شده است!'.encode(), 200, Content_Type='text/html')
    with tempfile.TemporaryDirectory(prefix="revive-guided-qa-") as directory:
        serve(args.port, directory)


if __name__ == "__main__":
    main()
