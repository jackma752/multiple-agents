"""Descendant-process records for an owned child (read-only, Windows only).

Same Toolhelp snapshot as the S4/S5 probes (``tools/probes/appserver_s4.py``),
kept here so the adapter does not import probe code. Nothing in this module
stops a process: the supervised boundary closes only the entry process the
adapter owns, and descendants are recorded, never ended by PID or by name.

Windows reuses process ids, and a process keeps its parent's id after the
parent is gone. A long-lived unrelated process whose dead parent's id was
reused by our child would otherwise look like its descendant (seen offline:
a tray program "descending" from a fresh stub launcher). So a parent link
counts only if the child was created no earlier than the parent, and a
"survivor" is the same process (id, image and creation time), not a new one
that happens to reuse the id.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

Created = Callable[[int], Optional[int]]


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


def creation_time(pid: int) -> Optional[int]:
    """The process's creation time (FILETIME ticks), or None if it cannot be read."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(ctypes.c_ulonglong),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return int(created.value)
    finally:
        kernel32.CloseHandle(handle)


def descendants(root_pid: int, table: list[tuple[int, int, str]],
                created: Created = creation_time) -> list[tuple[int, str]]:
    """Processes whose parent chain reaches ``root_pid`` (the root itself excluded).

    A link whose child is older than its parent is a stale id, not a child.
    When either time is unreadable the link is kept: unknown is not proof.
    """
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, parent, name in table:
        if pid != parent:
            children.setdefault(parent, []).append((pid, name))
    times: dict[int, Optional[int]] = {}

    def when(pid: int) -> Optional[int]:
        if pid not in times:
            times[pid] = created(pid)
        return times[pid]

    found: list[tuple[int, str]] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, name in children.get(parent, []):
            if any(pid == seen for seen, _ in found) or pid == root_pid:
                continue
            child_t, parent_t = when(pid), when(parent)
            if child_t is not None and parent_t is not None and child_t < parent_t:
                continue  # its real parent died; the id was reused
            found.append((pid, name))
            frontier.append(pid)
    return found


def snapshot_descendants(root_pid: int, created: Created = creation_time
                         ) -> Optional[list[tuple[int, str, Optional[int]]]]:
    """Descendants now, each with its creation time (to recognise it later)."""
    table = process_table()
    if table is None:
        return None
    return [(pid, name, created(pid)) for pid, name in descendants(root_pid, table, created)]


def survivors(before: list, grace_seconds: float, created: Created = creation_time) -> Optional[list]:
    """Which of ``before`` are still the same processes once ``grace_seconds`` have passed.

    A descendant that is merely mid-exit is not a survivor, so the table is
    re-read until either none remain or the grace period is over. An entry is
    ``(pid, image)`` or ``(pid, image, creation time)``; with a creation time,
    a new process that reuses the id is not the same process.
    """
    deadline = time.monotonic() + max(grace_seconds, 0.0)
    while True:
        table = process_table()
        if table is None:
            return None
        present = {(pid, name) for pid, _parent, name in table}
        left = []
        for entry in before:
            pid, name = entry[0], entry[1]
            if (pid, name) not in present:
                continue
            born = entry[2] if len(entry) > 2 else None
            now = created(pid) if born is not None else None
            if born is not None and now is not None and now != born:
                continue  # the id now belongs to another process
            left.append(entry)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)
