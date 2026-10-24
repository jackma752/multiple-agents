"""T-3: context-first composition, output preference, cache hit rate (offline stubs only)."""

from agenthub.output import OUTPUT_HINTS
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from tests.http.test_conversation_stream import make_client, submit, turn_and_run  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, approve, conversation, run
from tests.http.test_file_refs import files, hub, invocations, sent_to_codex  # noqa: F401  (fixtures)
from tests.http.test_stage_summary import save


def test_stable_context_goes_first_and_the_message_last(hub):
    client, factory = hub
    cid = conversation(client)
    summary = save(client, cid, file_refs=["src/app.py:L1-5"])
    a = submit(client, cid, key="a", text="First question?", summary_id=summary["summary_id"])
    b = submit(client, cid, key="b", text="A different, longer second question?",
               summary_id=summary["summary_id"])
    kinds = [x["kind"] for x in a["context_preview"]["parts"]]
    assert kinds[0] == "summary_frame" and kinds[-1] == "user_input"
    assert a["sent_text"].endswith("\n\nFirst question?")
    # Same summary and files: the two calls share their whole lead, byte for byte.
    lead = len(a["sent_text"]) - len("First question?")
    assert a["sent_text"][:lead] == b["sent_text"][:lead]
    approve(client, a)
    assert run(client, a).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 0) == a["sent_text"]


def test_an_output_preference_is_a_visible_approved_hint(hub):
    client, factory = hub
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="Fix it.", output_style="concise", file_refs=["src/app.py:L1-2"])
    kinds = [x["kind"] for x in turn["context_preview"]["parts"]]
    assert kinds[-3:] == ["output_hint", "separator", "user_input"]  # after the files, before the message
    assert turn["sent_text"].endswith(OUTPUT_HINTS["concise"] + "\n\nFix it.")
    plain = submit(client, cid, key="b", text="Fix it.", file_refs=["src/app.py:L1-2"])
    assert "output_hint" not in [x["kind"] for x in plain["context_preview"]["parts"]]
    assert plain["payload_hash"] != turn["payload_hash"]
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 0) == turn["sent_text"]
    before = invocations(client)
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "x", "agent_id": "codex", "client_request_id": "c", "output_style": "terse"})
    assert r.status_code == 422 and invocations(client) == before


def test_the_cache_hit_rate_is_reported_apart_from_totals(make_client):
    client = make_client(InMemoryStubFactory("usage-multi-request"))
    cid = conversation(client)
    turn_and_run(client, cid, "a", text="hello")
    usage = client.get(f"/api/conversations/{cid}/usage", headers=USER).json()
    cache = usage["total"]["cache"]
    assert cache == {"calls": 1, "cache_read": 24832, "input_total": 30539, "rate": round(24832 / 30539, 4)}
    assert usage["total"]["known"]["input_total"] == 30539  # the total itself is unchanged
    empty = client.get(f"/api/conversations/{conversation(client)}/usage", headers=USER).json()
    assert empty["total"]["cache"]["rate"] is None
