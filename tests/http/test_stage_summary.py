"""T-2: stage summaries, compress and continue, summary drafting (offline stubs only).

A summary goes out instead of the history: its text and its file snippets are
parts of the approved preview, and the earlier messages are not sent. Compress
and continue opens a linked conversation and leaves the source untouched.
Drafting is an ordinary call carrying recent history, so it waits for approval.
"""

from sqlalchemy import func, select

from agenthub.context import part_texts
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.db import ChatMessageRow
from tests.http import test_multi_agent as multi
from tests.http.test_budget_admission import (
    S3_TOKENS, counters, item, ledger_matches_counters, refused, root_of, set_budget, spend,
)
from tests.http.test_conversation_stream import make_client, submit, turn_and_run  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, approve, conversation, run
from tests.http.test_file_refs import files, hub, invocations, sent_to_codex  # noqa: F401  (fixtures)
from tests.http.test_multi_agent import hub as two_agents  # noqa: F401  (fixture)

SUMMARY = {"goal": "Fix the parser", "constraints": "Keep approvals", "done": "Wrote tests",
           "open_issues": "Line 12 crashes", "next_steps": "Patch and rerun",
           "file_refs": ["src/app.py:L10-12"]}
EARLIER = "EARLIER-CONTEXT that must not be resent"


def save(client, cid, **overrides):
    r = client.post(f"/api/conversations/{cid}/summaries", headers=USER, json={**SUMMARY, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def messages_of(client, cid):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(ChatMessageRow).where(ChatMessageRow.conversation_id == cid))


def test_versions_are_kept_and_listed_newest_first(hub):
    client, _ = hub
    cid = conversation(client)
    first, second = save(client, cid), save(client, cid, goal="Fix the parser, take two")
    assert (first["version"], second["version"]) == (1, 2) and first["source"] == "manual"
    listed = client.get(f"/api/conversations/{cid}/summaries", headers=USER).json()
    assert [s["version"] for s in listed["summaries"]] == [2, 1] and "Goal:" in listed["instruction"]
    r = client.post(f"/api/conversations/{cid}/summaries", headers=USER, json={**SUMMARY, "goal": " "})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_summary"


def test_a_summary_goes_out_instead_of_the_history(hub):
    client, factory = hub
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=EARLIER)
    summary = save(client, cid)
    turn = submit(client, cid, key="b", text="Continue.", summary_id=summary["summary_id"])
    p = turn["context_preview"]
    kinds = [x["kind"] for x in p["parts"]]
    assert kinds[:5] == ["user_input", "separator", "summary_frame", "summary_content", "summary_frame"]
    assert "file_snippet" in kinds  # the summary's file reference, read now
    assert all(x["summary_id"] == summary["summary_id"] and x["summary_version"] == 1
               for x in p["parts"] if x["kind"].startswith("summary"))
    texts = part_texts(turn["sent_text"], p)
    assert "Line 12 crashes" in texts[3] and "line 10\nline 11\nline 12" in turn["sent_text"]
    assert EARLIER not in turn["sent_text"]
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 1) == turn["sent_text"]


def test_a_summary_of_another_conversation_is_not_found(hub):
    client, _ = hub
    mine, other = conversation(client), conversation(client)
    summary = save(client, other)
    before = invocations(client)
    r = client.post(f"/api/conversations/{mine}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "k", "summary_id": summary["summary_id"]})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "summary_not_found"
    assert invocations(client) == before


def test_compress_and_continue_opens_a_linked_conversation_and_keeps_the_source(hub):
    client, factory = hub
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=EARLIER)
    summary = save(client, cid)
    kept = messages_of(client, cid)
    r = client.post(f"/api/conversations/{cid}/compress", headers=USER, json={"summary_id": summary["summary_id"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["parent_conversation_id"] == cid and new["continued_from_summary_id"] == summary["summary_id"]
    assert messages_of(client, cid) == kept and messages_of(client, new["conversation_id"]) == 0
    listed = client.get(f"/api/conversations/{new['conversation_id']}/summaries", headers=USER).json()
    assert [s["summary_id"] for s in listed["summaries"]] == [summary["summary_id"]]  # the source's
    turn = submit(client, new["conversation_id"], key="n1", text="Go on.", summary_id=summary["summary_id"])
    assert EARLIER not in turn["sent_text"] and "Patch and rerun" in turn["sent_text"]
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 1) == turn["sent_text"]
    conversations = client.get("/api/conversations", headers=USER).json()["conversations"]
    assert next(c for c in conversations if c["conversation_id"] == new["conversation_id"])["parent_conversation_id"] == cid
    # An unrelated conversation still cannot use it.
    stranger = conversation(client)
    r = client.post(f"/api/conversations/{stranger}/compress", headers=USER, json={"summary_id": summary["summary_id"]})
    assert r.status_code == 404


def test_drafting_is_an_ordinary_call_with_history_and_its_reply_reads_back(hub):
    client, _ = hub
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=EARLIER)
    instruction = client.get(f"/api/conversations/{cid}/summaries", headers=USER).json()["instruction"]
    starts = client.app.state.worker.adapter.start_count
    draft_call = submit(client, cid, key="d", text=instruction, include_history=True)
    p = draft_call["context_preview"]
    assert [x["role"] for x in p["parts"] if x["kind"] == "history_content"] == ["user", "assistant"]
    assert EARLIER in draft_call["sent_text"]  # the history is what the approval shows
    assert draft_call["status"] == "awaiting_approval" and client.app.state.worker.adapter.start_count == starts
    # The page reads a reply back into the form; saving records where it came from.
    with client.app.state.db.session() as s:
        reply = ChatMessageRow(conversation_id=cid, role="assistant",
                               text="Goal:\nFix it\n\nNext steps:\nRerun\n\nFiles:\n- src/app.py:L1-2")
        s.add(reply)
        s.commit()
        reply_id = reply.id
    draft = client.get(f"/api/conversations/{cid}/summary-draft", headers=USER, params={"message_id": reply_id}).json()
    assert (draft["goal"], draft["next_steps"], draft["file_refs"]) == ("Fix it", "Rerun", ["src/app.py:L1-2"])
    saved = save(client, cid, **{k: draft[k] for k in SUMMARY}, from_message_id=reply_id)
    assert saved["source"] == f"draft:{reply_id}" and saved["goal"] == "Fix it"


def compress_to(client, cid, summary):
    r = client.post(f"/api/conversations/{cid}/compress", headers=USER, json={"summary_id": summary["summary_id"]})
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"]


def test_a_budget_refusal_survives_compress_and_continue(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=EARLIER)  # S3_TOKENS spent
    root = root_of(client, cid)
    set_budget(client, root, total=40000, per_call=20000)
    refused(client, cid, "b", "token_budget_exceeded", previewed=True)  # 30,618 + 20,000 > 40,000
    new = compress_to(client, cid, save(client, cid, file_refs=[]))
    # Same task tree: the policy, the spend and the refusal carry over; nothing resets.
    assert root_of(client, new) == root
    assert spend(client, new) == (S3_TOKENS, 0, 0, 0)
    refused(client, new, "c", "token_budget_exceeded", previewed=True)
    # A new conversation and new provider threads: nothing of the source can be resumed.
    r = client.post(f"/api/conversations/{new}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "r", "context_mode": "native_resume"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_native_session"


def test_calls_after_compress_go_to_the_same_task_ledger(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "a", text=EARLIER)
    root = root_of(client, cid)
    set_budget(client, root, total=200000, per_call=60000)
    summary = save(client, cid, file_refs=[])
    new = compress_to(client, cid, summary)
    turn = submit(client, new, key="n", text="Go on.", summary_id=summary["summary_id"])
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert item(client, turn["invocation_id"])["reservation"] == 60000  # admitted under the source's policy
    usage = client.get(f"/api/conversations/{new}/usage", headers=USER).json()
    assert usage["total"]["calls"] == 1  # this conversation's own call
    assert usage["root_task"]["root_task_id"] == root and usage["root_task"]["total"]["calls"] == 2
    assert counters(client, cid) == counters(client, new) == (2 * S3_TOKENS, 0, 0, 0)
    ledger_matches_counters(client, cid)
    # A policy change of the tree reaches the continued conversation too.
    set_budget(client, root, total=300000, per_call=60000)
    events = client.get(f"/api/conversations/{new}/events", headers=USER).json()["events"]
    assert any(e["type"] == "task.policy_updated" for e in events)


def test_another_agent_gets_the_summary_not_the_history(two_agents):
    client, _router, prompts = two_agents
    cid = multi.conversation(client)
    multi.approve_and_run(client, multi.submit(client, cid, "codex", EARLIER, "k1").json())
    summary = save(client, cid, file_refs=[])
    turn = multi.submit(client, cid, "claude-code", "Take over.", "k2", summary_id=summary["summary_id"]).json()
    assert multi.approve_and_run(client, turn).json()["status"] == "succeeded"
    assert prompts == [turn["sent_text"]]
    assert "Line 12 crashes" in prompts[0] and EARLIER not in prompts[0]
