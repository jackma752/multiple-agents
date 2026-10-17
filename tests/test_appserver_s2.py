import importlib.util
import json
from pathlib import Path

import pytest

from agenthub_adapters import appserver as A
from agenthub_adapters.appserver_s2_evidence import S2Evidence


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s2_probe_under_test", ROOT / "tools/probes/appserver_s2.py")
S2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S2)


class RecordingTransport:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))


def ready_client():
    transport = RecordingTransport()
    client = S2.S2Client(transport, workdir="isolated", timeout=5)
    client._send({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "agent-hub-s2", "version": "0.1.0"}}})
    client._dispatch({"id": 1, "result": {}})
    client.notify("initialized")
    client._send({"id": 2, "method": "thread/start", "params": client.thread_params()})
    client._dispatch({"id": 2, "result": {"thread": {"id": "t"}}})
    client.evidence = S2Evidence("t")
    return client, transport


@pytest.mark.parametrize("approved,risk", [(False, False), (True, False), (False, True)])
def test_real_mode_requires_both_acknowledgements_before_side_effects(monkeypatch, tmp_path, approved, risk):
    target = tmp_path / "not-created"
    monkeypatch.setattr(S2, "RUN_ROOT", target)
    monkeypatch.setattr(S2.S1, "preflight", lambda *_: pytest.fail("must not access home or binary"))
    monkeypatch.setattr(S2.A, "StdioTransport", lambda *a, **kw: pytest.fail("must not launch"))
    assert S2.run_probe(real=True, approved=approved, acknowledge_risk=risk) == 2
    assert not target.exists()


@pytest.mark.parametrize("method", ["thread/start", "turn/start", "thread/resume", "thread/fork", "turn/interrupt", "command/exec"])
def test_nothing_can_be_sent_before_handshake(method):
    transport = RecordingTransport()
    client = S2.S2Client(transport, workdir="isolated", timeout=1)
    with pytest.raises(A.AppServerProtocolError):
        client._send({"id": 1, "method": method, "params": {}})
    assert transport.messages == []


def test_initialize_response_required_before_initialized():
    transport = RecordingTransport()
    client = S2.S2Client(transport, workdir="isolated", timeout=1)
    client._send({"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "agent-hub-s2", "version": "0.1.0"}}})
    with pytest.raises(A.AppServerProtocolError):
        client.notify("initialized")
    assert len(transport.messages) == 1


@pytest.mark.parametrize("mutation", ["prompt", "model", "sandbox", "approval"])
def test_turn_payload_is_fixed_before_transmission(mutation):
    client, transport = ready_client()
    params = client.turn_params()
    if mutation == "prompt":
        params["input"][0]["text"] = "different"
    elif mutation == "model":
        params["model"] = "different"
    elif mutation == "sandbox":
        params["sandboxPolicy"] = {"type": "dangerFullAccess"}
    else:
        params["approvalPolicy"] = "never"
    with pytest.raises(A.AppServerProtocolError):
        client._send({"id": 3, "method": "turn/start", "params": params})
    assert len(transport.messages) == 3 and client.turn_attempts == 0


def test_exactly_one_turn_and_never_answer_an_approval():
    client, transport = ready_client()
    client._send({"id": 3, "method": "turn/start", "params": client.turn_params()})
    with pytest.raises(A.AppServerProtocolError):
        client._send({"id": 4, "method": "turn/start", "params": client.turn_params()})
    with pytest.raises(A.AppServerProtocolError):
        client.respond("approval", {"decision": "accept"})
    assert len(transport.messages) == 4 and client.turn_attempts == 1


def test_server_request_is_not_auto_approved():
    client, transport = ready_client()
    with pytest.raises(A.AppServerProtocolError):
        client._dispatch({"id": "req", "method": "item/commandExecution/requestApproval", "params": {}})
    assert len(transport.messages) == 3 and client.violation


def test_extra_thread_detected_even_before_turn_response():
    client, transport = ready_client()
    with pytest.raises(A.AppServerProtocolError):
        client._dispatch({"method": "thread/started", "params": {"thread": {"id": "child"}}})
    assert client.violation and len(transport.messages) == 3


def test_turn_activity_before_approved_send_is_rejected():
    client, transport = ready_client()
    with pytest.raises(A.AppServerProtocolError):
        client._dispatch({"method": "turn/started", "params": {
            "threadId": "t", "turn": {"id": "unexpected", "status": "inProgress"}}})
    assert client.violation and client.turn_attempts == 0
    assert len(transport.messages) == 3


def test_partial_turn_write_cannot_retry():
    client, transport = ready_client()

    def broken_write(_):
        raise OSError("partial write")

    transport.write = broken_write
    with pytest.raises(A.AppServerDisconnected):
        client._send({"id": 3, "method": "turn/start", "params": client.turn_params()})
    with pytest.raises(A.AppServerProtocolError):
        client._send({"id": 4, "method": "turn/start", "params": client.turn_params()})
    assert client.turn_attempts == 1


@pytest.mark.parametrize("scenario,passed,terminal", [
    ("happy", True, True), ("turn-failed", False, True), ("no-usage", False, True),
    ("wrong-turn", False, False), ("approval", False, False), ("tool", False, False),
    ("disconnect", False, False), ("timeout", False, False),
])
def test_actual_python_process_positive_and_negative_controls(monkeypatch, tmp_path, scenario, passed, terminal):
    monkeypatch.setattr(S2, "RUN_ROOT", tmp_path)
    work_root = tmp_path / "work"
    work_root.mkdir()
    monkeypatch.setattr(S2, "WORK_ROOT", work_root)
    monkeypatch.setattr(S2.S1, "preflight", lambda *_: pytest.fail("offline must not access real home or Codex"))
    assert S2.run_probe(scenario=scenario) == (0 if passed else 1)
    result = json.loads(next(tmp_path.glob("*/result.json")).read_text(encoding="utf-8"))
    assert result["passed"] is passed
    assert result["terminal_confirmed"] is terminal
    assert result["turn_attempts"] == 1 and result["process_stopped"]
    assert result["usage_source"] == "synthetic_fixture"
    assert [m["method"] for m in result["sent"]] == ["initialize", "initialized", "thread/start", "turn/start"]
    assert result["manual_review_required"] is (not terminal)
    assert result["workdir_empty"]
    assert result["workdir_removed"]
    assert not Path(result["workdir"]).exists()
    if passed:
        assert result["usage"]["inputTokens"] == 10
        assert result["final_reply"] == "OK"


def test_real_mode_rejects_stub_outcome_selection_before_side_effects(monkeypatch, tmp_path):
    target = tmp_path / "not-created"
    monkeypatch.setattr(S2, "RUN_ROOT", target)
    with pytest.raises(ValueError):
        S2.run_probe(real=True, approved=True, acknowledge_risk=True, scenario="turn-failed")
    assert not target.exists()


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_workdir_inspection_failure_keeps_completed_turn_evidence(monkeypatch, tmp_path, error):
    monkeypatch.setattr(S2, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(S2, "WORK_ROOT", tmp_path)
    original = Path.iterdir

    def failing_inspection(path):
        if path.name.startswith("agenthub-s2-work-"):
            raise error("unavailable")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", failing_inspection)
    assert S2.run_probe() == 1
    result = json.loads(next(tmp_path.glob("stub-*/result.json")).read_text(encoding="utf-8"))
    assert result["turn_attempts"] == 1 and result["process_stopped"]
    assert result["terminal_confirmed"] and not result["passed"]
    assert result["workdir_check_error_type"] == error.__name__
    assert result["manual_review_required"]
