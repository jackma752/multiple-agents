"""The /chat page: served safely, calls only real routes, demo loop works.

Offline only. The browser walk-through itself is recorded in
docs/chat-frontend.md; these tests pin what can be checked without one.
"""

import re

import pytest
from starlette.routing import Match

from agenthub_adapters import appserver_stub
from agenthub_adapters.appserver_stub import DEMO_COMMAND, DEMO_INTRO, DEMO_OUTCOME, InMemoryStubFactory
from agenthub_rt.ui import CHAT_HTML
from tests.http.test_async_and_tool_approval import pending_card, settle, start, status_of
from tests.http.test_conversation_events import events, types
from tests.http.test_conversation_stream import make_client, submit  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, approve, conversation, messages


def page_source():
    with open(CHAT_HTML, "r", encoding="utf-8") as fh:
        return fh.read()


def test_chat_page_is_served_without_secrets_or_html_injection(make_client):
    client = make_client(InMemoryStubFactory("demo"))
    r = client.get("/chat")  # no token needed to load the static page
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers.get("cache-control") == "no-store"
    html = r.text
    # Text from users, models and tool requests is inserted with textContent only.
    assert "innerHTML" not in html and "outerHTML" not in html and "insertAdjacentHTML" not in html
    assert "eval(" not in html and "new Function" not in html
    # EventSource cannot send Authorization; the page must read SSE with fetch.
    assert "new EventSource" not in html and "Last-Event-ID" in html
    # The token lives in the tab session only, never in localStorage or the URL.
    # Non-secret model/display preferences may persist; credentials remain tab-only.
    import re
    assert set(re.findall(r"localStorage\.(?:getItem|setItem)\(['\"]([^'\"]+)", html)) <= {
        "agenthub.models", "agenthub.showGrandTotal"}
    assert 'sessionStorage.setItem("agenthub.token"' in html and "token=" not in html


CALL_RE = re.compile(r'api\("(GET|POST)",\s*`?"?(/api/[^`"]+)[`"]')
FETCH_RE = re.compile(r'fetch\(`(/api/[^`?]+)')


def page_calls():
    src = page_source()
    calls = {(m, p) for m, p in CALL_RE.findall(src)}
    calls |= {("GET", p) for p in FETCH_RE.findall(src)}
    return sorted(calls)


def concrete(path):
    return re.sub(r"\$\{[^}]+\}", "x", path)


def test_every_api_call_in_the_page_matches_a_route(make_client):
    client = make_client(InMemoryStubFactory("demo"))
    calls = page_calls()
    # Guard the extraction itself: the loop's essential calls must be found.
    wanted = {
        ("POST", "/api/conversations/${enc(cid)}/invocations"),
        ("POST", "/api/approvals/${enc(c.approval_id)}/approve"),
        ("POST", "/api/invocations/${enc(c.id)}/run"),
        ("GET", "/api/tool-approvals/${enc(id)}"),
        ("POST", "/api/tool-approvals/${enc(card.tool_approval_id)}/${decision}"),
        ("GET", "/api/conversations/${enc(cid)}/events/stream"),
        ("GET", "/api/conversations/${enc(cid)}/messages"),
    }
    assert wanted <= set(calls), set(calls)
    for method, path in calls:
        sample = concrete(path)
        if "${decision}" in path:
            samples = [path.replace("${decision}", d) for d in ("approve", "decline")]
            samples = [concrete(s) for s in samples]
        else:
            samples = [sample]
        for s in samples:
            scope = {"type": "http", "path": s, "method": method}
            assert any(route.matches(scope)[0] == Match.FULL for route in client.app.routes), (method, s)


def test_page_has_one_creation_path_and_view_generation_guards():
    """Pinned review fixes (the behaviour itself is checked in the browser, docs/chat-frontend.md)."""
    src = page_source()
    # One place creates conversations; the button and send() share its in-flight promise.
    assert src.count('api("POST", "/api/conversations"') == 1
    assert "function createConversation(" in src and "if (S.createPromise) await S.createPromise;" in src
    # Every async result is tied to the view generation it started in.
    assert "const gen = ++S.viewGen;" in src and "function current(gen)" in src
    for guard in ("async function loadMessages(gen", "async function fetchCard(id, gen",
                  "function apply(ev, gen", "async function resync(gen", "parseBlock(buf.slice(0, cut), gen)"):
        assert guard in src, guard
    assert "streamGen" not in src  # the old shared counter that a stale select() could reuse


def test_page_needs_an_explicit_mode_and_a_fresh_consent_per_call():
    src = page_source()
    assert '<option value="" selected>请选择</option>' in src  # no default mode
    assert "execution_mode: mode" in src and '$("mode").value = "";' in src
    assert "accept_unmanaged_native_agents: S.consent.get(c.id) === true" in src
    assert "S.consent.delete(c.id);" in src  # consent never carried over
    assert "S.consent = new Map();" in src   # nor across conversations


@pytest.fixture
def fast_demo(monkeypatch):
    monkeypatch.setattr(appserver_stub, "DEMO_DELAY", 0)


def test_demo_loop_send_approve_stream_card_decline_reply_resume(make_client, fast_demo):
    """The loop the page drives, end to end against the demo stub."""
    client = make_client(InMemoryStubFactory("demo", thread_id="thread-demo"))
    cid = conversation(client)
    first = submit(client, cid, text="列一下目录")
    assert first["status"] == "awaiting_approval"
    approve(client, first)
    assert start(client, first).status_code == 202
    card = pending_card(client, cid)
    assert card["details"]["command"] == DEMO_COMMAND
    streamed = [e["payload"]["text"] for e in events(client, cid)["events"] if e["type"] == "stream.delta"]
    assert "".join(streamed) == "".join(DEMO_INTRO)  # output arrived before the card
    r = client.post(f"/api/tool-approvals/{card['tool_approval_id']}/decline", headers=USER)
    assert r.status_code == 200
    settle(client)
    assert status_of(client, first["invocation_id"]) == "succeeded"
    reply = messages(client, cid)[-1]
    assert reply["role"] == "assistant"
    assert reply["text"] == "".join(DEMO_INTRO + DEMO_OUTCOME["decline"])
    assert reply["result"]["native_session"]["thread_id"] == "thread-demo"

    # "Refresh": a new reader replays the same ordered history from seq 0.
    replay = events(client, cid)
    names = types(replay)
    assert names.index("tool_approval.requested") < names.index("tool_approval.status") \
        < names.index("tool_approval.delivery")
    assert [e["seq"] for e in replay["events"]] == list(range(1, len(replay["events"]) + 1))
    card_at = names.index("tool_approval.requested")
    before = "".join(e["payload"]["text"] for e in replay["events"][:card_at] if e["type"] == "stream.delta")
    after = "".join(e["payload"]["text"] for e in replay["events"][card_at:] if e["type"] == "stream.delta")
    assert before == "".join(DEMO_INTRO) and after == "".join(DEMO_OUTCOME["decline"])

    second = submit(client, cid, key="two", text="继续", context_mode="native_resume")
    assert second["resume_thread_id"] == "thread-demo"
    approve(client, second)
    assert start(client, second).status_code == 202
    card2 = pending_card(client, cid)
    client.post(f"/api/tool-approvals/{card2['tool_approval_id']}/approve", headers=USER,
                json={"card_hash": card2["card_hash"]})
    settle(client)
    assert messages(client, cid)[-1]["text"] == "".join(DEMO_INTRO + DEMO_OUTCOME["accept"])
