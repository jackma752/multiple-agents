r"""Project pip / certifi / proxy trust: check it, or configure the venv. Verification stays ON.

Findings on this machine (2026-10-22, see docs/codex-proxy-setup.md section 7):

* Direct HTTPS to pypi.org / files.pythonhosted.org times out. Through the
  local proxy ``http://127.0.0.1:8090`` it works, and the proxy is a plain
  CONNECT tunnel: the servers present public chains (GlobalSign, Google Trust
  Services) that verify against certifi, pip's vendored certifi and the system
  store. So pip "hangs" only when it is not told to use the proxy; no private
  proxy CA is involved and none has to be trusted.
* Both certifi bundles match the hashes pip recorded at install time.

Modes:

``--check`` (default, read-only)
    certifi bundle integrity against RECORD; the venv's pip.ini (proxy present,
    no ``trusted-host``, ``cert`` readable); TLS through the proxy with
    hostname and chain verification.

``--write-venv-config``
    write ``<venv>/pip.ini`` (site scope: this venv only) with the proxy, a
    timeout and ``cert`` = the venv's certifi bundle. With ``--extra-ca PEM``
    a separate bundle ``<venv>/agenthub-ca-bundle.pem`` = certifi + that PEM is
    written and used instead, for a proxy that does intercept TLS. certifi's own
    file is never modified, and ``trusted-host`` is never written.

No model is called and no package is downloaded.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.client
import io
import os
from pathlib import Path
import ssl
import sys

PROXY = "http://127.0.0.1:8090"
CHECK_HOSTS = ("pypi.org", "files.pythonhosted.org")
FORBIDDEN_KEYS = ("trusted-host", "trusted_host", "no-check-certificate", "client-cert")
PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"


def venv_root() -> Path:
    return Path(sys.prefix)


def config_text(proxy: str, cert: Path) -> str:
    """The venv pip.ini. Proxy and CA bundle only; never a verification bypass."""
    return ("# Written by tools/bootstrap/pip_trust.py. TLS verification stays on:\n"
            "# no trusted-host, no disabled certificate checks.\n"
            "[global]\n"
            f"proxy = {proxy}\n"
            f"cert = {cert}\n"
            "timeout = 30\n"
            "retries = 2\n")


def config_problems(text: str) -> list[str]:
    """What is wrong with a pip.ini for this project (empty list = fine)."""
    problems = []
    keys = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")) or "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        keys[key.lower()] = value
    for key in FORBIDDEN_KEYS:
        if key in keys:
            problems.append(f"forbidden:{key}")
    if not keys.get("proxy"):
        problems.append("missing:proxy")
    cert = keys.get("cert")
    if cert and not Path(cert).is_file():
        problems.append("cert_unreadable")
    return problems


def record_matches(site_packages: Path, dist_glob: str, member: str):
    """True/False: the installed file equals the hash in its RECORD; None if no entry."""
    for record in site_packages.glob(f"{dist_glob}/RECORD"):
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if row and row[0].replace("\\", "/") == member and len(row) > 1 and row[1].startswith("sha256="):
                data = (site_packages / member).read_bytes()
                actual = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                return actual == row[1].split("=", 1)[1]
    return None


def combined_bundle(certifi_pem: Path, extra_pem: Path, target: Path) -> Path:
    """certifi + one extra PEM, written to ``target``; certifi's file is untouched."""
    extra = extra_pem.read_bytes()
    if PEM_BEGIN not in extra:
        raise ValueError("extra CA file contains no PEM certificate")
    ssl.create_default_context(cafile=str(extra_pem))  # must parse as CA material
    target.write_bytes(certifi_pem.read_bytes().rstrip(b"\n") + b"\n\n# Extra CA added by pip_trust.py\n" + extra)
    return target


def tls_through_proxy(host: str, cafile: str, proxy: str = PROXY, timeout: float = 10) -> str:
    """Handshake via CONNECT with hostname and chain verification; returns a one-line verdict."""
    proxy_host, proxy_port = proxy.split("://", 1)[-1].rsplit(":", 1)
    try:
        conn = http.client.HTTPConnection(proxy_host, int(proxy_port), timeout=timeout)
        conn.set_tunnel(host, 443)
        conn.connect()
        tls = ssl.create_default_context(cafile=cafile).wrap_socket(conn.sock, server_hostname=host)
        issuer = dict(x[0] for x in tls.getpeercert().get("issuer", ()))
        tls.close()
        return f"OK verified, issuer O={issuer.get('organizationName')}"
    except Exception as exc:  # noqa: BLE001
        return f"FAIL {type(exc).__name__}"


def check(venv: Path) -> int:
    import certifi
    site = Path(certifi.__file__).parents[1]
    ok = True
    for dist, member in (("certifi-*.dist-info", "certifi/cacert.pem"),
                         ("pip-*.dist-info", "pip/_vendor/certifi/cacert.pem")):
        state = record_matches(site, dist, member)
        print(f"bundle {member}: {'unmodified' if state else 'MODIFIED' if state is False else 'no RECORD entry'}")
        ok = ok and state is not False
    pip_ini = venv / "pip.ini"
    if pip_ini.is_file():
        problems = config_problems(pip_ini.read_text(encoding="utf-8"))
        print(f"venv pip.ini: {'ok' if not problems else ', '.join(problems)}")
        ok = ok and not problems
    else:
        print("venv pip.ini: absent (pip then needs HTTP(S)_PROXY in the environment)")
    for host in CHECK_HOSTS:
        verdict = tls_through_proxy(host, certifi.where())
        print(f"TLS via proxy {host}: {verdict}")
        ok = ok and verdict.startswith("OK")
    return 0 if ok else 1


def write_venv_config(venv: Path, proxy: str, extra_ca: Path | None) -> Path:
    import certifi
    cert = Path(certifi.where())
    if extra_ca is not None:
        cert = combined_bundle(cert, extra_ca, venv / "agenthub-ca-bundle.pem")
    text = config_text(proxy, cert)
    assert not config_problems(text), "generated config must pass its own check"
    target = venv / "pip.ini"
    target.write_text(text, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write-venv-config", action="store_true")
    parser.add_argument("--proxy", default=PROXY)
    parser.add_argument("--extra-ca", type=Path, help="PEM of an intercepting proxy's CA (not needed today)")
    args = parser.parse_args()
    venv = venv_root()
    if args.write_venv_config:
        print(f"written: {write_venv_config(venv, args.proxy, args.extra_ca)}")
    return check(venv)


if __name__ == "__main__":
    raise SystemExit(main())
