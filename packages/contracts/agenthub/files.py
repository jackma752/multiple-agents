"""File references for a call's context (T-1e/T-1f).

A reference names a file under the Hub's files root, optionally a line range:

* ``src/app.py`` - the whole file;
* ``src/app.py:L10-20`` (also ``:10-20``, ``:L10-L20``, ``:L10``) - lines 10 to 20;
* ``"docs/my notes.md":L3-5`` - quotes for clarity; a leading ``@`` is optional.

The range suffix is only recognised at the very end, so drive letters
(``C:\\repo\\a.py:L3-5``) and Chinese paths parse unambiguously. Lines are
1-based and inclusive. References to the same file are merged: a whole-file
reference absorbs its ranges, overlapping or adjacent ranges join.

The Hub reads the file itself and expands the selected text into the call's
input (``agenthub.context`` parts ``file_frame`` / ``file_snippet``); the
reference string is never passed through to an agent. What may be read is
decided by the kernel on the resolved real path (``agenthub_kernel.file_refs``).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable, Optional

MAX_REFS = 8
MAX_SPEC_CHARS = 1100
#: Largest file the Hub will open, and the most text one call may take from files.
MAX_FILE_BYTES = 1_000_000
MAX_SNIPPET_CHARS = 40_000
MAX_FILES_CHARS = 60_000

#: Never read: credentials, VCS internals, dependencies, build output, runtime
#: data and evidence. Matched case-insensitively on every path segment.
EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", "site-packages",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "dist", "build", "var", "evidence",
    ".ssh", ".aws", ".codex", ".claude",
})
EXCLUDED_NAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "id_rsa*", "id_ed25519*",
    "id_ecdsa*", "auth.json", "credentials*", "*secret*", ".netrc", ".npmrc", ".pypirc",
    "*.db", "*.sqlite", "*.sqlite3",
)

_RANGE = re.compile(r":L?(\d+)(?:-L?(\d+))?$", re.IGNORECASE)


@dataclass(frozen=True)
class FileRef:
    path: str
    start_line: Optional[int] = None  # None = whole file
    end_line: Optional[int] = None

    @property
    def whole(self) -> bool:
        return self.start_line is None


def parse_ref(spec: str) -> FileRef:
    """One reference spec, or ValueError."""
    if not isinstance(spec, str) or not spec.strip() or len(spec) > MAX_SPEC_CHARS:
        raise ValueError("file reference must be a nonblank string")
    text = spec.strip()
    if text.startswith("@"):
        text = text[1:]
    start = end = None
    match = _RANGE.search(text)
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        text = text[:match.start()]
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    if not text.strip() or '"' in text or "\x00" in text:
        raise ValueError(f"file reference has no usable path: {spec!r}")
    if start is not None and (start < 1 or end < start):
        raise ValueError(f"line range must be 1-based and ascending: {spec!r}")
    return FileRef(text, start, end)


def excluded(rel_parts: Iterable[str]) -> bool:
    """Is a root-relative path (as its segments) one the Hub never reads?"""
    parts = [p.casefold() for p in rel_parts if p not in ("", ".")]
    if not parts:
        return True
    if any(p in EXCLUDED_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in EXCLUDED_NAMES)


def merge_ranges(ranges: Iterable[Optional[tuple]]) -> list[Optional[tuple]]:
    """Merge one file's ranges: ``[None]`` (whole file) absorbs all; else sorted, joined."""
    spans = list(ranges)
    if any(r is None for r in spans):
        return [None]
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(m) for m in merged]
