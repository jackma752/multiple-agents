r"""Proxy reachability check (NO model calls).

Tests the two candidate local proxy ports against the OpenAI/ChatGPT endpoints
and a control host, and records the HTTP status codes. Nothing here starts Codex
or invokes a model.

Reports, per (proxy port, target):
  * whether the connection through the proxy completes
  * the HTTP status code and a short server header
  * the error type when it fails instead
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

import httpx

PORTS = [8090, 10090]
TARGETS = [
    ("https://chatgpt.com/", "chatgpt.com"),
    ("https://api.openai.com/v1/models", "api.openai.com"),
    ("https://pypi.org/simple/", "pypi.org (control)"),
]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_check.json")


def port_open(port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(status: dict, port: int, url: str, label: str) -> None:
    proxy = f"http://127.0.0.1:{port}"
    entry = {"port": port, "target": label, "url": url, "proxy": proxy}
    try:
        with httpx.Client(proxy=proxy, timeout=20, trust_env=False,
                          follow_redirects=False) as c:
            r = c.get(url)
        entry.update({
            "ok": True,
            "status_code": r.status_code,
            "server": r.headers.get("server", ""),
            "content_type": r.headers.get("content-type", ""),
            "via": r.headers.get("via", ""),
            "x_squid": r.headers.get("x-squid-error", ""),
            "body_head": r.text[:160].replace("\n", " ") if r.text else "",
            "error": "",
        })
    except Exception as exc:  # noqa: BLE001
        entry.update({
            "ok": False, "status_code": None, "server": "", "content_type": "",
            "via": "", "x_squid": "", "body_head": "",
            "error": f"{type(exc).__name__}: {exc}",
        })
    status["results"].append(entry)
    code = entry["status_code"] if entry["ok"] else f"ERR {entry['error'][:60]}"
    print(f"  proxy {port:>5}  ->  {label:<20}  {code}")
    if entry["ok"] and entry["server"]:
        print(f"          server={entry['server']}  via={entry['via'] or '-'} "
              f"squid={entry['x_squid'] or '-'}")


def main() -> int:
    status = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "results": []}

    print("=" * 78)
    print("Proxy reachability check (no model calls)")
    print("=" * 78)

    print("\n1) are the candidate proxy ports listening?")
    listening = {}
    for p in PORTS:
        listening[p] = port_open(p)
        print(f"  127.0.0.1:{p}  -> {'OPEN' if listening[p] else 'closed'}")
    status["listening"] = listening

    print("\n2) direct (no proxy) baseline:")
    for url, label in TARGETS:
        try:
            with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as c:
                r = c.get(url)
            print(f"  direct          ->  {label:<20}  {r.status_code}")
            status["results"].append({"port": None, "target": label, "url": url,
                                      "proxy": "none", "ok": True,
                                      "status_code": r.status_code, "server": "",
                                      "content_type": "", "via": "", "x_squid": "",
                                      "body_head": "", "error": ""})
        except Exception as exc:  # noqa: BLE001
            print(f"  direct          ->  {label:<20}  ERR {type(exc).__name__}: {str(exc)[:60]}")
            status["results"].append({"port": None, "target": label, "url": url,
                                      "proxy": "none", "ok": False, "status_code": None,
                                      "server": "", "content_type": "", "via": "",
                                      "x_squid": "", "body_head": "",
                                      "error": f"{type(exc).__name__}: {exc}"})

    for p in PORTS:
        if not listening[p]:
            print(f"\n3) through proxy 127.0.0.1:{p}: SKIPPED (port closed)")
            continue
        print(f"\n3) through proxy 127.0.0.1:{p}:")
        for url, label in TARGETS:
            probe(status, p, url, label)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(status, fh, indent=2, ensure_ascii=False)
    print(f"\nwritten: {os.path.relpath(OUT, os.path.dirname(os.path.dirname(OUT)))}")

    print("\nHow to read this:")
    print("  A real HTTP status (403/421) means the proxy DID reach the target and")
    print("  the target (or the proxy's egress policy) answered. A timeout or DNS")
    print("  error means the request never completed. Nothing here proves WHY a")
    print("  given status was returned.")
    print("\nThis script does not start Codex and does not call any model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
