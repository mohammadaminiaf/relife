"""Find the library's Node runtime on Windows, macOS and Linux."""
from pathlib import Path
import os
import shutil

ROOT = Path(__file__).resolve().parents[1]


def node_runtime():
    paths = (".tools/node_modules/node/bin/node.exe", ".tools/node_modules/node/node.exe") if os.name == "nt" else (".tools/node_modules/node/bin/node",)
    for relative in paths:
        candidate = ROOT / relative
        if candidate.is_file():
            return str(candidate)
    return shutil.which("node")
