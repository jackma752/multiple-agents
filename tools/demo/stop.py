r"""Stop the Agent Hub Mock API precisely, by PID.

Why this exists: ``Get-Process python | Stop-Process -Force`` kills EVERY python
process on the machine, including unrelated work and other agents' processes.
This tool stops exactly one server.

How ownership is verified (fail-closed):

1. The PID comes from the PID file written by ``serve.py``.
2. The server on that port must identify itself as Agent Hub over HTTP
   (``GET /openapi.json`` -> ``info.title == "Agent Hub"``). This is a
   non-destructive check and works without extra dependencies.
3. Only then is that PID terminated.

NOTES ON WINDOWS SEMANTICS
--------------------------
* ``os.kill(pid, 0)`` is NOT a safe liveness probe on Windows: for ordinary
  signals CPython calls ``TerminateProcess``, so it can *kill* the target, and
  under a restrictive sandbox it raises ``OSError`` and looks like "no such
  process". This tool therefore never uses it for liveness.
* ``os.kill(pid, signal.SIGTERM)`` on Windows also maps to ``TerminateProcess``
  (no graceful shutdown is possible); the PID file is removed either way.

Usage:

    .\.venv\Scripts\python.exe tools\demo\stop.py            # default port 8080
    .\.venv\Scripts\python.exe tools\demo\stop.py --port 8090
    .\.venv\Scripts\python.exe tools\demo\stop.py --status    # report only
    .\.venv\Scripts\python.exe tools\demo\stop.py --force     # skip ownership check

Exit codes: 0 stopped/healthy, 1 refusal, 2 no PID file.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPECTED_TITLE = "Agent Hub"


def pid_file_path(port: int) -> str:
    return os.path.join(REPO_ROOT, "var", f"serve-{port}.pid")


def read_pid(port: int) -> int | None:
    path = pid_file_path(port)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="ascii") as fh:
            text = fh.read().strip()
        return int(text) if text else None
    except (OSError, ValueError):
        return None


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Non-destructive liveness: can we open a TCP connection?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _identifies_as_agent_hub(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    """Ask the server on this port who it is. No side effects."""
    url = f"http://{host}:{port}/openapi.json"
    try:
        # Bypass any environment proxy: interception answers localhost with 502.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            spec = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"could not read {url}: {type(exc).__name__}"
    title = (spec.get("info") or {}).get("title")
    if title != EXPECTED_TITLE:
        return False, f"server at {host}:{port} reports title={title!r}, not {EXPECTED_TITLE!r}"
    return True, f"{url} reports title={title!r}"


def _cmdline(pid: int) -> str:
    try:
        import psutil  # type: ignore

        return " ".join(psutil.Process(pid).cmdline())
    except Exception:  # noqa: BLE001
        return ""


def status(host: str, port: int) -> int:
    pid = read_pid(port)
    if pid is None:
        print(f"port {port}: no PID file ({os.path.relpath(pid_file_path(port), REPO_ROOT)})")
        return 2
    print(f"PID file : pid {pid}")
    if not _port_open(host, port):
        print(f"port {port}: nothing listening (stale PID file)")
        return 1
    ok, why = _identifies_as_agent_hub(host, port)
    print(f"identity : {why}")
    print("HEALTHY" if ok else "NOT OURS")
    return 0 if ok else 1


def _request_graceful_shutdown(host: str, port: int, token: str) -> tuple[bool, str]:
    """Ask the server to stop itself over HTTP (no process signalling)."""
    url = f"http://{host}:{port}/api/dev/shutdown"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Length": "0"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return bool(body.get("stopping")), str(body)


def stop(host: str, port: int, force: bool, token: str, no_graceful: bool) -> int:
    path = pid_file_path(port)
    pid = read_pid(port)
    if pid is None:
        print(f"no PID file for port {port} ({os.path.relpath(path, REPO_ROOT)})")
        return 2
    print(f"PID file : {os.path.relpath(path, REPO_ROOT)} -> pid {pid}")

    if not _port_open(host, port):
        print(f"port {port}: nothing listening; removing stale PID file")
        try:
            os.remove(path)
        except OSError:
            pass
        return 0

    ok, why = _identifies_as_agent_hub(host, port)
    print(f"identity : {why}")
    if not ok and not force:
        print("REFUSING to terminate: the server on this port is not Agent Hub.")
        print("(use --force only if you are certain this PID is yours)")
        return 1

    import time

    # Preferred path: graceful self-shutdown over HTTP. This works even where
    # the environment forbids signalling the process from outside.
    if not no_graceful:
        sent, detail = _request_graceful_shutdown(host, port, token)
        print(f"graceful : {detail}")
        if sent or not _port_open(host, port, timeout=0.5):
            for _ in range(30):
                if not _port_open(host, port, timeout=0.5):
                    break
                time.sleep(0.5)
            if not _port_open(host, port, timeout=0.5):
                try:
                    os.remove(path)
                except OSError:
                    pass
                print(f"stopped pid {pid} (port {port} closed, graceful)")
                return 0
            print("graceful shutdown did not close the port; falling back to signal")
        else:
            print("graceful shutdown unavailable; falling back to signal")

    cmd = _cmdline(pid)
    if cmd:
        print(f"cmdline  : {cmd[:120]}")
    else:
        print("cmdline  : unavailable (psutil not installed)")

    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to signal pid {pid}: {type(exc).__name__}: {exc}")
        print(
            "This environment forbids terminating the process from outside.\n"
            "Use the graceful path (do not pass --no-graceful) or stop it from\n"
            "the terminal that started it (Ctrl-C)."
        )
        return 1

    for _ in range(20):
        if not _port_open(host, port, timeout=0.5):
            break
        time.sleep(0.5)

    if _port_open(host, port, timeout=0.5):
        print(f"pid {pid} still listening after 10s; it may have ignored the signal")
        return 1

    try:
        os.remove(path)
    except OSError:
        pass
    print(f"stopped pid {pid} (port {port} closed)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop the Agent Hub Mock API by PID.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--status", action="store_true", help="report only")
    parser.add_argument(
        "--force",
        action="store_true",
        help="terminate even if the port does not identify as Agent Hub",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token"),
        help="user token for the graceful shutdown request",
    )
    parser.add_argument(
        "--no-graceful",
        action="store_true",
        help="skip the HTTP shutdown request and signal the process directly",
    )
    args = parser.parse_args()
    if args.status:
        return status(args.host, args.port)
    return stop(args.host, args.port, args.force, args.token, args.no_graceful)


if __name__ == "__main__":
    sys.exit(main())
