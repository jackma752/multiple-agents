r"""Project pip / certifi / proxy trust: check it, or configure the venv. Verification stays ON.

Findings on this machine (2026-10-22, see docs/codex-proxy-setup.md section 7):

* Direct HTTPS to pypi.org / files.pythonhosted.org times out. Through the
  local proxy ``http://127.0.0.1:8090`` it works, and the proxy is a plain
  CONNECT tunnel: the servers present public chains (GlobalSign, Google Trust
  Services) that verify against certifi, pip's vendored certifi and the system
  store. So pip "hangs" only when it is not told to use the proxy; no private
  proxy CA is involved and none has to be trusted.
* Both certifi bundles match the hashes pip recorded at install time.
* Since the upgrade to pip 26.2.2 / certifi 2026.10.19, pip verifies through
  the Windows trust store (its bundled truststore); ``cert`` only ADDS CAs.
  Verification is on: PyPI addressed by IP is refused with a certificate error.

Modes:

``--check`` (default, read-only)
    certifi bundle integrity against RECORD; the venv's pip.ini (proxy present,
    no ``trusted-host``, ``cert`` readable); TLS through the proxy with
    hostname and chain verification, including chatgpt.com with the certifi
    bundle that Codex gets as ``CODEX_CA_CERTIFICATE``.

``--negative-control``
    additionally run ``pip index versions certifi`` three times (metadata only):
    PyPI by IP (must be refused with a certificate error), with a bundle that
    holds only an unrelated root (records whether ``cert`` replaces or adds to
    the trust store), and the configured setup (must succeed).

``--write-venv-config``
    write ``<venv>/pip.ini`` (site scope: this venv only) with the proxy, a
    timeout and ``cert`` = the venv's certifi bundle. With ``--extra-ca PEM``
    a separate bundle ``<venv>/agenthub-ca-bundle.pem`` = certifi + that PEM is
    written and used instead, for a proxy that does intercept TLS. certifi's own
    file is never modified, and ``trusted-host`` is never written.

No model is called and no package is downloaded (``pip index`` reads index metadata).
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
    # Codex child processes get CODEX_CA_CERTIFICATE = this same certifi bundle.
    verdict = tls_through_proxy("chatgpt.com", certifi.where())
    print(f"Codex CA bundle, TLS via proxy chatgpt.com: {verdict}")
    return 0 if ok and verdict.startswith("OK") else 1


def single_ca_bundle(certifi_pem: Path, issuer_marker: str, target: Path) -> Path:
    """One CA out of certifi (chosen by its ``# Issuer:`` line): a bundle that cannot verify PyPI."""
    text = certifi_pem.read_text(encoding="ascii")
    blocks = ["# Issuer: " + part for part in text.split("# Issuer: ")[1:]]
    chosen = [b for b in blocks if b.startswith(f"# Issuer: {issuer_marker}")]
    if len(chosen) != 1 or chosen[0].count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError(f"expected exactly one certifi entry for {issuer_marker!r}")
    target.write_text(chosen[0].strip() + "\n", encoding="ascii")
    return target


def pip_index(python: str, extra_args: list[str], timeout: float = 90) -> tuple[int, str]:
    """``pip index versions certifi`` (metadata only); returns (exit code, error class seen)."""
    import subprocess
    proc = subprocess.run([python, "-m", "pip", "index", "versions", "certifi", "--retries", "0",
                           "--timeout", "15", "--disable-pip-version-check", *extra_args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    output = proc.stdout + proc.stderr
    # pip's own wording first; through truststore the detail is the (localized) OS message.
    seen = next((m for m in ("problem confirming the ssl certificate", "CERTIFICATE_VERIFY_FAILED",
                             "SSLCertVerificationError", "SSLError") if m in output), "")
    return proc.returncode, seen


def negative_control(venv: Path) -> int:
    """Prove pip verifies certificates, and record what ``cert`` means for this pip.

    1. PyPI addressed by IP instead of name: the certificate cannot match, so
       pip MUST refuse with a certificate error (verification is on).
    2. ``--cert`` with a bundle that holds only an unrelated root: pip 24.2+
       verifies through the system store (bundled truststore) and treats
       ``cert`` as additional CAs, so this still succeeds; older pip refuses.
       Recorded, not judged.
    3. The configured setup must succeed.
    """
    import certifi
    import socket
    import tempfile
    python = sys.executable
    ip = socket.getaddrinfo("pypi.org", 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    mismatch_code, mismatch_seen = pip_index(python, ["--index-url", f"https://{ip}/simple/"])
    with tempfile.TemporaryDirectory() as tmp:
        lone = single_ca_bundle(Path(certifi.where()), "CN=Amazon Root CA 1", Path(tmp) / "amazon-only.pem")
        lone_code, lone_seen = pip_index(python, ["--cert", str(lone)])
    ok_code, _ = pip_index(python, [])
    print(f"negative control (PyPI by IP, name cannot match): exit {mismatch_code}, "
          f"{mismatch_seen or 'no certificate error seen'}")
    print(f"cert semantics (bundle with only Amazon Root CA 1): exit {lone_code} -> "
          f"{'cert is ADDITIVE to the system store' if lone_code == 0 else 'cert REPLACES the trust store'}"
          f"{' (' + lone_seen + ')' if lone_seen else ''}")
    print(f"positive control (configured setup): exit {ok_code}")
    return 0 if (mismatch_code != 0 and mismatch_seen and ok_code == 0) else 1


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
    parser.add_argument("--negative-control", action="store_true",
                        help="also prove pip refuses PyPI with a CA bundle that cannot verify it")
    args = parser.parse_args()
    venv = venv_root()
    if args.write_venv_config:
        print(f"written: {write_venv_config(venv, args.proxy, args.extra_ca)}")
    status = check(venv)
    if args.negative_control:
        status = status or negative_control(venv)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
