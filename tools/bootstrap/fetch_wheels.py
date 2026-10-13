"""Download wheels for a fixed dependency list using urllib (works over TLS).

pip's own downloader stalls on this host (see HANDOFF §1) while direct urllib
HTTPS works. We resolve the latest version of each package from PyPI's JSON
API, download a compatible cp312/win_amd64 or pure-python wheel into a local
``wheelhouse``, then run ``pip install --no-index --find-links wheelhouse``.

This is intentionally a bootstrap helper, not part of the runtime.
"""

from __future__ import annotations

import json
import os
import sys
import ssl
import urllib.request
from urllib.parse import quote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WHEELHOUSE = os.path.join(REPO_ROOT, "wheelhouse")

# Direct + important transitive deps. `pip` during install resolves the full
# closure from what lands in wheelhouse; if a transitive dep is missing we add
# it here and re-run.
PACKAGES = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "pydantic",
    "python-multipart",
    "pytest",
    "httpx",
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
    "uvloop",
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
    "passlib",
    "bcrypt",
    "email-validator",
    "dnspython",
    "annotated-doc",
    "markdown-it-py",
    "mdit-py-plugins",
    "pygments",
    "rich",
    "pymdown-extensions",
]

LOOPBACK = sys.version_info[:2]  # unused, kept for clarity


def _best_wheel(files: list[dict]) -> str | None:
    """Pick a wheel compatible with cp312 / win_amd64 or pure-python."""
    scored: list[tuple[int, str]] = []
    for f in files:
        fname = f["filename"]
        lower = fname.lower()
        if not lower.endswith(".whl"):
            continue
        if "-cp" in lower or "-py" in lower or "none-any" in lower:
            # candidate
            score = 0
            if "win_amd64" in lower:
                score += 3
            if "none-any" in lower:
                score += 2
            if "-cp312-" in lower or "py3-none-any" in lower:
                score += 1
            if ("-cp" in lower and "-cp312" not in lower and "-py3" not in lower):
                continue  # wrong abi
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
            print(f"  got {os.path.basename(dest)} ({len(data)} bytes)")
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
            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  !! resolve {name}: {exc}")
        return False
    files = meta.get("urls") or []
    best = _best_wheel(files)
    if not best:
        print(f"  !! no compatible wheel for {name}")
        return False
    fname = best.split("/")[-1]
    dest = os.path.join(WHEELHOUSE, fname)
    if os.path.exists(dest):
        print(f"  already have {fname}")
        return True
    return download(best, dest)


def main() -> None:
    os.makedirs(WHEELHOUSE, exist_ok=True)
    ok = True
    for name in PACKAGES:
        print(f"== {name} ==")
        if not fetch(name):
            ok = False
    print("DONE ok=" + str(ok))


if __name__ == "__main__":
    main()
