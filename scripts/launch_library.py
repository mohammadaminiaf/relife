#!/usr/bin/env python3
"""Launch the local library, reuse its background service, or pause and stop it."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def health(url):
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, help="Folder for downloads, EPUBs and progress; defaults to the project")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop", action="store_true", help="Pause the running job and stop this project's background service")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be 1..65535")
    data_dir = (args.data_dir or ROOT).expanduser().resolve()
    url = f"http://127.0.0.1:{args.port}"
    running = health(url)
    if running and (running.get("app") != "revive-library" or running.get("root") != str(ROOT)):
        print("A different or older service is using this port. Stop it first, or choose --port 8766.", file=sys.stderr)
        return 1
    if args.status:
        print(json.dumps(running or {"status": "stopped", "url": url}, indent=2))
        return 0
    if args.stop:
        if running:
            control = Path(running["data_dir"]) / "analysis/recovery/service-control.json"
            try:
                token = json.loads(control.read_text(encoding="utf-8"))["token"]
                request = urllib.request.Request(url + "/shutdown", data=json.dumps({"token": token}).encode(),
                                                 headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=5):
                    pass
            except (OSError, ValueError, KeyError) as error:
                print(f"Could not stop the library gracefully: {error}", file=sys.stderr)
                return 1
            print("Stopping the library service. Active work is being paused; progress stays saved.")
            started = time.monotonic()
            while control.exists() and time.monotonic() - started < 30:
                time.sleep(0.2)
            if control.exists():
                print("The service is still settling its current transfer. Wait before restarting it.", file=sys.stderr)
                return 1
        else:
            print("Library service is already stopped.")
        return 0
    if running and running.get("data_dir") != str(data_dir):
        print("This port is serving a different storage folder. Use its existing --data-dir or a different --port.", file=sys.stderr)
        return 1
    if not running:
        log_dir = data_dir / "analysis/recovery"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / "service.log"
        with log.open("a", encoding="utf-8") as output:
            options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen([sys.executable, str(ROOT / "scripts/ghbook.py"), "serve", "--port", str(args.port), "--data-dir", str(data_dir)],
                                       cwd=ROOT, env=dict(os.environ, PYTHONUTF8="1"), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, **options)
        # A first launch can fetch the catalog. Show its location while it initializes.
        print("Starting your library. First launch may download the catalog...", flush=True)
        started = time.monotonic()
        while time.monotonic() - started < 180:
            running = health(url)
            if running:
                break
            if process.poll() is not None:
                print("Library could not start. " + log.read_text(encoding="utf-8")[-1800:], file=sys.stderr)
                return 1
            time.sleep(0.5)
        if not running:
            print(f"Catalog initialization is still running. Check {log} and open {url} when ready.")
            return 0
    launcher = ".\\library.cmd" if os.name == "nt" else "./library.sh"
    print(f"Library: {url}\nSaved books: {data_dir}\nStop service: {launcher} --port {args.port} --stop", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
