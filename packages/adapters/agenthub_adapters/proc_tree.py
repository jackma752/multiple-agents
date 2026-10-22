"""Descendant-process records for an owned child (read-only, Windows only).

Same Toolhelp snapshot as the S4/S5 probes (``tools/probes/appserver_s4.py``),
kept here so the adapter does not import probe code. Nothing in this module
stops a process: the supervised boundary closes only the entry process the
adapter owns, and descendants are recorded, never ended by PID or by name.
"""

from __future__ import annotations

import os
import time
from typing import Optional


def process_table() -> Optional[list[tuple[int, int, str]]]:
    """``[(pid, parent_pid, image name)]`` for every process, or None if unavailable."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        rows = []
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return rows
    finally:
        kernel32.CloseHandle(snapshot)


def descendants(root_pid: int, table: list[tuple[int, int, str]]) -> list[tuple[int, str]]:
    """Processes whose parent chain reaches ``root_pid`` (the root itself excluded)."""
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, parent, name in table:
        if pid != parent:
            children.setdefault(parent, []).append((pid, name))
    found: list[tuple[int, str]] = []
    frontier = [root_pid]
    while frontier:
        for pid, name in children.get(frontier.pop(), []):
            if all(pid != seen for seen, _ in found) and pid != root_pid:
                found.append((pid, name))
                frontier.append(pid)
    return found


def snapshot_descendants(root_pid: int) -> Optional[list[tuple[int, str]]]:
    table = process_table()
    return descendants(root_pid, table) if table is not None else None


def survivors(before: list[tuple[int, str]], grace_seconds: float) -> Optional[list[tuple[int, str]]]:
    """Which of ``before`` are still present once ``grace_seconds`` have passed.

    A descendant that is merely mid-exit is not a survivor, so the table is
    re-read until either none remain or the grace period is over.
    """
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while True:
        table = process_table()
        if table is None:
            return None
        present = {(pid, name) for pid, _parent, name in table}
        left = [entry for entry in before if entry in present]
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)
