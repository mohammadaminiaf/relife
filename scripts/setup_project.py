#!/usr/bin/env python3
"""Check the library runtime, or restore the full REA toolchain on macOS/Linux."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from app_runtime import node_runtime

ROOT = Path(__file__).resolve().parents[1]
ENGINE_NAME = "jadx-headless-mcp-0.7.1-all.jar"
ENGINE_URL = "https://github.com/1013503897/jadx-headless-mcp/releases/download/v0.7.1/" + ENGINE_NAME
ENGINE_SIZE = 42619858
ENGINE_SHA256 = "6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75"


def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def install_engine():
    destination = ROOT / ".tools" / ENGINE_NAME
    if destination.is_file() and sha256(destination) == ENGINE_SHA256:
        print("JADX engine already matches the pinned SHA-256.", flush=True)
        return
    print("Downloading the pinned JADX engine (42.6 MB)...", flush=True)
    step = (ENGINE_SIZE + 7) // 8

    def part(start):
        end = min(start + step, ENGINE_SIZE) - 1
        for attempt in range(3):
            try:
                request = urllib.request.Request(ENGINE_URL, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(request, timeout=90) as response:
                    if response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{ENGINE_SIZE}":
                        raise ValueError("Unexpected byte range from the engine release")
                    data = response.read()
                if len(data) != end - start + 1:
                    raise ValueError("Incomplete engine download")
                return data
            except (OSError, ValueError):
                if attempt == 2:
                    raise

    with ThreadPoolExecutor(max_workers=8) as pool:
        data = b"".join(pool.map(part, range(0, ENGINE_SIZE, step)))
    if hashlib.sha256(data).hexdigest() != ENGINE_SHA256:
        raise ValueError("JADX engine SHA-256 mismatch; installation refused")
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".jadx-", suffix=".part", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
        os.replace(staged, destination)
    finally:
        if staged:
            staged.unlink(missing_ok=True)
    print("JADX engine downloaded and verified.", flush=True)


def check():
    source = ROOT / "rea/package.json"
    if not source.is_file():
        raise ValueError("REA source is missing; run ./setup.sh to initialize the submodule")
    pin = run(["git", "ls-files", "--stage", "--", "rea"], capture_output=True, text=True).stdout.split()
    revision = run(["git", "-C", "rea", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if not pin or pin[0] != "160000" or pin[1] != revision:
        raise ValueError("REA checkout does not match the submodule revision recorded in this project")
    engine = ROOT / ".tools" / ENGINE_NAME
    if not engine.is_file() or sha256(engine) != ENGINE_SHA256:
        raise ValueError("Pinned JADX engine is missing or differs; run ./setup.sh")
    runtime = ROOT / ".tools/node_modules/node/bin/node"
    if not runtime.is_file():
        raise ValueError("Project Node.js is missing; run ./setup.sh")
    packages = json.loads((ROOT / ".tools/package.json").read_text())["dependencies"]
    actual_node = run([str(runtime), "--version"], capture_output=True, text=True).stdout.strip()
    if actual_node != "v" + packages["node"]:
        raise ValueError("Project Node.js version differs from the pinned version")
    actual_rea = run([str(ROOT / "rea.sh"), "--version"], capture_output=True, text=True).stdout.strip()
    if actual_rea != packages["rea-agents"] or json.loads(source.read_text())["version"] != actual_rea:
        raise ValueError("REA source/runtime versions differ from the project pin")
    doctor = run([str(ROOT / "rea.sh"), "doctor", "--provider", "jadx", "--json"], capture_output=True, text=True)
    report = json.loads(doctor.stdout)
    if report.get("healthy") is not True:
        raise ValueError("REA's JADX readiness check failed: " + doctor.stdout)
    run([str(ROOT / "bru.sh"), "--version"], capture_output=True, text=True)
    print(f"Ready: REA {actual_rea}, Node {actual_node}, verified JADX, full JDK, Bruno CLI.", flush=True)
    print(f"REA source revision: {revision}", flush=True)


def check_app():
    runtime = node_runtime()
    if not runtime:
        raise ValueError("Node.js is required for EPUB conversion. Install Node.js, then rerun setup.")
    version = run([str(runtime), "--version"], capture_output=True, text=True).stdout.strip()
    packages = json.loads((ROOT / ".tools/package.json").read_text())["dependencies"]
    if os.name != "nt" and version != "v" + packages["node"]:
        raise ValueError("Project Node.js differs from its pinned version")
    run([str(runtime), "--input-type=module", "-e", "import {createDecipheriv} from 'node:crypto'; if(typeof createDecipheriv !== 'function') process.exit(1)"], capture_output=True, text=True)
    launcher = ".\\library.cmd" if os.name == "nt" else "./library.sh"
    print(f"Library app ready: Python {sys.version.split()[0]}, Node {version}. Run {launcher}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check the existing installation without changing it")
    parser.add_argument("--app-only", action="store_true", help="Set up the local library app without JADX, a JDK, REA source or Bruno CLI")
    args = parser.parse_args()
    try:
        if os.name == "nt":
            if not args.app_only:
                raise ValueError("Use setup.cmd --app-only for the Windows library. Full APK analysis requires macOS, Linux or WSL.")
            check_app()
            return 0
        if not args.app_only and not shutil.which("git"):
            raise ValueError("Git is required to initialize the pinned REA submodule")
        if not args.check:
            npm = shutil.which("npm")
            if not npm or not shutil.which("node"):
                raise ValueError("Install Node.js with npm first, then rerun ./setup.sh; the project will install its own pinned runtime")
            if not args.app_only:
                print("Initializing the pinned REA source checkout...", flush=True)
                # REA's own optional provider submodules are not needed for the APK workflow.
                run(["git", "submodule", "update", "--init", "--", "rea"])
            print("Installing project Node.js and REA from the lockfile...", flush=True)
            run([npm, "ci", "--prefix", ".tools", "--omit=optional", "--no-audit", "--no-fund"])
            env = dict(os.environ)
            env["PATH"] = str(ROOT / ".tools/node_modules/node/bin") + os.pathsep + env.get("PATH", "")
            if not args.app_only:
                print("Installing the pinned Bruno CLI...", flush=True)
                run([npm, "ci", "--prefix", ".tools/bruno", "--no-audit", "--no-fund"], env=env)
                install_engine()
        check_app() if args.app_only else check()
        return 0
    except subprocess.CalledProcessError as error:
        print("setup: " + (error.stdout or error.stderr or str(error)).strip(), file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print("setup: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
