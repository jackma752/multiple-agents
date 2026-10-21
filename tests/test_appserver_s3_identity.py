"""S3 re-pin and the zero-model identity check. Offline: no Codex is executed."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ID = load("s3_identity_under_test", "tools/probes/appserver_s3_identity.py")
S3 = load("s3_probe_for_identity_test", "tools/probes/appserver_s3.py")
REAL_OLD_CAPTURE = ROOT.parents[1] / "var" / "codex-schema"


def write_capture(root: Path, *, drop=None):
    """A minimal schema capture containing exactly the facts S3 relies on."""
    root.mkdir(parents=True)
    tag = lambda *values: {"oneOf": [{"properties": {"method": {"enum": [v]}}} for v in values]}
    notifications = [m for m in ID.NOTIFICATIONS if m != drop]
    v2 = {"definitions": {
        "ThreadStartParams": {"properties": dict.fromkeys(["approvalPolicy", "approvalsReviewer", "sandbox", "cwd"], {})},
        "ThreadStartResponse": {"properties": dict.fromkeys(["approvalPolicy", "approvalsReviewer", "sandbox", "cwd", "thread"], {})},
        "TurnStartParams": {"properties": dict.fromkeys(["threadId", "input", "approvalPolicy", "approvalsReviewer", "sandboxPolicy"], {})},
        "AskForApproval": {"oneOf": [{"enum": ["on-request", "never"]}]},
        "ApprovalsReviewer": {"enum": ["user"]},
        "SandboxMode": {"enum": ["read-only"]},
        "SandboxPolicy": {"oneOf": [{"properties": {"type": {"enum": ["readOnly"]}}}]},
        "CommandExecutionStatus": {"enum": [s for s in ("inProgress", "declined", "completed") if s != drop]},
        "ThreadItem": {"oneOf": [{"properties": {"type": {"enum": ["commandExecution"]}, **dict.fromkeys(
            ["id", "command", "cwd", "status", "processId", "exitCode", "aggregatedOutput"], {})}}]},
        "ServerRequestResolvedNotification": {"properties": dict.fromkeys(["threadId", "requestId"], {})},
    }}
    (root / "codex_app_server_protocol.v2.schemas.json").write_text(json.dumps(v2), encoding="utf-8")
    (root / "CommandExecutionRequestApprovalParams.json").write_text(json.dumps({"properties": dict.fromkeys(
        ["itemId", "threadId", "turnId", "command", "cwd", "approvalId", "availableDecisions"], {})}), encoding="utf-8")
    decisions = [d for d in ("accept", "decline") if d != drop]
    (root / "CommandExecutionRequestApprovalResponse.json").write_text(json.dumps(
        {"properties": {"decision": {"oneOf": [{"enum": decisions}]}}}), encoding="utf-8")
    (root / "ServerRequest.json").write_text(json.dumps(tag("item/commandExecution/requestApproval")), encoding="utf-8")
    (root / "ServerNotification.json").write_text(json.dumps(tag(*notifications)), encoding="utf-8")
    return root


def test_s3_is_pinned_to_its_own_identity_and_s1_keeps_its_history():
    path, digest, size = S3.S3_CODEX_IDENTITY
    assert path.endswith(r"bin\896a0166b774037a\codex.exe") and size == 341312816
    assert digest == "f6dbf48cb70f62649c645f26c87dc9bf84cc36986202dfad8e12fb5b8ca70312"
    assert S3.S2.S1.S1_CODEX_SHA256.startswith("e98c516f") and S3.S2.S1.S1_CODEX_SIZE == 334121776


def test_s3_real_mode_preflights_the_s3_identity(monkeypatch, tmp_path):
    seen = []

    def preflight(home, identity=None):
        seen.append(identity)
        raise S3.S2.S1.CheckFailed("stop here: offline test")

    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S3.S2.S1, "preflight", preflight)
    monkeypatch.setattr(S3.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S3.run_probe(real=True, approved=True, acknowledge_risk=True, codex_home=str(tmp_path / "home"),
                        scenario="happy") == 1
    assert seen == [S3.S3_CODEX_IDENTITY]
    result = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["process_started"] is False and result["turn_attempts"] == 0
    assert result["error_type"] == "CheckFailed"


def test_preflight_refuses_a_changed_file_for_an_explicit_identity(tmp_path):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"not the reviewed build")
    with pytest.raises(S3.S2.S1.CheckFailed):
        S3.S2.S1.preflight(str(tmp_path / "home"), (str(binary), "0" * 64, binary.stat().st_size))


def test_fact_check_accepts_a_complete_capture_and_names_missing_facts(tmp_path):
    old = write_capture(tmp_path / "old")
    assert ID.compare(old, write_capture(tmp_path / "same")) == {
        "compatible": True, "facts_checked": 21, "missing_in_old": [], "missing_in_new": []}
    for drop, fact in (("decline", "approval_response:decision has decline"),
                       ("declined", "command_status has inProgress,declined,completed"),
                       ("serverRequest/resolved", "notification:serverRequest/resolved")):
        result = ID.compare(old, write_capture(tmp_path / f"new-{drop.replace('/', '-')}", drop=drop))
        assert result["compatible"] is False and fact in result["missing_in_new"]


@pytest.mark.skipif(not REAL_OLD_CAPTURE.is_dir(), reason="local 2026-10-14 capture not present")
def test_fact_check_finds_every_fact_in_the_real_old_capture():
    result = ID.compare(REAL_OLD_CAPTURE, REAL_OLD_CAPTURE)
    assert result["compatible"] is True and result["facts_checked"] == 21


def test_capture_refuses_without_approval_and_never_executes(monkeypatch, capsys):
    monkeypatch.setattr(ID.subprocess, "run", lambda *a, **k: pytest.fail("must not execute Codex"))
    monkeypatch.setattr("sys.argv", ["identity", "--capture"])
    assert ID.main() == 2
    assert "REFUSED" in capsys.readouterr().out


def test_capture_stops_before_executing_a_changed_file(monkeypatch, tmp_path):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"x")
    monkeypatch.setattr(ID, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(ID.subprocess, "run", lambda *a, **k: pytest.fail("must not execute Codex"))
    assert ID.capture((str(binary), "0" * 64, 1)) == 2
    report = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text(encoding="utf-8"))
    assert report["pinned_identity_matches"] is False and report["model_turns"] == 0


@pytest.mark.parametrize("drop,code", [(None, 0), ("decline", 5)])
def test_capture_with_a_faked_cli_runs_only_the_three_local_commands(monkeypatch, tmp_path, drop, code):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"pinned")
    identity = (str(binary), ID.S1.sha256(str(binary)), binary.stat().st_size)
    calls = []

    class Done:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake_run(argv, **kwargs):
        calls.append(argv[1:])
        assert kwargs["env"]["CODEX_HOME"] != r"C:\Users\Administrator\AppData\Local\AgentHub\codex-home"
        if argv[1:] == ["--version"]:
            return Done("codex-cli 9.9.9-test\n")
        if argv[1:] == ["app-server", "generate-json-schema", "--help"]:
            return Done("Usage: ... --out <DIR> ... --experimental")
        write_capture(Path(argv[-1]), drop=drop)
        return Done("")

    monkeypatch.setattr(ID, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(ID, "OLD_CAPTURE", write_capture(tmp_path / "old"))
    monkeypatch.setattr(ID.subprocess, "run", fake_run)
    assert ID.capture(identity) == code
    assert [c[:3] for c in calls] == [["--version"], ["app-server", "generate-json-schema", "--help"],
                                      ["app-server", "generate-json-schema", "--experimental"]]
    assert all("turn/start" not in " ".join(c) and c[:2] != ["app-server", "--stdio"] for c in calls)
    report = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text(encoding="utf-8"))
    assert report["version"] == "codex-cli 9.9.9-test" and report["model_turns"] == 0
    assert report["compatible"] is (drop is None)
