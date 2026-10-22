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


def test_extra_ca_without_a_certificate_is_refused(tmp_path):
    base = tmp_path / "base.pem"
    base.write_text("x", encoding="utf-8")
    bogus = tmp_path / "bogus.pem"
    bogus.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(ValueError):
        T.combined_bundle(base, bogus, tmp_path / "bundle.pem")
    assert not (tmp_path / "bundle.pem").exists()
