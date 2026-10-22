"""Fetch EXACT pinned wheels into wheelhouse, replacing the "latest" ones.

The latest fastapi (0.143) pulls a huge transitive closure (opentelemetry,
annotated-doc, rich, ...). For a lean P2 kernel we pin fastapi to the stable
0.115.x line, which depends on only starlette + pydantic + typing-extensions.

We download a specific version from PyPI's JSON API (the per-version endpoint
returns exactly that version's files), then reinstall the whole stack from the
wheelhouse.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from urllib.parse import quote

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WHEELHOUSE = os.path.join(REPO_ROOT, "wheelhouse")

# (name, exact version) — pip resolves from here; all must exist in wheelhouse.
PINNED = [
    ("fastapi", "0.115.6"),
    ("starlette", "0.41.3"),
    ("pydantic", "2.9.2"),
    ("pydantic-core", "2.23.4"),
    ("sqlalchemy", "2.0.36"),
    ("uvicorn", "0.32.1"),
    ("httpx", "0.27.2"),
    ("httpcore", "1.0.7"),
    ("pytest", "8.3.4"),
    ("python-multipart", "0.0.17"),
    ("anyio", "4.6.2"),
    ("annotated-types", "0.7.0"),
    ("typing-extensions", "4.12.2"),
    ("h11", "0.14.0"),
    ("sniffio", "1.3.1"),
    ("idna", "3.10"),
    ("certifi", "2026.10.19"),  # upgraded 2026-10-22 (docs/codex-proxy-setup.md section 7)
    ("iniconfig", "2.0.0"),
    ("click", "8.1.7"),
    ("colorama", "0.4.6"),
    ("packaging", "24.2"),
    ("pluggy", "1.5.0"),
    ("greenlet", "3.1.1"),
    ("typing-inspection", "0.4.0"),
    ("annotated-doc", "0.0.5"),
    ("markdown-it-py", "3.0.0"),
    ("mdit-py-plugins", "0.4.2"),
    ("pygments", "2.18.0"),
    ("rich", "13.9.4"),
    ("pymdown-extensions", "10.14"),
    ("mypy-extensions", "1.0.0"),
    ("exceptiongroup", "1.2.2"),
    ("tomli", "2.2.1"),
    ("websockets", "13.1"),
    ("httptools", "0.6.4"),
]


def _best(files):
    scored = []
    for f in files:
        lower = f["filename"].lower()
        if not lower.endswith(".whl"):
            continue
        if "macosx" in lower or "manylinux" in lower or "musllinux" in lower or "pypy" in lower or "pp3" in lower:
            continue
        if "-cp" in lower and "-cp312" not in lower and "py3" not in lower:
            continue
        score = 0
        if "win_amd64" in lower:
            score += 3
        if "none-any" in lower:
            score += 2
        if "-cp312" in lower:
            score += 1
        scored.append((score, f["url"]))
    if not scored:
        return None
    scored.sort(key=lambda t: -t[0])
    return scored[0][1]


def download(url, dest, retries=5):
    ctx = ssl.create_default_context()
    for a in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agent-hub/p2"})
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                data = resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            print(f"  got {os.path.basename(dest)} ({len(data)} bytes)")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  attempt {a}: {e}")
            if a == retries:
                return False
    return False


def fetch(name, ver):
    url = f"https://pypi.org/pypi/{quote(name)}/{ver}/json"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "agent-hub/p2"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"  !! resolve {name}=={ver}: {e}")
        return False
    best = _best(meta.get("urls") or [])
    if not best:
        print(f"  !! no compatible wheel for {name}=={ver}")
        return False
    dest = os.path.join(WHEELHOUSE, best.split("/")[-1])
    if os.path.exists(dest):
        print(f"  already have {os.path.basename(dest)}")
        return True
    return download(best, dest)


def main():
    os.makedirs(WHEELHOUSE, exist_ok=True)
    ok = True
    for name, ver in PINNED:
        print(f"== {name}=={ver} ==")
        if not fetch(name, ver):
            ok = False
    print("DONE ok=" + str(ok))


if __name__ == "__main__":
    main()
