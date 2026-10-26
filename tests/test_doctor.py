"""tools/doctor.py is read-only: discovery and preflight must be provable without a real agent."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("doctor", ROOT / "tools/doctor.py")
D = importlib.util.module_from_spec(spec)
# dataclasses resolves annotations through sys.modules[cls.__module__], so the
# module must be registered before it is executed.
sys.modules["doctor"] = D
spec.loader.exec_module(D)


def fake_binary(tmp_path, name="codex.exe", payload=b"MZ fake binary"):
    path = tmp_path / name
    path.write_bytes(payload)
    return str(path)


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

def test_sha256_file_matches_hashlib(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abc" * 1000)
    assert D.sha256_file(str(path)) == hashlib.sha256(b"abc" * 1000).hexdigest()


def test_inspect_reports_missing_file_without_raising(tmp_path):
    cand = D.inspect("codex", str(tmp_path / "nope.exe"), "pinned", read_version_too=False)
    assert cand.exists is False
    assert cand.sha256 is None
    assert "FileNotFoundError" in cand.read_error or "WinError" in cand.read_error


def test_inspect_records_size_and_hash(tmp_path):
    path = fake_binary(tmp_path)
    cand = D.inspect("codex", path, "env", read_version_too=False)
    assert cand.exists is True
    assert cand.size == len(b"MZ fake binary")
    assert cand.sha256 == hashlib.sha256(b"MZ fake binary").hexdigest()
    assert cand.version is None and cand.version_error == ""


def test_read_version_never_raises_on_a_non_executable(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not a program", encoding="utf-8")
    version, error = D.read_version(str(path), timeout=5)
    assert version is None
    assert error  # OSError/SubprocessError text, whichever the platform raises


# --------------------------------------------------------------------------- #
# profile location and loading
# --------------------------------------------------------------------------- #

def test_profile_path_honours_explicit_override(tmp_path):
    env = {"AGENT_HUB_PROFILE": str(tmp_path / "p.json")}
    assert D.profile_path(env) == str(tmp_path / "p.json")


def test_profile_path_defaults_to_localappdata_and_host(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    path = D.profile_path(env)
    assert path.startswith(str(tmp_path))
    assert "AgentHub" in path and path.endswith(".json")


def test_load_profile_refuses_a_path_inside_the_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "profile.json"
    target.write_text("{}", encoding="utf-8")
    data, error = D.load_profile(str(target), repo_root=str(repo))
    assert data == {}
    assert "inside the repository" in error


def test_load_profile_absent_file_is_not_an_error(tmp_path):
    data, error = D.load_profile(str(tmp_path / "missing.json"), repo_root=str(tmp_path / "repo"))
    assert data == {} and error == ""


def test_load_profile_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    data, error = D.load_profile(str(path), repo_root=str(tmp_path / "repo"))
    assert data == {}
    assert "JSONDecodeError" in error


def test_load_profile_rejects_non_object_channels(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"channels": []}), encoding="utf-8")
    data, error = D.load_profile(str(path), repo_root=str(tmp_path / "repo"))
    assert data == {}
    assert "channels" in error


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def candidate(sha, size=10, exists=True):
    return D.Candidate(channel="codex", path="x", source="env", exists=exists,
                       size=size, sha256=sha)


def test_classify_missing_when_no_bytes(monkeypatch):
    monkeypatch.setattr(D, "_pinned_constant", lambda channel: {"sha256": "p", "size": 10})
    assert D.classify("codex", candidate(None, exists=False), {}) == "missing"


def test_classify_pinned_matches_the_constant(monkeypatch):
    monkeypatch.setattr(D, "_pinned_constant",
                        lambda channel, transport=None: {"sha256": "p", "size": 10})
    assert D.classify("codex", candidate("p"), {}) == "pinned"


def test_classify_adopted_beats_pinned(monkeypatch):
    monkeypatch.setattr(D, "_pinned_constant",
                        lambda channel, transport=None: {"sha256": "p", "size": 10})
    profile = {"channels": {"codex": {"sha256": "a", "size": 10}}}
    assert D.classify("codex", candidate("a"), profile) == "adopted"


def test_classify_unknown_bytes_are_only_a_candidate(monkeypatch):
    monkeypatch.setattr(D, "_pinned_constant",
                        lambda channel, transport=None: {"sha256": "p", "size": 10})
    assert D.classify("codex", candidate("other"), {}) == "candidate"


def test_adopted_entry_ignores_wrong_types():
    assert D.adopted_entry({"channels": {"codex": "nope"}}, "codex") == {}
    assert D.adopted_entry({}, "codex") == {}


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

def test_codex_candidates_put_profile_then_env_then_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_glob_candidates", lambda pattern: [])
    monkeypatch.setattr("shutil.which", lambda name: None)
    from agenthub_kernel import config

    monkeypatch.setattr(config, "PINNED_CODEX_PATH", str(tmp_path / "pinned.exe"))
    env = {"AGENT_HUB_CODEX_PATH": str(tmp_path / "env.exe"),
           "LOCALAPPDATA": str(tmp_path)}
    profile = {"channels": {"codex": {"path": str(tmp_path / "adopted.exe")}}}
    pairs = D.codex_candidates(env, profile)
    sources = [source for _, source in pairs]
    assert sources[:3] == ["profile", "env", "pinned"]
    assert len({p.lower() for p, _ in pairs}) == len(pairs)


def test_candidates_are_deduped_case_insensitively():
    pairs = [("C:\\A\\codex.exe", "env"), ("c:\\a\\CODEX.EXE", "pinned"), ("C:\\B\\codex.exe", "x")]
    out = D._dedupe(pairs)
    assert [source for _, source in out] == ["env", "x"]


# --------------------------------------------------------------------------- #
# report and rendering
# --------------------------------------------------------------------------- #

def _report(ok, checks):
    return {
        "host": "HOST",
        "repo_root": "C:\\repo",
        "profile": {"path": "C:\\p.json", "loaded": False, "error": ""},
        "common": [{"name": "python_version", "status": D.OK, "detail": "3.12.0"}],
        "channels": [{
            "channel": "codex",
            "status": "pinned",
            "candidate": {"path": "C:\\codex.exe", "source": "pinned", "size": 10,
                          "sha256": "a" * 64, "version": "codex-cli 1", "version_error": "",
                          "read_error": "", "exists": True},
            "seen": [],
            "checks": checks,
        }],
        "ok": ok,
    }


def test_build_report_is_ok_only_without_failing_checks(monkeypatch):
    monkeypatch.setattr(D, "common_checks", lambda env, write_check=False: [])
    monkeypatch.setattr(D, "codex_candidates", lambda env, profile: [])
    monkeypatch.setattr(D, "codex_checks", lambda cand, profile, env, transport=None: [])
    report = D.build_report(["codex"], {})
    assert report["ok"] is True
    monkeypatch.setattr(D, "codex_checks", lambda cand, profile, env, transport=None: [
        D.Check("identity", D.FAIL, "nope", "adopt it")])
    assert D.build_report(["codex"], {})["ok"] is False


def test_build_report_rejects_unknown_channel():
    with pytest.raises(ValueError):
        D.build_report(["nope"], {})


def test_render_text_shows_every_section():
    text = D.render_text(_report(True, [{"name": "identity", "status": D.OK, "detail": "ok"}]))
    assert "host        : HOST" in text
    assert "environment" in text
    assert "[codex] status = pinned" in text
    assert "verdict: all requested channels pass" in text


def test_render_text_prints_the_hint_on_its_own_line():
    text = D.render_text(_report(False, [
        {"name": "identity", "status": D.FAIL, "detail": "nope", "hint": "adopt it"}]))
    assert "-> adopt it" in text
    assert "at least one channel has a FAIL" in text


def test_main_prints_json_and_returns_nonzero_when_blocked(monkeypatch, capsys):
    monkeypatch.setattr(D, "run", lambda channels, write_check=False: _report(False, []))
    code = D.main(["--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["channels"][0]["channel"] == "codex"


def test_main_defaults_to_every_channel(monkeypatch):
    seen = {}

    def fake_run(channels, write_check=False):
        seen["channels"] = channels
        return _report(True, [])

    monkeypatch.setattr(D, "run", fake_run)
    assert D.main([]) == 0
    assert seen["channels"] == list(D.CHANNELS)


def test_codex_has_one_reviewed_constant_per_transport(monkeypatch):
    from agenthub_adapters import appserver_adapter
    from agenthub_kernel import config

    monkeypatch.setattr(config, "PINNED_CODEX_SHA256", "e" * 64)
    monkeypatch.setattr(config, "PINNED_CODEX_SIZE", 111)
    exec_pin = D._pinned_constant("codex", "exec")
    app_pin = D._pinned_constant("codex", "app-server")
    assert exec_pin["sha256"] == "e" * 64
    assert app_pin["sha256"] == appserver_adapter.PINNED_APPSERVER["sha256"]
    assert D.classify("codex", candidate(app_pin["sha256"], size=app_pin["size"]), {},
                      "app-server") == "pinned"
    assert D.classify("codex", candidate(app_pin["sha256"], size=app_pin["size"]), {},
                      "exec") == "candidate"


def test_pick_candidate_prefers_the_adopted_path_then_env_then_the_pin(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_pinned_constant",
                        lambda channel, transport=None: {"path": str(tmp_path / "pin.exe")})
    seen = [
        D.Candidate("codex", str(tmp_path / "pin.exe"), "pinned", True, 1, "p"),
        D.Candidate("codex", str(tmp_path / "env.exe"), "env", True, 1, "e"),
        D.Candidate("codex", str(tmp_path / "adopted.exe"), "profile", True, 1, "a"),
    ]
    profile = {"channels": {"codex": {"path": str(tmp_path / "adopted.exe")}}}
    assert D.pick_candidate("codex", seen, profile).path.endswith("adopted.exe")
    assert D.pick_candidate("codex", seen, {}).path.endswith("pin.exe")
    missing = [D.Candidate("codex", str(tmp_path / "gone.exe"), "pinned", False)]
    assert D.pick_candidate("codex", missing, {}).exists is False
    assert D.pick_candidate("codex", [], {}) is None


def test_pick_candidate_reports_the_missing_pin_rather_than_a_working_neighbour(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_pinned_constant",
                        lambda channel, transport=None: {"path": str(tmp_path / "pin.exe")})
    seen = [
        D.Candidate("codex", str(tmp_path / "pin.exe"), "pinned", False),
        D.Candidate("codex", str(tmp_path / "newer.exe"), "runtime", True, 1, "n"),
    ]
    picked = D.pick_candidate("codex", seen, {}, "exec")
    assert picked.path.endswith("pin.exe")
    assert picked.exists is False


def test_render_text_does_not_repeat_the_chosen_candidate_in_also_seen():
    report = _report(True, [{"name": "identity", "status": D.OK, "detail": "ok"}])
    report["channels"][0]["seen"] = [
        {"path": "C:\\codex.exe", "source": "pinned", "exists": True},
        {"path": "C:\\other.exe", "source": "installed", "exists": True},
    ]
    text = D.render_text(report)
    assert "also seen (1)" in text
    assert "other.exe" in text
    assert text.count("C:\\codex.exe") == 1
