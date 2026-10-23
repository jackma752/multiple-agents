"""S6-4 evidence collector, offline: a decline-only Hub run on the in-memory stub, then collect().

The stub's ``approval-default`` scenario has the S3 shape: a PowerShell-wrapped
command, the tested build's default decision list, and a reply of DECLINED
after a decline. No Codex process, no model call.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agenthub_adapters.appserver_adapter import AppServerCodexAdapter
from agenthub_adapters.appserver_stub import InMemoryStubFactory, StdioStubFactory
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import Database
from agenthub_rt.api import create_app
from tests.http.test_async_and_tool_approval import pending_card
from tests.http.test_conversations import USER

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s6_4_collect_under_test", ROOT / "tools/probes/appserver_s6_4_collect.py")
S64 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S64)

STUB = {"expected_inner": "Get-ChildItem -Name", "expected_binding": "appserver-stub:in-memory"}
# The stub as a real child process: the only offline run with a descendant report to judge.
STDIO = {**STUB, "expected_binding": "appserver-stub:stdio"}


def web_run(tmp_path, *, scenario="approval-default", mode="supervised", click=True, ttl=10, stdio=False):
    """What the page does: one conversation, one call, consent, run, decline on the card."""
    factory = StdioStubFactory(scenario) if stdio else InMemoryStubFactory(scenario)
    adapter = AppServerCodexAdapter(
        transport_factory=factory, workdir_root=str(tmp_path / "codex-runs"),
        store=ExecutionStore(str(tmp_path / "appserver-executions.db"), table="appserver_executions"))
    adapter.accept_enabled = False  # what the real factory gets: decline only
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'agent-hub.db').as_posix()}"), adapter=adapter)
    with TestClient(app) as client:
        app.state.worker.tool_approval_ttl = ttl
        cid = client.post("/api/conversations", headers=USER, json={"title": "S6-4"}).json()["conversation_id"]
        turn = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "text": "run the fixed command", "agent_id": "codex", "client_request_id": "s6-4",
            "execution_mode": mode}).json()
        body = {"accept_unmanaged_native_agents": True} if mode == "supervised" else None
        assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER, json=body).status_code == 200
        assert client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER).status_code == 202
        if scenario == "approval-default" and click:
            card = pending_card(client, cid)
            assert card["available_decisions"] == ["decline"]
            assert client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline",
                               headers=USER).status_code == 200
        assert app.state.worker.wait_idle(15)
    app.state.db.engine.dispose()
    return tmp_path


def test_a_clean_decline_through_the_web_path_passes(tmp_path):
    facts = S64.collect(web_run(tmp_path, stdio=True), **STDIO)
    assert facts["verdict"] == "PASS" and facts["reason_codes"] == []
    assert facts["descendants_checked"] and facts["local_process_exit_confirmed"]
    assert facts["cards"] == 1 and facts["card_status"] == "declined"
    assert facts["card_delivery"] == "sent" and facts["card_decision_sent"] == "decline"
    assert facts["card_available_decisions"] == ["decline"]
    assert facts["provider_available_decisions"] == ["accept", "cancel"]
    assert facts["command_view_parsed"] and facts["inner_command_is_fixed"]
    assert facts["reply_exact"] and facts["workdir_empty"] and facts["event_order_ok"]
    assert facts["tool_items"] == [{"item_type": "commandExecution", "status": "declined",
                                    "process_id_reported": False, "completed": True,
                                    "lifecycle_seen": True, "output_events": 0}]
    assert "run the fixed command" not in json.dumps(facts)  # no prompt text is written


def test_without_a_process_to_inspect_cleanup_is_not_shown(tmp_path):
    # The in-memory stub has no pid, so no descendant report: everything else clean, still not PASS.
    facts = S64.collect(web_run(tmp_path), **STUB)
    assert facts["verdict"] == "INCONCLUSIVE" and facts["reason_codes"] == ["local_cleanup_unconfirmed"]
    assert facts["descendants_checked"] is False


def test_an_expired_card_is_not_a_click(tmp_path):
    facts = S64.collect(web_run(tmp_path, click=False, ttl=0.3), **STUB)
    assert facts["verdict"] == "INCONCLUSIVE" and "card_expired_not_clicked" in facts["reason_codes"]
    assert facts["card_decision_sent"] == "decline"  # the Hub's own expiry rule still declined once


def test_a_strict_call_is_not_the_supervised_web_path(tmp_path):
    facts = S64.collect(web_run(tmp_path, mode="strict"), **STUB)
    assert facts["verdict"] == "FAIL" and "not_supervised" in facts["reason_codes"]


def test_no_tool_request_is_inconclusive(tmp_path):
    facts = S64.collect(web_run(tmp_path, scenario="happy"), **STUB)
    assert facts["verdict"] == "INCONCLUSIVE"
    assert {"no_tool_card", "reply_not_declined"} <= set(facts["reason_codes"])


def test_a_file_in_the_work_directory_is_execution_evidence(tmp_path):
    data_dir = web_run(tmp_path)
    workdir = next((data_dir / "codex-runs").glob("inv-*"))
    (workdir / "S3_EXECUTED.txt").write_text("x")
    facts = S64.collect(data_dir, **STUB)
    assert facts["verdict"] == "FAIL" and "execution_evidence" in facts["reason_codes"]


def test_a_different_binding_fails(tmp_path):
    facts = S64.collect(web_run(tmp_path), expected_inner=STUB["expected_inner"],
                        expected_binding=S64.PINNED_BINDING)
    assert facts["verdict"] == "FAIL" and "binding_mismatch" in facts["reason_codes"]


def _clean_facts():
    return {"invocations": 1, "conversations": 1, "execution_mode": "supervised", "binding_matches": True,
            "card_decision_sent": "decline", "accept_suppressed": [], "workdir_empty": True, "tool_items": [],
            "card_status": "declined", "card_delivery": "sent", "cards": 1, "card_method": S64.APPROVAL,
            "card_available_decisions": ["decline"], "command_view_parsed": True, "inner_command_is_fixed": True,
            "auto_declined": [], "unanswered": [], "tool_process_state": "none", "native_agents": 0,
            "invocation_status": "succeeded", "turn_status": "completed", "terminal": True,
            "reply_contains": True, "approval_status": "consumed", "foreign_events": 0, "late_events": 0,
            "usage_anomalies": 0, "usage_present": True, "local_process_exit_confirmed": True,
            "descendants_checked": True, "descendants_survived": False, "held_slots": 0, "event_order_ok": True}


@pytest.mark.parametrize("change, code", [
    ({"card_decision_sent": "accept"}, "accept_path_reached"),
    ({"accept_suppressed": ["tool-1"]}, "accept_path_reached"),
    ({"card_delivery": "failed"}, "decline_not_delivered"),
    ({"card_available_decisions": ["accept", "decline"]}, "card_offered_more_than_decline"),
    ({"tool_items": [{"item_type": "commandExecution", "status": "completed", "process_id_reported": True}]},
     "execution_evidence"),
    ({"invocations": 2}, "budget_exceeded"),
])
def test_judge_fails_each_broken_boundary(change, code):
    facts = {**_clean_facts(), **change}
    assert code in S64.judge(facts)[0]


@pytest.mark.parametrize("change, code", [
    ({"descendants_checked": False}, "local_cleanup_unconfirmed"),
    ({"native_agents": 1}, "native_agent"),
    ({"inner_command_is_fixed": False}, "command_not_the_fixed_one"),
    ({"auto_declined": ["item/fileChange/requestApproval"]}, "request_outside_the_card"),
])
def test_judge_marks_what_was_not_shown(change, code):
    facts = {**_clean_facts(), **change}
    fail, inconclusive = S64.judge(facts)
    assert fail == [] and code in inconclusive
    assert S64.judge(_clean_facts()) == ([], [])
