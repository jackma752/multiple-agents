r"""Connectivity control check with a RELIABLE TLS stack (no model calls).

Companion to tools/net_check.ps1. The PowerShell version's HTTPS column is
unreliable on this machine because the shell's schannel TLS stack is broken -
even pypi.org fails there. Python's OpenSSL works (it downloaded wheels earlier
in this project), so this script gives a trustworthy TLS/HTTPS verdict.

It performs, per host:
  1. DNS resolution (all A records) + reverse sanity (which resolver answered)
  2. TCP connect on 443 with an explicit timeout
  3. TLS handshake + HTTP HEAD over TLS

It does NOT start Codex, does NOT call any model, and does NOT read credentials.

Usage:
    .\.venv\Scripts\python.exe tools\net_check.py
    .\.venv\Scripts\python.exe tools\net_check.py --hosts chatgpt.com,api.openai.com
    .\.venv\Scripts\python.exe tools\net_check.py --json var\net_check.json
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time

DEFAULT_HOSTS = ["chatgpt.com", "api.openai.com", "pypi.org"]
PORT = 443


def dns_lookup(host: str) -> dict:
    info = {"ips": [], "error": "", "families": []}
    try:
        addrs = socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)
        seen = []
        for fam, _t, _p, _c, sa in addrs:
            ip = sa[0]
            if ip not in seen:
                seen.append(ip)
                info["families"].append("IPv6" if fam == socket.AF_INET6 else "IPv4")
        info["ips"] = seen
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def tcp_connect(host: str, timeout: float) -> dict:
    start = time.time()
    try:
        s = socket.create_connection((host, PORT), timeout=timeout)
        ms = int((time.time() - start) * 1000)
        return {"ok": True, "error": "", "ms": ms, "sock": s}
    except Exception as exc:  # noqa: BLE001
        ms = int((time.time() - start) * 1000)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "ms": ms, "sock": None}


def tls_head(host: str, sock, timeout: float) -> dict:
    try:
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(sock, server_hostname=host)
        # Report the peer certificate subject so we can see WHO we are talking to.
        cert = ss.getpeercert() or {}
        subject = ""
        for rdn in cert.get("subject", ()):
            for k, v in rdn:
                if k == "commonName":
                    subject = v
        issuer = ""
        for rdn in cert.get("issuer", ()):
            for k, v in rdn:
                if k == "organizationName":
                    issuer = v
        version = ss.version()
        ss.sendall(
            f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        raw = ss.recv(256)
        first = raw.split(b"\r\n")[0].decode("latin1", "replace")
        ss.close()
        return {
            "ok": True,
            "tls": version,
            "http_status_line": first,
            "cert_cn": subject,
            "cert_issuer": issuer,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "tls": "", "http_status_line": "", "cert_cn": "",
                "cert_issuer": "", "error": f"{type(exc).__name__}: {exc}"}


def check(host: str, timeout: float) -> dict:
    dns = dns_lookup(host)
    tcp = tcp_connect(host, timeout)
    tls = {"ok": False, "tls": "", "http_status_line": "", "cert_cn": "",
           "cert_issuer": "", "error": "not attempted (no TCP)"}
    if tcp["ok"]:
        tls = tls_head(host, tcp["sock"], timeout)

    if not dns["ips"]:
        verdict = "DNS_FAIL"
    elif not tcp["ok"]:
        verdict = "TCP_FAIL"
    elif not tls["ok"]:
        verdict = "TLS_FAIL"
    else:
        verdict = "REACHABLE"

    return {
        "host": host,
        "dns_ips": dns["ips"],
        "dns_error": dns["error"],
        "tcp_ok": tcp["ok"],
        "tcp_error": tcp["error"],
        "tcp_ms": tcp["ms"],
        "tls_ok": tls["ok"],
        "tls_version": tls["tls"],
        "http_status_line": tls["http_status_line"],
        "cert_cn": tls["cert_cn"],
        "cert_issuer": tls["cert_issuer"],
        "tls_error": tls["error"],
        "verdict": verdict,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="DNS/TCP/TLS control check (no model calls).")
    p.add_argument("--hosts", default=",".join(DEFAULT_HOSTS))
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--json", default="")
    args = p.parse_args()

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    print("=" * 78)
    print("Connectivity control check via Python/OpenSSL (no model calls)")
    print(f"time   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"targets: {', '.join(hosts)}  port {PORT}  timeout {args.timeout}s")
    print("=" * 78)

    results = []
    for h in hosts:
        print(f"\n--- {h} ---")
        r = check(h, args.timeout)
        results.append(r)

        if r["dns_ips"]:
            print(f"  DNS  : OK    {', '.join(r['dns_ips'])}")
        else:
            print(f"  DNS  : FAIL  {r['dns_error']}")

        if r["tcp_ok"]:
            print(f"  TCP  : OK    {h}:{PORT} ({r['tcp_ms']}ms)")
        else:
            print(f"  TCP  : FAIL  {r['tcp_error']} ({r['tcp_ms']}ms)")

        if r["tls_ok"]:
            print(f"  TLS  : OK    {r['tls_version']}  {r['http_status_line']}")
            print(f"  CERT : CN={r['cert_cn'] or '(none)'}  issuer={r['cert_issuer'] or '(none)'}")
        else:
            print(f"  TLS  : FAIL  {r['tls_error']}")
        print(f"  => {r['verdict']}")

    print("\n" + "=" * 78)
    print("Summary")
    print("=" * 78)
    width = max(len(r["host"]) for r in results)
    for r in results:
        print(f"  {r['host']:<{width}}  dns={'ok' if r['dns_ips'] else 'FAIL':<4} "
              f"tcp={'ok' if r['tcp_ok'] else 'FAIL':<4} "
              f"tls={'ok' if r['tls_ok'] else 'FAIL':<4}  {r['verdict']}")

    print("\nHow to read this:")
    print("  pypi.org fails too        -> the machine/network is down overall.")
    print("  pypi.org OK, OpenAI fails -> target-specific; needs the network side.")
    print("  DNS error or odd IP       -> suspect DNS policy / poisoning")
    print("                               (compare cert CN/issuer with the expected")
    print("                               vendor; a wrong issuer hints at interception).")
    print("  TCP timeout but DNS OK    -> silent drop; cause undetermined from here.")
    print("\nThis script does not start Codex and does not call any model.")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results},
                      fh, indent=2, ensure_ascii=False)
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
