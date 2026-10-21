import importlib.util
import json
from pathlib import Path

import pytest

from agenthub_adapters import appserver as A
from agenthub_adapters.appserver_s3_evidence import S3Evidence, COMMAND, APPROVAL


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s3_probe_under_test", ROOT / "tools/probes/appserver_s3.py")
S3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S3)


class RecordingTransport:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))


def settings():
    return {"thread": {"id": "t"}, "cwd": str(ROOT), "approvalPolicy": "on-request",
            "approvalsReviewer": "user", "sandbox": {"type": "readOnly", "networkAccess": False}}


def ready(*, turn=True):
    transport = RecordingTransport()
    client = S3.S3Client(transport, workdir=str(ROOT), timeout=5)
    client._send({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "agent-hub-s3", "version": "0.1.0"}}})
    client._dispatch({"id": 1, "result": {}})
    client.notify("initialized")
    client._send({"id": 2, "method": "thread/start", "params": client.thread_params()})
    client._dispatch({"id": 2, "result": settings()})
    client.evidence = S3Evidence("t")
    if turn:
        client.validate_thread_settings(settings())
        client._send({"id": 3, "method": "turn/start", "params": client.turn_params()})
    return client, transport


def pending(client):
    ids = {"threadId": "t", "turnId": "r"}
    client._dispatch({"method": "turn/started", "params": {"threadId": "t", "turn": {"id": "r", "status": "inProgress"}}})
    client._dispatch({"method": "item/started", "params": {**ids, "item": {
        "id": "cmd", "type": "commandExecution", "status": "inProgress", "command": COMMAND, "cwd": str(ROOT)}}})
    client._dispatch({"id": "req", "method": APPROVAL, "params": {**ids, "itemId": "cmd", "command": COMMAND}})
    client.evidence.bind_turn("r")


@pytest.mark.parametrize("approved,risk", [(False, False), (True, False), (False, True)])
def test_s3_real_mode_requires_new_double_ack_before_any_side_effect(monkeypatch, tmp_path, approved, risk):
    target = tmp_path / "absent"
    monkeypatch.setattr(S3, "RUN_ROOT", target)
    monkeypatch.setattr(S3.S2.S1, "preflight", lambda *_: pytest.fail("must not touch real home"))
    monkeypatch.setattr(S3.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S3.run_probe(real=True, approved=approved, acknowledge_risk=risk) == 2
    assert not target.exists()


@pytest.mark.parametrize("field,value", [
    ("approvalsReviewer", "auto_review"), ("approvalsReviewer", None),
    ("approvalPolicy", "never"), ("sandbox", {"type": "workspaceWrite"}),
    ("sandbox", {"type": "readOnly", "networkAccess": True}), ("sandbox", None),
    ("cwd", "another-directory"), ("cwd", None),
])
def test_effective_thread_settings_rejected_before_turn(field, value):
    client, transport = ready(turn=False)
    result = settings()
    result[field] = value
    with pytest.raises(A.AppServerProtocolError):
        client.validate_thread_settings(result)
    with pytest.raises(A.AppServerProtocolError):
        client._send({"id": 3, "method": "turn/start", "params": client.turn_params()})
    assert client.turn_attempts == 0 and len(transport.messages) == 3


@pytest.mark.parametrize("decision", ["accept", "acceptForSession", "cancel", {"acceptWithExecpolicyAmendment": {}}])
def test_s3_cannot_transmit_any_decision_other_than_fixed_decline(decision):
    client, transport = ready()
    pending(client)
    with pytest.raises(A.AppServerProtocolError):
        client.respond("req", {"decision": decision})
    assert len(transport.messages) == 4


def test_approval_response_is_bound_and_sent_once_only():
    client, transport = ready()
    pending(client)
    client.tick()
    client.tick()
    assert transport.messages[-1] == {"jsonrpc": "2.0", "id": "req", "result": {"decision": "decline"}}
    assert len(transport.messages) == 5 and client.evidence.response_written
    with pytest.raises(A.AppServerProtocolError):
        client.respond("req", {"decision": "decline"})
    assert len(transport.messages) == 5


def test_partial_response_write_cannot_retry():
    client, transport = ready()
    pending(client)

    def partial(_):
        raise OSError("partial write")

    transport.write = partial
    with pytest.raises(A.AppServerDisconnected):
        client.tick()
    client.tick()  # no retry after an attempt
    assert client.evidence.response_attempted and not client.evidence.response_written
    assert len(client.sent) == 5


@pytest.mark.parametrize("scenario,verdict,terminal", [
    ("happy", "PASS", True), ("selfreport", "INCONCLUSIVE", True),
    ("executed", "FAIL", False), ("wrong-turn", "FAIL", False),
    ("wrong-item", "FAIL", False), ("duplicate", "FAIL", False),
    ("no-resolved", "INCONCLUSIVE", True), ("failed-item", "INCONCLUSIVE", True),
    ("disconnect", "INCONCLUSIVE", False), ("timeout", "INCONCLUSIVE", False),
    ("failed-turn", "INCONCLUSIVE", True),
])
def test_actual_python_peer_positive_and_negative_controls(monkeypatch, tmp_path, scenario, verdict, terminal):
    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(S3.S2.S1, "preflight", lambda *_: pytest.fail("offline only"))
    assert S3.run_probe(scenario=scenario) == (0 if verdict == "PASS" else 1)
    result = json.loads(next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["probe_verdict"] == verdict
    assert result["terminal_confirmed"] is terminal
    assert result["process_stopped"] and result["turn_attempts"] == 1
    assert result["workdir_removed"] and not Path(result["workdir"]).exists()
    assert result["usage_source"] == "synthetic_fixture"
    assert [m["method"] for m in result["sent"][:4]] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert len(result["sent"]) <= 5
    for m in result["sent"][4:]:
        assert m["decision"] == "decline"
    if verdict == "PASS":
        assert all(result["checks"].values()) and result["final_reply"] == "DECLINED"


def test_marker_or_other_file_prevents_pass_and_is_preserved():
    result = {"passed": False, "workdir_empty": False, "probe_verdict": "PASS"}
    S3.S3Client.finalize_result(result)
    assert result["probe_verdict"] == "FAIL" and result["manual_review_required"]


@pytest.mark.parametrize("scenario", ["auto-review", "writable-sandbox"])
def test_effective_permission_mismatch_stops_process_before_any_turn(monkeypatch, tmp_path, scenario):
    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    assert S3.run_probe(scenario=scenario) == 1
    result = json.loads(next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["turn_attempts"] == 0 and result["process_stopped"]
    assert [m["method"] for m in result["sent"]] == ["initialize", "initialized", "thread/start"]
    assert not result["passed"]


def test_nonempty_workdir_is_kept_as_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(S3, "RUN_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(S3.S2, "WORK_ROOT", tmp_path)
    original = S3.S3Client.exercise

    def writes_marker(client):
        original(client)
        (Path(client.workdir) / "S3_EXECUTED.txt").write_text("synthetic fixture", encoding="utf-8")

    monkeypatch.setattr(S3.S3Client, "exercise", writes_marker)
    assert S3.run_probe() == 1
    result = json.loads(next((tmp_path / "evidence").glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["probe_verdict"] == "FAIL" and result["manual_review_required"]
    assert not result["workdir_removed"] and (Path(result["workdir"]) / "S3_EXECUTED.txt").is_file()
