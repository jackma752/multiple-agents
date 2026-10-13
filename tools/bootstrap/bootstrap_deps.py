"""Iteratively resolve and install the full dependency closure from wheelhouse.

Strategy: run ``pip install --no-index --find-links wheelhouse``. If it fails
with "Could not find a version that satisfies the requirement X", download that
package's best wheel into wheelhouse (via urllib) and retry. Loop until the
install succeeds or no progress is made.

This avoids pip's own (stalled) network downloader while still resolving all
transitive dependencies exactly as pip would.
"""

from __future__ import annotations

import os
import re
import ssl
import subprocess
import sys
import urllib.request
from urllib.parse import quote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_PY = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
WHEELHOUSE = os.path.join(REPO_ROOT, "wheelhouse")

DIRECT = ["fastapi", "uvicorn", "sqlalchemy", "pydantic", "python-multipart", "pytest", "httpx"]

# Extra deps we already know are needed (ensures they are present up front).
KNOWN = [
    "starlette",
    "anyio",
    "typing-extensions",
    "pydantic-core",
    "annotated-types",
    "h11",
    "sniffio",
    "idna",
    "certifi",
    "httpcore",
    "iniconfig",
    "click",
    "httptools",
    "colorama",
    "packaging",
    "pluggy",
    "typing-inspection",
    "greenlet",
    "mypy-extensions",
    "websockets",
    "tomli",
    "exceptiongroup",
    "wcwidth",
    "pyyaml",
    "annotated-doc",
    "markdown-it-py",
    "mdit-py-plugins",
    "pygments",
    "rich",
    "pymdown-extensions",
]

req_re = re.compile(
    r"Could not find a version that satisfies the requirement\s+([A-Za-z0-9_.\-]+)"
)


def _best_wheel(files: list[dict]) -> str | None:
    scored = []
    for f in files:
        lower = f["filename"].lower()
        if not lower.endswith(".whl"):
            continue
        if "-cp" in lower or "-py" in lower or "none-any" in lower:
            score = 0
            if "win_amd64" in lower:
                score += 3
            if "none-any" in lower:
                score += 2
            if "-cp312" in lower or "none-any" in lower:
                score += 1
            if "-cp" in lower and "-cp312" not in lower and "py3" not in lower:
                continue  # wrong ABI
            if "macosx" in lower or "manylinux" in lower or "musllinux" in lower:
                continue  # non-windows
            if "pypy" in lower or "pp3" in lower:
                continue  # non-cpython
            scored.append((score, f["url"]))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    return scored[0][1]


def download(url: str, dest: str, retries: int = 5) -> bool:
    ctx = ssl.create_default_context()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agent-hub/p2"})
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                data = resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            print(f"  fetched {os.path.basename(dest)} ({len(data)} bytes)")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  attempt {attempt}/{retries}: {exc}")
            if attempt == retries:
                return False
    return False


def fetch(name: str) -> bool:
    url = f"https://pypi.org/pypi/{quote(name)}/json"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-hub/p2"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            import json

            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  !! resolve {name}: {exc}")
        return False
    best = _best_wheel(meta.get("urls") or [])
    if not best:
        print(f"  !! no compatible wheel for {name}")
        return False
    dest = os.path.join(WHEELHOUSE, best.split("/")[-1])
    if os.path.exists(dest):
        print(f"  already have {os.path.basename(dest)}")
        return True
    return download(best, dest)


def run_pip() -> str:
    proc = subprocess.run(
        [
            VENV_PY,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            WHEELHOUSE,
            "--no-build-isolation",
            *DIRECT,
        ],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(WHEELHOUSE),
    )
    return proc.stdout + "\n" + proc.stderr


def main() -> None:
    os.makedirs(WHEELHOUSE, exist_ok=True)
    for name in KNOWN:
        fetch(name)

    rounds = 0
    while rounds < 30:
        rounds += 1
        out = run_pip()
        print(out[-1500:])
        if "Successfully installed" in out:
            print("=== SUCCESS ===")
            return
        m = req_re.search(out)
        if not m:
            print("=== STUCK (no resolvable name in error) ===")
            print(out)
            return
        name = m.group(1)
        print(f"--- resolving missing dep: {name} ---")
        if not fetch(name):
            print(f"=== FAILED to fetch {name} ===")
            return
    print("=== ROUND LIMIT ===")


if __name__ == "__main__":
    main()
