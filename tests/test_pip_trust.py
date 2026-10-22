"""tools/bootstrap/pip_trust.py: offline only (no network, no downloads)."""
import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pip_trust_under_test", ROOT / "tools/bootstrap/pip_trust.py")
T = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(T)


def test_generated_config_sets_proxy_and_ca_and_never_bypasses_verification(tmp_path):
    cert = tmp_path / "cacert.pem"
    cert.write_text("x", encoding="utf-8")
    text = T.config_text("http://127.0.0.1:8090", cert)
    assert "proxy = http://127.0.0.1:8090" in text and f"cert = {cert}" in text
    assert T.config_problems(text) == []
    assert "trusted-host" not in text.split("\n", 2)[2]  # only mentioned in the comment


@pytest.mark.parametrize("extra,problem", [
    ("trusted-host = pypi.org", "forbidden:trusted-host"),
    ("trusted_host = files.pythonhosted.org", "forbidden:trusted_host"),
    ("no-check-certificate = true", "forbidden:no-check-certificate"),
])
def test_verification_bypasses_are_flagged(tmp_path, extra, problem):
    text = f"[global]\nproxy = http://127.0.0.1:8090\n{extra}\n"
    assert problem in T.config_problems(text)


def test_missing_proxy_and_unreadable_cert_are_flagged(tmp_path):
    problems = T.config_problems(f"[global]\ncert = {tmp_path / 'absent.pem'}\n")
    assert problems == ["missing:proxy", "cert_unreadable"]


def make_record(site, member, data, *, tamper=False):
    (site / member).parent.mkdir(parents=True, exist_ok=True)
    (site / member).write_bytes(data + (b"tampered" if tamper else b""))
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    info = site / "certifi-1.dist-info"
    info.mkdir(exist_ok=True)
    (info / "RECORD").write_text(f"{member},sha256={digest},{len(data)}\n", encoding="utf-8")


@pytest.mark.parametrize("tamper,expected", [(False, True), (True, False)])
def test_bundle_integrity_against_install_record(tmp_path, tamper, expected):
    make_record(tmp_path, "certifi/cacert.pem", b"-----BEGIN CERTIFICATE-----\n", tamper=tamper)
    assert T.record_matches(tmp_path, "certifi-*.dist-info", "certifi/cacert.pem") is expected
    assert T.record_matches(tmp_path, "certifi-*.dist-info", "certifi/other.pem") is None


def test_extra_ca_goes_to_a_separate_bundle_and_certifi_is_untouched(tmp_path):
    import certifi
    base = tmp_path / "base.pem"
    base.write_bytes(Path(certifi.where()).read_bytes())
    before = base.read_bytes()
    extra = tmp_path / "proxy-ca.pem"
    # Any real CA certificate will do for the format check; take one from certifi itself.
    first = before.split(b"-----BEGIN CERTIFICATE-----", 2)[1].split(b"-----END CERTIFICATE-----")[0]
    extra.write_bytes(b"-----BEGIN CERTIFICATE-----" + first + b"-----END CERTIFICATE-----\n")
    target = T.combined_bundle(base, extra, tmp_path / "bundle.pem")
    assert base.read_bytes() == before
    assert target.read_bytes().count(b"-----BEGIN CERTIFICATE-----") == before.count(b"-----BEGIN CERTIFICATE-----") + 1


def test_single_ca_bundle_holds_exactly_the_chosen_root(tmp_path):
    import certifi
    target = T.single_ca_bundle(Path(certifi.where()), "CN=Amazon Root CA 1", tmp_path / "one.pem")
    data = target.read_text(encoding="ascii")
    assert data.count("-----BEGIN CERTIFICATE-----") == 1 and data.startswith("# Issuer: CN=Amazon Root CA 1")
    with pytest.raises(ValueError):
        T.single_ca_bundle(Path(certifi.where()), "CN=No Such Root", tmp_path / "none.pem")


@pytest.mark.parametrize("output,seen", [
    ("Could not fetch URL https://1.2.3.4/simple/x/: There was a problem confirming the ssl certificate: "
     "('localized OS message',) - skipping", "problem confirming the ssl certificate"),
    ("SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]'))", "CERTIFICATE_VERIFY_FAILED"),
    ("ERROR: No matching distribution found for certifi", ""),
])
def test_certificate_errors_are_recognised_in_pip_output(monkeypatch, output, seen):
    import subprocess

    class Done:
        returncode, stdout, stderr = 1, output, ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: Done())
    assert T.pip_index("python", []) == (1, seen)


def test_extra_ca_without_a_certificate_is_refused(tmp_path):
    base = tmp_path / "base.pem"
    base.write_text("x", encoding="utf-8")
    bogus = tmp_path / "bogus.pem"
    bogus.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ValueError):
        T.combined_bundle(base, bogus, tmp_path / "bundle.pem")
    assert not (tmp_path / "bundle.pem").exists()
