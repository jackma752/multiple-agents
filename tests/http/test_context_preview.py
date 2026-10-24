"""T-1d: the pre-send context preview, bound into the approval (offline stubs only).

The preview's parts must add up to exactly what the adapter receives, the
provider side is never folded into the estimate, and a preview changed after
approval voids the approval (adapter start count 0).
"""

import pytest

from agenthub.budget import estimate_visible_tokens
from agenthub.context import part_texts
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.adapters import describe
from agenthub_kernel.db import InvocationRow
from tests.http import test_multi_agent as multi
from tests.http.test_conversation_stream import (  # noqa: F401  (make_client is a fixture)
    make_client, submit, turn_and_run,
)
from tests.http.test_conversations import USER, approve, conversation, run
from tests.http.test_multi_agent import hub  # noqa: F401  (fixture)


def stored(client, turn):
    with client.app.state.db.session() as s:
        return dict(s.get(InvocationRow, turn["invocation_id"]).snapshot)


def turn_start_text(factory, index):
    start = next(m for m in factory.servers[index].received if m.get("method") == "turn/start")
    return start["params"]["input"][0]["text"]


def handed_over(client):
    """A conversation whose Codex reply "OK" is handed to Claude Code with a short note."""
    cid = multi.conversation(client)
    multi.approve_and_run(client, multi.submit(client, cid, "codex", "say OK", "k1").json())
    source = multi.replies(client, cid)[0]
    return cid, source


def test_the_parts_add_up_to_what_codex_receives(make_client):
    factory = InMemoryStubFactory("happy")
    client = make_client(factory)
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="请看 this file")
    p, snap = turn["context_preview"], stored(client, turn)
    assert p == snap["context_preview"]  # the card shows what the approval binds
    assert [x["kind"] for x in p["parts"]] == ["user_input"]
    assert "".join(part_texts(snap["task_input"], p)) == snap["task_input"]
    assert p["chars"] == len(snap["task_input"]) and p["estimate"] == p["parts"][0]["estimate"]
    assert p["adapter_text"] == {"chars": 0}
    hidden = p["provider_hidden"]
    assert hidden["status"] == "unknown" and hidden["includes"] == ["instructions_and_tools"]
    assert [o["input_total"] for o in hidden["observed"]] == [15183, 15753]
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert turn_start_text(factory, 0) == "".join(part_texts(snap["task_input"], p))


def test_a_handoff_is_split_into_frame_and_content_and_claude_code_receives_the_sum(hub):
    client, _router, prompts = hub
    cid, source = handed_over(client)
    turn = multi.submit(client, cid, "claude-code", "Review.", "k2",
                        handoff_message_ids=[source["message_id"]]).json()
    p = turn["context_preview"]
    assert [x["kind"] for x in p["parts"]] == [
        "handoff_frame", "handoff_content", "handoff_frame", "separator", "user_input"]  # context first
    texts = part_texts(turn["sent_text"], p)
    assert texts[-2:] == ["\n\n", "Review."] and texts[1] == "OK"
    assert all(x["message_id"] == source["message_id"] and x["agent_id"] == "codex" for x in p["parts"][:3])
    assert p["parts"][1]["chars"] == turn["handoff"][0]["chars"]
    assert p["parts"][1]["sha256"] == turn["handoff"][0]["sha256"]
    assert [o["input_total"] for o in p["provider_hidden"]["observed"]] == [1752, 1798]
    assert multi.approve_and_run(client, turn).json()["status"] == "succeeded"
    assert prompts == ["".join(texts)]  # exactly the parts, nothing more


def test_native_resume_marks_the_thread_history_unknown(make_client):
    client = make_client(InMemoryStubFactory("stream", thread_id="thread-abc"))
    cid = conversation(client)
    first, _ = turn_and_run(client, cid, "one", text="follow up")
    second = submit(client, cid, key="two", text="follow up", context_mode="native_resume")
    a, b = first["context_preview"], second["context_preview"]
    assert a["provider_hidden"]["includes"] == ["instructions_and_tools"]
    assert b["provider_hidden"]["includes"] == ["instructions_and_tools", "thread_history"]
    assert b["parts"] == a["parts"] and b["estimate"] == a["estimate"]  # history never folded in


@pytest.mark.parametrize("tamper, code", [
    # No longer describes the text sent.
    (lambda p: {**p, "estimate": p["estimate"] + 1}, "snapshot_invalid"),
    (lambda p: {**p, "parts": [{**p["parts"][0], "sha256": "0" * 64}]}, "snapshot_invalid"),
    # Still a valid preview, but not the one that was approved.
    (lambda p: {**p, "provider_hidden": {"status": "unknown", "includes": []}}, "snapshot_changed"),
    (lambda p: {k: v for k, v in p.items() if k != "adapter_text"}, "snapshot_changed"),
])
def test_a_preview_changed_after_approval_voids_it(make_client, tamper, code):
    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="hello world")
    approve(client, turn)
    with client.app.state.db.session() as s:
        inv = s.get(InvocationRow, turn["invocation_id"])
        inv.snapshot = {**inv.snapshot, "context_preview": tamper(inv.snapshot["context_preview"])}
        s.commit()
    adapter = client.app.state.worker.adapter
    starts = adapter.start_count
    r = run(client, turn)
    assert r.status_code == 409 and r.json()["detail"]["code"] == code, r.text
    assert adapter.start_count == starts
    with client.app.state.db.session() as s:
        assert s.get(InvocationRow, turn["invocation_id"]).status == "invalid"


def test_the_budget_estimate_is_the_previews_and_the_card_agrees_with_admission(hub):
    client, _router, prompts = hub
    cid, source = handed_over(client)
    turn = multi.submit(client, cid, "claude-code", "Review.", "k2",
                        handoff_message_ids=[source["message_id"]]).json()
    p = turn["context_preview"]
    assert p["estimate"] > estimate_visible_tokens(turn["sent_text"])  # rounded up part by part
    assert turn["budget"]["estimate_visible"] == p["estimate"]
    root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
    r = client.patch(f"/api/tasks/{root}/policy", headers=USER,
                     json={"token_budget_per_call": p["estimate"] - 1, "token_budget_mode": "strict"})
    assert r.status_code == 200 and r.json()["revoked_approvals"] == 1, r.text
    again = multi.submit(client, cid, "claude-code", "Review.", "k3",
                         handoff_message_ids=[source["message_id"]]).json()
    assert again["context_preview"]["estimate"] == p["estimate"]
    assert client.post(f"/api/approvals/{again['approval_id']}/approve", headers=USER).status_code == 200
    msgs = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
    card = next(m["invocation"]["budget"] for m in msgs
                if m["invocation"] and m["invocation"]["invocation_id"] == again["invocation_id"])
    assert card["preview"]["code"] == "estimate_exceeds_per_call_budget"
    r = run(client, again)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "estimate_exceeds_per_call_budget"
    assert prompts == []  # Claude Code was never launched


def test_a_replayed_request_keeps_its_preview(make_client):
    client = make_client(InMemoryStubFactory("happy"))
    cid = conversation(client)
    a = submit(client, cid, key="same", text="x")
    b = submit(client, cid, key="same", text="x")
    assert b["replayed"] and b["context_preview"] == a["context_preview"]
    assert b["payload_hash"] == a["payload_hash"]


def test_describe_shows_each_agents_context_profile(hub):
    client, router, _prompts = hub
    agents = describe(router)["agents"]
    assert agents["codex"]["context_profile"]["adds_prompt_text"] is False
    assert [o["input_total"] for o in agents["claude-code"]["context_profile"]["observed"]] == [1752, 1798]
