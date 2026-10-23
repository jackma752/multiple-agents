"""Staging a pinned Codex runtime and its zero-model Step A. Offline: no Codex is executed."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STEP = load("codex_runtime_step_a_under_test", "tools/probes/codex_runtime_step_a.py")
STAGE = STEP.STAGE  # the very module Step A reads its candidates from
S3 = load("s3_probe_for_runtime_test", "tools/probes/appserver_s3.py")
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER  # noqa: E402

VALID = {"status": "Valid", "subject": "CN=test", "thumbprint": "T", "not_after": "2099-01-01T00:00:00",
         "timestamper": "CN=tsa"}
real_baseline = pytest.mark.skipif(not STEP.BASELINE.is_dir(), reason="local alpha.11 Step A capture not present")


def fake_candidate(tmp_path, monkeypatch, *, purpose="runtime", label="test"):
    """A candidate built from small local files, registered in CANDIDATES for this test only."""
    src = tmp_path / "src"
    src.mkdir()
    files = []
    for name, body in (("codex.exe", b"codex bytes"), ("codex-command-runner.exe", b"runner"),
                       ("path/rg.exe", b"rg")):
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files.append((path, name, STAGE.sha256(path), len(body)))
    monkeypatch.setitem(STAGE.CANDIDATES, label, {"purpose": purpose, "version_inferred": "codex-cli 9.9.9-test",
                                                  "files": files, "missing": []})
    return files


def no_exec(monkeypatch):
    """Fail the test if anything tries to start a process (signatures are faked)."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"must not execute anything: {args[0] if args else kwargs}")
    monkeypatch.setattr(STAGE.subprocess, "run", refuse)
    monkeypatch.setattr(STEP.subprocess, "run", refuse)


# ---------------------------------------------------------------- staging


def test_the_candidates_pin_the_reviewed_files():
    runtime, archive = STAGE.CANDIDATES["alpha.11.2"], STAGE.CANDIDATES["alpha.11"]
    names = [f[1] for f in runtime["files"]]
    assert names == ["codex.exe", "codex-command-runner.exe", "codex-windows-sandbox-setup.exe",
                     "codex-code-mode-host.exe", "path/rg.exe"]
    assert runtime["files"][0][2:] == ("c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a", 336134960)
    assert runtime["purpose"] == "runtime" and runtime["missing"] == []
    # The archive is exactly the binary S3-S6-3 ran, and it says what it lacks.
    path, digest, size = S3.S3_CODEX_IDENTITY
    assert archive["files"][0][2:] == (digest, size) == (PINNED_APPSERVER["sha256"], PINNED_APPSERVER["size"])
    assert archive["purpose"] == "archive"
    assert archive["missing"] == ["codex-windows-sandbox-setup.exe", "codex-code-mode-host.exe"]
    for label in STAGE.CANDIDATES:
        folder = STAGE.target_dir(label, Path("R"))
        assert folder.parent == Path("R") and folder.name.startswith("sha256-") and len(folder.name) == 23


def test_staging_copies_verifies_and_records_provenance(tmp_path, monkeypatch):
    files = fake_candidate(tmp_path, monkeypatch)
    no_exec(monkeypatch)
    root = tmp_path / "runtimes"
    result = STAGE.stage("test", root=root, sign=lambda p: VALID, now="2026-10-23T21:00:00")
    folder = STAGE.target_dir("test", root)
    assert result["copied"] == ["codex.exe", "codex-command-runner.exe", "path/rg.exe"]
    assert result["problems"] == [] and result["signatures_valid"] is True
    for _src, name, digest, _size in files:
        assert STAGE.sha256(folder / name) == digest
        assert not (folder / name).stat().st_mode & 0o200  # staged read-only
        assert not (folder / (name + ".partial")).exists()
    provenance = json.loads((folder / STAGE.PROVENANCE).read_text(encoding="utf-8"))
    assert provenance["version_verified"] is None and provenance["staged_at"] == "2026-10-23T21:00:00"
    assert [f["sha256"] for f in provenance["files"]] == [f[2] for f in files]
    assert all(f["signature"]["status"] == "Valid" and f["source"] for f in provenance["files"])


def test_a_source_that_changed_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    files = fake_candidate(tmp_path, monkeypatch)
    files[-1][0].write_bytes(b"tampered")
    root = tmp_path / "runtimes"
    with pytest.raises(STAGE.Refused, match="source_changed:path/rg.exe"):
        STAGE.stage("test", root=root, sign=lambda p: VALID)
    assert not root.exists()


def test_a_missing_source_is_refused(tmp_path, monkeypatch):
    files = fake_candidate(tmp_path, monkeypatch)
    files[0][0].unlink()
    with pytest.raises(STAGE.Refused, match="source_missing:codex.exe"):
        STAGE.stage("test", root=tmp_path / "runtimes", sign=lambda p: VALID)


def test_staging_again_only_confirms_and_never_overwrites(tmp_path, monkeypatch):
    fake_candidate(tmp_path, monkeypatch)
    root = tmp_path / "runtimes"
    STAGE.stage("test", root=root, sign=lambda p: VALID, now="first")
    again = STAGE.stage("test", root=root, sign=lambda p: VALID, now="second")
    assert again["already_staged"] is True and again["copied"] == []
    folder = STAGE.target_dir("test", root)
    assert json.loads((folder / STAGE.PROVENANCE).read_text(encoding="utf-8"))["staged_at"] == "first"
    staged = folder / "codex-command-runner.exe"
    staged.chmod(0o666)
    staged.write_bytes(b"replaced later")
    with pytest.raises(STAGE.Refused, match="target_differs:codex-command-runner.exe"):
        STAGE.stage("test", root=root, sign=lambda p: VALID)
    assert STAGE.verify("test", root) == ["changed:codex-command-runner.exe"]


def test_an_invalid_signature_is_recorded_and_fails_the_stage(tmp_path, monkeypatch):
    fake_candidate(tmp_path, monkeypatch)
    result = STAGE.stage("test", root=tmp_path / "runtimes",
                         sign=lambda p: {"status": "NotSigned"} if p.name == "rg.exe" else VALID)
    assert result["signatures_valid"] is False and result["unsigned"] == ["path/rg.exe"]


# ---------------------------------------------------------------- Step A


def staged(tmp_path, monkeypatch, **kw):
    fake_candidate(tmp_path, monkeypatch, **kw)
    root = tmp_path / "runtimes"
    STAGE.stage("test", root=root, sign=lambda p: VALID)
    return root


def tiny_baseline(tmp_path):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "a.json").write_text(json.dumps({"definitions": {"X": {"type": "object"}}}), encoding="utf-8")
    return base, STEP.manifest(base)


def test_capture_refuses_without_approval_and_never_executes(monkeypatch, capsys):
    no_exec(monkeypatch)
    monkeypatch.setattr("sys.argv", ["step-a", "--capture", "alpha.11.2"])
    assert STEP.main() == 2
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("damage, problem", [
    ("change", "changed:codex.exe"),
    ("signature", "signature:path/rg.exe:HashMismatch"),
    ("baseline", "baseline_changed"),
    ("archive", "not_a_runtime"),
])
def test_capture_stops_before_executing(tmp_path, monkeypatch, damage, problem):
    root = staged(tmp_path, monkeypatch, purpose="archive" if damage == "archive" else "runtime")
    base, digest = tiny_baseline(tmp_path)
    if damage == "change":
        exe = STAGE.target_dir("test", root) / "codex.exe"
        exe.chmod(0o666)
        exe.write_bytes(b"other")
    if damage == "baseline":
        (base / "a.json").write_text("{}", encoding="utf-8")
    sign = (lambda p: {"status": "HashMismatch"} if p.name == "rg.exe" else VALID) if damage == "signature" \
        else (lambda p: VALID)
    no_exec(monkeypatch)
    code = STEP.capture("test", root=root, baseline=base, baseline_manifest=digest,
                        run_root=tmp_path / "runs", sign=sign)
    report = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text(encoding="utf-8"))
    assert code == 2 and problem in report["preflight"]["problems"]
    assert report["steps"] == [] and report["model_turns"] == 0


def test_capture_with_a_faked_cli_runs_only_the_three_local_commands(tmp_path, monkeypatch):
    root = staged(tmp_path, monkeypatch)
    base, digest = tiny_baseline(tmp_path)
    calls = []

    class Done:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["CODEX_HOME"] != r"C:\Users\Administrator\AppData\Local\AgentHub\codex-home"
        assert Path(kwargs["cwd"]) == Path(kwargs["env"]["CODEX_HOME"])
        if argv[1:] == ["--version"]:
            return Done("codex-cli 9.9.9-test\n")
        if argv[1:] == ["app-server", "generate-json-schema", "--help"]:
            return Done("Usage: ... --out <DIR> ... --experimental")
        out = Path(argv[-1])
        shutil.copytree(base, out)
        (out / "ServerNotification.json").write_text("{}", encoding="utf-8")
        return Done("")

    monkeypatch.setattr(STEP.subprocess, "run", fake_run)
    monkeypatch.setattr(STEP, "facts", lambda capture: {"fact": True})
    code = STEP.capture("test", root=root, baseline=base, baseline_manifest=digest, run_root=tmp_path / "runs",
                        sign=lambda p: VALID)
    report = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text(encoding="utf-8"))
    assert code == 0 and report["model_turns"] == 0
    assert all(Path(c[0]) == STAGE.target_dir("test", root) / "codex.exe" for c in calls)
    assert [c[1:4] for c in calls] == [["--version"], ["app-server", "generate-json-schema", "--help"],
                                       ["app-server", "generate-json-schema", "--experimental"]]
    assert all("--stdio" not in c and "turn/start" not in " ".join(c) for c in calls)
    assert report["version"] == "codex-cli 9.9.9-test" and report["version_matches_inference"] is True
    assert report["schema"]["added"] == ["ServerNotification.json"]
    assert report["verdict"].startswith("COMPATIBLE_REVIEW_DIFF") and report["scratch_home_removed"] is True


def test_a_hanging_command_is_a_stop_and_the_report_still_shows_the_cleanup(tmp_path, monkeypatch):
    root = staged(tmp_path, monkeypatch)
    base, digest = tiny_baseline(tmp_path)

    def hang(argv, **kwargs):
        raise STEP.subprocess.TimeoutExpired(argv, STEP.COMMAND_TIMEOUT)

    monkeypatch.setattr(STEP.subprocess, "run", hang)
    code = STEP.capture("test", root=root, baseline=base, baseline_manifest=digest, run_root=tmp_path / "runs",
                        sign=lambda p: VALID)
    report = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text(encoding="utf-8"))
    assert code == 6 and report["verdict"] == "STOP: TimeoutExpired" and report["model_turns"] == 0
    assert report["scratch_home_removed"] is True and "codex_processes_after" in report


def test_schema_diff_names_files_and_definitions(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        root.mkdir()
    (old / "same.json").write_text('{"a": 1}', encoding="utf-8")
    (new / "same.json").write_text('{"a": 1}', encoding="utf-8")
    (old / "gone.json").write_text("{}", encoding="utf-8")
    (new / "fresh.json").write_text("{}", encoding="utf-8")
    (old / "defs.json").write_text(json.dumps({"title": "T", "definitions": {"A": {"x": 1}, "B": {}}}), encoding="utf-8")
    (new / "defs.json").write_text(json.dumps({"title": "U", "definitions": {"A": {"x": 2}, "B": {}, "C": {}}}),
                                   encoding="utf-8")
    diff = STEP.schema_diff(old, new)
    assert diff["identical"] is False and diff["added"] == ["fresh.json"] and diff["removed"] == ["gone.json"]
    assert diff["changed"] == ["defs.json"]
    assert diff["definitions_changed"] == {"defs.json": ["A", "C", "<top level>"]}


@real_baseline
def test_every_fact_is_present_in_the_real_alpha11_capture():
    result = STEP.compare(STEP.BASELINE, STEP.BASELINE)
    assert result["missing_in_old"] == [] and result["compatible"] is True
    assert result["schema"]["identical"] is True and result["verdict"].startswith("IDENTICAL_SCHEMA")
    prefixes = {k.split("|")[0] for k in STEP.facts(STEP.BASELINE)}
    assert prefixes == {"s3", "s4", "s5", "adapter"} and result["facts_checked"] >= 60
    assert STEP.manifest(STEP.BASELINE) == STEP.BASELINE_MANIFEST


@real_baseline
def test_a_capture_missing_an_adapter_method_stops(tmp_path):
    copy = tmp_path / "capture"
    shutil.copytree(STEP.BASELINE, copy)
    path = copy / "ClientRequest.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"turn/interrupt"', '"turn/interruptX"'),
                    encoding="utf-8")
    result = STEP.compare(STEP.BASELINE, copy)
    assert result["compatible"] is False and result["verdict"].startswith("STOP")
    assert {"s4|request:turn/interrupt", "adapter|request:turn/interrupt"} <= set(result["missing_in_new"])
    assert result["schema"]["changed"] == ["ClientRequest.json"]
