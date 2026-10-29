"""Controlled file storage for paper tasks: ``<root>/<task_id>/...`` only.

Paths in the database are relative to the root and re-checked on every read,
so neither a stored row nor a request can point outside the task directory.
Files are content-addressed (SHA-256), which also lets an identical file be
recognized and reused instead of read and billed again.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


class PaperStore:
    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)

    def _resolve(self, rel_path: str) -> str:
        if not isinstance(rel_path, str) or os.path.isabs(rel_path) or ".." in rel_path.replace("\\", "/").split("/"):
            raise ValueError("path outside the paper store")
        full = os.path.realpath(os.path.join(self.root, rel_path))
        if os.path.commonpath([os.path.normcase(full), os.path.normcase(self.root)]) != os.path.normcase(self.root):
            raise ValueError("path outside the paper store")
        return full

    def save(self, task_id: str, area: str, data: bytes, suffix: str = "") -> tuple[str, str, int]:
        if not (_SAFE.match(task_id) and _SAFE.match(area)) or (suffix and not _SAFE.match(suffix)):
            raise ValueError("unsafe store name")
        digest = hashlib.sha256(data).hexdigest()
        rel = f"{task_id}/{area}/{digest[:24]}{suffix}"
        full = self._resolve(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if not os.path.exists(full):
            tmp = full + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, full)
        return rel, digest, len(data)

    def write_export(self, task_id: str, version: int, name: str, data: bytes) -> tuple[str, str, int]:
        if not (_SAFE.match(task_id) and _SAFE.match(name)) or type(version) is not int:
            raise ValueError("unsafe export name")
        rel = f"{task_id}/exports/v{version}/{name}"
        full = self._resolve(rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, full)
        return rel, hashlib.sha256(data).hexdigest(), len(data)

    def read(self, rel_path: str, expected_sha256: Optional[str] = None) -> bytes:
        with open(self._resolve(rel_path), "rb") as fh:
            data = fh.read()
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise ValueError("stored file changed since it was recorded")
        return data

    def exists(self, rel_path: str, expected_sha256: Optional[str] = None) -> bool:
        try:
            self.read(rel_path, expected_sha256)
            return True
        except (OSError, ValueError):
            return False

    def path(self, rel_path: str) -> str:
        return self._resolve(rel_path)
