"""Read referenced files for a call's context, inside the files root only (T-1e/T-1f).

The boundary is checked on the RESOLVED real path (``os.path.realpath``
follows symlinks and junctions, collapses ``..``) against the real root,
case-insensitively on Windows; excluded names are checked on both the
requested and the resolved path. Only explicit references are read - paths
mentioned in chat text never are.

The text read here goes into the call's input and its context preview, so
the approval binds exactly that text (the snippet's hash, range and the
whole file's hash). Running the call uses the approved text; a file edited
after approval changes nothing that is sent.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from agenthub.context import FILE_FRAME, FILE_SNIPPET, SEPARATOR
from agenthub.files import (
    EXCLUDED_DIRS, MAX_FILE_BYTES, MAX_FILES_CHARS, MAX_SNIPPET_CHARS, excluded, merge_ranges, parse_ref,
)

#: Upper bound on files examined by one listing (the picker's autocomplete).
LIST_SCAN_LIMIT = 20_000


class FileRefError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def _root(root: Optional[str]) -> str:
    if not root:
        raise FileRefError("file_refs_disabled", "未配置文件根目录（AGENT_HUB_FILES_ROOT），不能引用文件")
    return os.path.realpath(root)


def resolve(root: Optional[str], path: str) -> tuple[str, str]:
    """(real path, root-relative posix path) of a referenced file, or FileRefError."""
    real_root = _root(root)
    candidate = path if os.path.isabs(path) else os.path.join(real_root, path)
    real = os.path.realpath(candidate)
    try:
        inside = os.path.normcase(os.path.commonpath([real, real_root])) == os.path.normcase(real_root)
    except ValueError:  # another drive
        inside = False
    if not inside or os.path.normcase(real) == os.path.normcase(real_root):
        raise FileRefError("file_outside_root", f"不在文件根目录内：{path}")
    rel = os.path.relpath(real, real_root)
    requested = os.path.relpath(os.path.abspath(candidate), real_root) if not os.path.isabs(path) else rel
    if excluded(rel.split(os.sep)) or excluded(requested.replace("/", os.sep).split(os.sep)):
        raise FileRefError("file_excluded", f"该路径默认不允许引用（凭据、依赖、构建产物、运行数据或证据）：{path}")
    if not os.path.isfile(real):
        raise FileRefError("file_not_found", f"文件不存在：{path}")
    return real, rel.replace(os.sep, "/")


def read_lines(real: str, display: str) -> tuple[list[str], str]:
    """The file's lines (newlines normalised to ``\\n``) and its SHA-256, or FileRefError."""
    if os.path.getsize(real) > MAX_FILE_BYTES:
        raise FileRefError("file_too_large", f"文件超过 {MAX_FILE_BYTES} 字节：{display}")
    with open(real, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    if b"\x00" in data:
        raise FileRefError("file_not_text", f"不是文本文件：{display}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileRefError("file_not_text", f"不是 UTF-8 文本：{display}") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, hashlib.sha256(data).hexdigest()


def file_segments(root: Optional[str], specs) -> list[tuple]:
    """Context segments for the referenced files, merged per file, or FileRefError/ValueError."""
    order: list[str] = []
    wanted: dict[str, tuple[str, list]] = {}
    for spec in specs:
        ref = parse_ref(spec)
        real, rel = resolve(root, ref.path)
        if rel not in wanted:
            order.append(rel)
            wanted[rel] = (real, [])
        wanted[rel][1].append(None if ref.whole else (ref.start_line, ref.end_line))
    segments: list[tuple] = []
    total = 0
    for rel in order:
        real, ranges = wanted[rel]
        lines, file_hash = read_lines(real, rel)
        for span in merge_ranges(ranges):
            start, end = span if span is not None else (1, len(lines))
            if span is not None and end > len(lines):
                raise FileRefError("range_out_of_bounds", f"{rel} 只有 {len(lines)} 行，没有第 {end} 行")
            snippet = "\n".join(lines[start - 1:end])
            if len(snippet) > MAX_SNIPPET_CHARS:
                raise FileRefError("snippet_too_large",
                                   f"{rel} 第 {start}-{end} 行有 {len(snippet)} 字，超过单段上限 "
                                   f"{MAX_SNIPPET_CHARS}；请缩小行范围")
            total += len(snippet)
            if total > MAX_FILES_CHARS:
                raise FileRefError("files_too_large", f"引用的文件内容合计超过 {MAX_FILES_CHARS} 字")
            where = {"path": rel, "start_line": start, "end_line": end, "file_sha256": file_hash}
            segments += [(SEPARATOR, "\n\n"),
                         (FILE_FRAME, f"--- File: {rel} (lines {start}-{end} of {len(lines)}) ---\n", where),
                         (FILE_SNIPPET, snippet, where),
                         (FILE_FRAME, "\n--- End of file ---", where)]
    return segments


def list_files(root: Optional[str], query: str = "", limit: int = 50) -> dict:
    """Root-relative paths that may be referenced, matching ``query`` (case-insensitive)."""
    real_root = _root(root)
    needle = query.strip().casefold().replace("\\", "/")
    found, scanned = [], 0
    for current, dirs, files in os.walk(real_root):
        rel_dir = os.path.relpath(current, real_root)
        base = [] if rel_dir == "." else rel_dir.split(os.sep)
        dirs[:] = sorted(d for d in dirs if d.casefold() not in EXCLUDED_DIRS)
        for name in sorted(files):
            scanned += 1
            if scanned > LIST_SCAN_LIMIT:
                return {"files": found, "truncated": True}
            parts = base + [name]
            if excluded(parts):
                continue
            rel = "/".join(parts)
            if needle in rel.casefold():
                found.append(rel)
                if len(found) >= limit:
                    return {"files": found, "truncated": True}
    return {"files": found, "truncated": False}
