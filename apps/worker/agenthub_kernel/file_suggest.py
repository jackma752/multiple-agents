"""Local file suggestions for the composer (T-1g, minimal).

Candidates come from three places only:

* paths mentioned in the message - ``src/app.py``, ``src/app.py:42``,
  ``@"a b.md":L3-5`` or a Python traceback's ``File "x.py", line 42``;
  a mention that is not a path under the root is looked up by file NAME
  (the root's file list, never file contents);
* tests or sources matching the files already chosen (``app.py`` ->
  ``test_app.py`` and back);
* this conversation's recent file references.

Each suggestion is a reference spec the user may add; nothing is added or
sent by suggesting. A mention with a line number suggests a window around
it; a large file without one asks for a range (``needs_range``) instead of
offering the whole file. Every candidate goes through the same boundary as
a reference (``file_refs.resolve``), so excluded or outside files are never
suggested.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Optional

from agenthub.budget import estimate_visible_tokens
from agenthub.files import parse_ref
from agenthub_kernel.file_refs import LIST_SCAN_LIMIT, FileRefError, list_files, read_lines, resolve

MAX_SUGGESTIONS = 8
#: A whole file is offered only below this size; above it the user picks lines.
WHOLE_FILE_CHARS = 12_000
#: Lines around a mentioned line number.
WINDOW = 15

_MENTION = re.compile(
    r'File "(?P<tb>[^"\n]{1,300})", line (?P<tbl>\d+)'
    r'|@?"(?P<q>[^"\n]{1,300})"(?::L?(?P<ql>\d+)(?:-L?(?P<qe>\d+))?)?'
    r'|@?(?P<p>(?:[A-Za-z]:[\\/])?[\w.\-]+(?:[\\/][\w.\-]+)*\.[A-Za-z0-9]{1,8})'
    r'(?::L?(?P<pl>\d+)(?:-L?(?P<pe>\d+))?)?'
)


def mentions(text: str) -> list[tuple[str, Optional[int], Optional[int]]]:
    """(path, first line, last line) for each path-like mention, in order."""
    found = []
    for m in _MENTION.finditer(text or ""):
        if m.group("tb"):
            found.append((m.group("tb"), int(m.group("tbl")), None))
        elif m.group("q"):
            path = m.group("q")
            if "." in os.path.basename(path) or "/" in path or "\\" in path:
                found.append((path, _int(m.group("ql")), _int(m.group("qe"))))
        else:
            found.append((m.group("p"), _int(m.group("pl")), _int(m.group("pe"))))
    return found


def _int(value: Optional[str]) -> Optional[int]:
    return int(value) if value else None


class _Names:
    """The root's referenceable file names, listed once per request (names only)."""

    def __init__(self, root: str) -> None:
        self.root, self._files = root, None

    def files(self) -> list[str]:
        if self._files is None:
            self._files = list_files(self.root, "", LIST_SCAN_LIMIT)["files"]
        return self._files

    def by_suffix(self, path: str, limit: int = 3) -> list[str]:
        want = path.replace("\\", "/").lstrip("./").casefold()
        return [f for f in self.files() if f.casefold() == want or f.casefold().endswith("/" + want)][:limit]

    def by_name(self, names: Iterable[str], limit: int = 2) -> list[str]:
        wanted = {n.casefold() for n in names}
        return [f for f in self.files() if f.rsplit("/", 1)[-1].casefold() in wanted][:limit]


def _located(root: str, names: _Names, path: str) -> list[str]:
    try:
        return [resolve(root, path)[1]]
    except FileRefError as exc:
        if exc.code in ("file_excluded", "file_refs_disabled"):
            return []  # never look an excluded path up some other way
    return names.by_suffix(path)


def _related_names(rel: str) -> list[str]:
    name = rel.rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(name)
    if stem.startswith("test_"):
        return [stem[5:] + ext]
    if stem.endswith("_test"):
        return [stem[:-5] + ext]
    return [f"test_{stem}{ext}", f"{stem}_test{ext}", f"{stem}.test{ext}", f"{stem}.spec{ext}"]


def _suggestion(root: str, rel: str, start: Optional[int], end: Optional[int], reason: str,
                line: Optional[int] = None) -> Optional[dict[str, Any]]:
    try:
        lines, _ = read_lines(resolve(root, rel)[0], rel)
    except FileRefError:
        return None
    total = len(lines)
    if line is not None:  # a mentioned line: a window around it
        start, end = max(1, line - WINDOW), min(total, line + WINDOW)
    if start is not None:
        end = min(end or start, total)
        if start > total:
            return None
        text = "\n".join(lines[start - 1:end])
        spec = f"{rel}:L{start}-{end}"
    else:
        text = "\n".join(lines)
        if len(text) > WHOLE_FILE_CHARS:
            return {"spec": None, "path": rel, "lines": total, "chars": len(text),
                    "estimate": estimate_visible_tokens(text), "reason": reason, "needs_range": True}
        start, end, spec = 1, total, rel
    return {"spec": spec, "path": rel, "start_line": start, "end_line": end, "lines": total,
            "chars": len(text), "estimate": estimate_visible_tokens(text), "reason": reason,
            "needs_range": False}


def suggest(root: Optional[str], text: str, selected: Iterable[str] = (),
            recent: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Up to :data:`MAX_SUGGESTIONS` references the user may add (raises if no root)."""
    if not root:
        raise FileRefError("file_refs_disabled", "未配置文件根目录（AGENT_HUB_FILES_ROOT），不能引用文件")
    names = _Names(root)
    chosen: set[str] = set()
    for spec in selected:
        try:
            chosen.add(resolve(root, parse_ref(spec).path)[1])
        except (FileRefError, ValueError):
            continue
    out: list[dict[str, Any]] = []
    seen = set(chosen)

    def add(item: Optional[dict[str, Any]]) -> None:
        if item is not None and item["path"] not in seen and len(out) < MAX_SUGGESTIONS:
            seen.add(item["path"])
            out.append(item)

    for path, start, end in mentions(text):
        for rel in _located(root, names, path):
            if start is not None and end is None:  # one line (an error location): its surroundings
                add(_suggestion(root, rel, None, None, "mentioned_line", line=start))
            else:
                add(_suggestion(root, rel, start, end, "mentioned"))
    for rel in sorted(chosen):
        for match in names.by_name(_related_names(rel)):
            add(_suggestion(root, match, None, None, "related"))
    for spec in recent:
        try:
            ref = parse_ref(spec)
            rel = resolve(root, ref.path)[1]
        except (FileRefError, ValueError):
            continue
        add(_suggestion(root, rel, ref.start_line, ref.end_line, "recent"))
    return out
