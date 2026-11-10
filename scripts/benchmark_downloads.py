"""Benchmark a small explicit selection in isolated storage, preserving the library."""
import argparse
import datetime
import json
from pathlib import Path
import time

import ghbook
from recovery_api import Manager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', nargs='+', type=int, default=[2, 3, 4, 5, 6, 9, 10, 11])
    parser.add_argument('--workers', type=int, choices=(1, 2, 4, 8), default=8)
    args = parser.parse_args()
    if not 1 <= len(set(args.ids)) <= 8:
        parser.error('Choose one to eight book IDs for a bounded benchmark')
    catalog = {int(book['id']): book for book in ghbook.catalog()}
    books = [catalog[book_id] for book_id in dict.fromkeys(args.ids)]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    storage = ghbook.ROOT / 'analysis/benchmarks' / stamp
    manager = Manager(books=books, data_dir=storage)
    started = time.monotonic()
    try:
        job = manager.create({'scope': 'all', 'download_workers': args.workers})
        print('Benchmarking selected books in isolated storage...', flush=True)
        manager.wait()
        report = manager.report(job['id'])
        comparisons = []
        for item in report['items']:
            result = item.get('result') or {}
            source = ghbook.ROOT / 'downloads' / catalog[item['book_id']]['file_name']
            if source.is_file() and result.get('sha256'):
                comparisons.append({'id': item['book_id'], 'matches_library_sha256': ghbook.file_hash(source) == result['sha256']})
        report['hash_comparisons'] = comparisons
        report['wall_seconds'] = time.monotonic() - started
        path = storage / 'report.json'
        ghbook.save_json(path, report)
        status = report['job']
        print(json.dumps({'state': status['state'], 'counts': status['counts'], 'telemetry': status['telemetry'],
                          'hash_comparisons': comparisons, 'report': str(path)}, ensure_ascii=True, indent=2))
        return int(status['counts']['failed'] > 0)
    finally:
        manager.close()


if __name__ == '__main__':
    raise SystemExit(main())
