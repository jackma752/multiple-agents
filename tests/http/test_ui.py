"""P3 console: read-model endpoints and the zero-build UI page.

The page itself cannot be clicked in this environment (no browser), so the
strongest available verification is:

* the page is served and contains every control the flow needs;
* the read-model endpoints are auth-protected and return the shape the page
  renders;
* a **simulated UI session** drives exactly the call sequence the JavaScript
  makes, re-deriving each id from the view endpoint - proving the console can
  auto-manage task_id / invocation_id / approval_id without the user copying or
  filling in placeholders.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agenthub.contracts import InvocationStatus
from agenthub_kernel.db import Database
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
AGENT = {"Authorization": "Bearer dev-agent-token"}


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{(tmp_path / 'ui.db').as_posix()}"
    app = create_app(db=Database(url))
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- #
# the page itself
# --------------------------------------------------------------------------- #


def test_ui_page_is_served(client):
    r = client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    html = r.text
    # The controls the P3 brief requires must be present.
    for needle in (
        "Agent Hub 控制台",
        "Mock 模式",          # clearly states no real agent
        'id="chat"',           # unified chat
        'id="approvals"',      # approval cards
        'id="tree"',           # task status
        'id="quota"',          # quota display
        'id="stop"',           # stop button
        'id="pause"',
        'id="apply-policy"',
        'id="token"',
    ):
        assert needle in html, f"missing UI element: {needle}"


def test_ui_page_lets_the_user_choose_the_agent(client):
    """The console must not hardcode an agent_id.

    A hardcoded id is unrunnable as soon as the adapter serves a different one:
    the call is created, approved, and then refused at launch. The field is filled
    from `/api/adapter`'s `agent_ids` instead.
    """
    html = client.get("/ui").text
    assert 'agent_id: "mock"' not in html, "the console still hardcodes agent_id"
    assert 'id="agent-id"' in html, "no agent field in the composer"
    assert 'id="agent-options"' in html, "no datalist for the served agent ids"
    assert "agent_ids" in html, "the page never reads the adapter's agent ids"
    assert "adapter_warning" in html, "the page ignores the creation-time warning"


def test_ui_page_has_no_placeholder_ids_to_fill_in(client):
    """No input asks the user to type a task/invocation/approval id."""
    html = client.get("/ui").text
    lowered = html.lower()
    for bad in ("输入 task_id", "填写 approval_id", "placeholder=\"task_id\"",
                "placeholder=\"invocation_id\"", "placeholder=\"approval_id\""):
        assert bad not in lowered, f"UI asks the user to supply an id: {bad}"


def test_ui_page_loads_without_auth_but_data_requires_it(client):
    """The static page is public; every API call it makes needs the token."""
    assert client.get("/ui").status_code == 200
    assert client.get("/api/tasks").status_code == 401


# --------------------------------------------------------------------------- #
# read model
# --------------------------------------------------------------------------- #


def test_read_model_requires_auth_but_not_the_user_role(client):
    """Read access needs an authenticated identity; approval needs the user role.

    Deliberate split: agent/worker identities are refused *approval* (see
    test_auth.py) but may read state, since a worker has to inspect the tree.
    Anything unauthenticated is refused outright.
    """
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks", headers=USER).status_code == 200
    assert client.get("/api/tasks", headers=AGENT).status_code == 200  # read is allowed

    created = client.post(
        "/api/invocations",
        json={"task_id": "-", "agent_id": "mock", "task_input": "x"},
    ).json()
    root = created["task_id"]
    assert client.get(f"/api/tasks/{root}/view").status_code == 401
    assert client.get(f"/api/tasks/{root}/view", headers=AGENT).status_code == 200

    # ...while the mutating endpoints stay user-only for that same identity.
    appr = created["approval_id"]
    assert client.post(f"/api/approvals/{appr}/approve", headers=AGENT).status_code == 403
    assert client.post(f"/api/tasks/{root}/stop", headers=AGENT).status_code == 403
    assert client.patch(
        f"/api/tasks/{root}/policy",
        json={"max_subagents_total": 1, "max_subagents_concurrent": 1},
        headers=AGENT,
    ).status_code == 403


def test_view_shape_matches_what_the_console_renders(client):
    created = client.post(
        "/api/invocations",
        json={"task_id": "-", "agent_id": "mock", "task_input": "build a login page"},
    ).json()
    root = created["task_id"]

    v = client.get(f"/api/tasks/{root}/view", headers=USER).json()

    for key in ("root", "tasks", "invocations", "approvals", "quota",
                "policy", "policy_versions", "stopped", "paused", "events"):
        assert key in v, f"view missing {key}"

    assert v["root"]["id"] == root
    assert v["root"]["goal"] == "build a login page"
    assert v["stopped"] is False and v["paused"] is False
    assert v["tasks"][0]["depth"] == 0

    # The freshly created call is awaiting approval, and the approval id is in
    # the view so the page can act on it.
    inv = v["invocations"][0]
    assert inv["id"] == created["invocation_id"]
    assert inv["status"] == InvocationStatus.AWAITING_APPROVAL.value
    assert v["approvals"][0]["invocation_id"] == inv["id"]
    assert v["approvals"][0]["status"] == "pending"

    # Quota block has the fields the gauges read.
    for k in ("launched_total", "concurrent", "total_invocations", "open_reservations"):
        assert k in v["quota"]


def test_tasks_list_summarises_for_the_sidebar(client):
    client.post("/api/invocations", json={"task_id": "-", "agent_id": "mock", "task_input": "one"})
    client.post("/api/invocations", json={"task_id": "-", "agent_id": "mock", "task_input": "two"})

    rows = client.get("/api/tasks", headers=USER).json()
    assert len(rows) == 2
    row = rows[0]
    for k in ("id", "goal", "invocations", "pending_approvals", "running", "quota", "stopped"):
        assert k in row
    assert row["pending_approvals"] == 1


# --------------------------------------------------------------------------- #
# simulated UI session: the exact sequence the JavaScript performs
# --------------------------------------------------------------------------- #


def test_simulated_ui_session_end_to_end(client):
    """Reproduce the console's call sequence, re-deriving ids only from the view."""
    token = USER

    # --- connect ------------------------------------------------------
    assert client.get("/api/dev/mock-activity", headers=token).status_code == 200

    # --- new conversation + send a message -----------------------------
    conv = client.post("/api/conversations", json={}).json()["conversation_id"]
    client.post(f"/api/conversations/{conv}/messages", json={"text": "build a login page"})

    created = client.post(
        "/api/invocations",
        json={"task_id": "-", "agent_id": "mock", "task_input": "build a login page"},
    ).json()
    # The page keeps these; the response must carry all three ids.
    assert set(("task_id", "invocation_id", "approval_id")) <= set(created)

    root = created["task_id"]

    # --- the page then renders purely from the view --------------------
    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    pending = [a for a in v["approvals"] if a["status"] == "pending"]
    assert len(pending) == 1
    appr_id = pending[0]["id"]  # taken from the VIEW, not from the create response

    # --- "推进调度" before approving: nothing may start -----------------
    out = client.post("/api/worker/drain", headers=token).json()
    assert [o for o in out if o["ok"]] == []
    assert client.get("/api/dev/mock-activity", headers=token).json()["start_count"] == 0

    # --- approve from the card -----------------------------------------
    assert client.post(f"/api/approvals/{appr_id}/approve", headers=token).status_code == 200
    client.post("/api/worker/drain", headers=token)
    assert client.get("/api/dev/mock-activity", headers=token).json()["start_count"] == 1

    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    assert v["invocations"][0]["status"] == InvocationStatus.RUNNING.value
    assert v["quota"]["concurrent"] == 0  # main call is depth 0; not counted as a sub-agent
    assert v["quota"]["total_invocations"] == 1

    # --- delegate two sub-agents (the UI would call the same endpoint) --
    subs = [
        client.post(
            f"/api/tasks/{root}/delegations",
            json={"agent_id": "mock", "goal": g, "parent_task_id": root},
        ).json()
        for g in ("write the HTML", "write the CSS")
    ]
    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    assert len(v["tasks"]) == 3
    assert len([a for a in v["approvals"] if a["status"] == "pending"]) == 2

    for s in subs:
        client.post(f"/api/approvals/{s['approval_id']}/approve", headers=token)
    client.post("/api/worker/drain", headers=token)
    assert client.get("/api/dev/mock-activity", headers=token).json()["start_count"] == 3

    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    assert v["quota"]["launched_total"] == 2
    assert v["quota"]["concurrent"] == 2

    # --- quota change through the panel --------------------------------
    r = client.patch(
        f"/api/tasks/{root}/policy",
        json={"max_subagents_total": 2, "max_subagents_concurrent": 1, "max_depth": 1,
              "max_agent_invocations": 10},
        headers=token,
    )
    assert r.status_code == 200
    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    assert v["root"]["policy_version"] == 2
    assert v["policy"]["max_subagents_concurrent"] == 1
    # Lowering the cap must not reset what was already used.
    assert v["quota"]["launched_total"] == 2

    # --- stop button ----------------------------------------------------
    r = client.post(f"/api/tasks/{root}/stop", headers=token).json()
    assert r["stopped"] is True
    v = client.get(f"/api/tasks/{root}/view", headers=token).json()
    assert v["stopped"] is True
    assert v["quota"]["concurrent"] == 0
    assert v["quota"]["launched_total"] == 2, "stop must not return cumulative usage"

    # --- nothing new may start after the stop ---------------------------
    client.post(f"/api/worker/drain", headers=token)
    assert client.get("/api/dev/mock-activity", headers=token).json()["start_count"] == 3


def test_view_reports_paused_state_for_the_header(client):
    created = client.post(
        "/api/invocations", json={"task_id": "-", "agent_id": "mock", "task_input": "x"}
    ).json()
    root = created["task_id"]

    client.post(f"/api/tasks/{root}/pause", headers=USER)
    assert client.get(f"/api/tasks/{root}/view", headers=USER).json()["paused"] is True
    client.post(f"/api/tasks/{root}/resume", headers=USER)
    assert client.get(f"/api/tasks/{root}/view", headers=USER).json()["paused"] is False


def test_view_404_for_unknown_task(client):
    assert client.get("/api/tasks/task-nope/view", headers=USER).status_code == 404


# --------------------------------------------------------------------------- #
# what the console must show for a REAL adapter (Codex)
# --------------------------------------------------------------------------- #


def test_ui_results_panel_renders_the_codex_reply_usage_and_state(client):
    """Source-level check that the panel binds every field the P3 brief requires.

    The page cannot be clicked here (no browser), so this pins the rendering
    contract: the results card must show the adapter status, the kernel status,
    thread_id, the final reply, usage, the event census, the exit code and the
    workdir delta.
    """
    html = client.get("/ui").text
    for needle in (
        "最终回复",              # final reply, verbatim
        "usage",
        "thread_id",
        "退出码",                 # exit code (real process evidence)
        "事件",
        "工作目录",
        "内核：",                 # kernel status next to the adapter status
        'id="results"',
    ):
        assert needle in html, f"results panel is missing: {needle}"


def test_ui_states_cancellation_is_unsupported(client):
    """Codex cannot cancel: the page must SAY so rather than imply a stop."""
    html = client.get("/ui").text
    assert "取消：<b>不支持</b>" in html
    assert "caps.cancel === true" in html, "cancel support must be read from the adapter"
    assert "保留配额" in html, "the page must say the slot is kept when a stop is unconfirmed"


def test_ui_stop_message_separates_confirmed_from_unconfirmed(client):
    """A stop is only 'stopped' where the adapter confirmed it."""
    html = client.get("/ui").text
    assert "r.confirmed" in html and "r.unconfirmed" in html
    assert "未能确认停止" in html
    assert "已停止：取消" not in html, "the old unconditional 'stopped' claim is back"


def test_stop_api_reports_confirmed_and_unconfirmed(client):
    """The endpoint behind that message must return both counts."""
    created = client.post(
        "/api/invocations", json={"task_id": "-", "agent_id": "mock", "task_input": "stop me"}
    ).json()
    root = created["task_id"]
    body = client.post(f"/api/tasks/{root}/stop", headers=USER).json()
    assert body["stopped"] is True
    assert body["confirmed"] == 0 and body["unconfirmed"] == 0  # nothing in flight
    assert "outcomes" in body


def test_simulated_console_session_with_the_real_codex_adapter(tmp_path):
    """Drive the console's exact call sequence against the Codex adapter.

    The stub-driven runs prove the HTTP path; this one pins what the RESULTS panel
    would render for Codex: the adapter status, the kernel status, the parsed
    thread_id, the final reply verbatim, real usage, the event census and the
    process exit code - plus the adapter's declared cancel=false, which the page
    turns into "取消：不支持".

    No real process runs here: the runner is the fake that replays the verified
    capture shape.
    """
    from agenthub_adapters.codex import CodexAdapter, FakeCodexRunner, success_script
    from agenthub_adapters.store import ExecutionStore

    adapter = CodexAdapter(
        codex_path=__file__,
        codex_home=str(tmp_path / "home"),
        workdir_root=str(tmp_path / "runs"),
        store=ExecutionStore(path=str(tmp_path / "codex.db"), table="codex_executions"),
        runner=FakeCodexRunner(success_script()),
    )
    app = create_app(
        db=Database(f"sqlite:///{(tmp_path / 'console-codex.db').as_posix()}"), adapter=adapter
    )

    with TestClient(app) as c:
        # 1) the header: real adapter, honest capability list
        info = c.get("/api/adapter", headers=USER).json()
        assert info["kind"] == "codex"
        assert info["capabilities"]["cancel"] is False, "Codex cannot cancel"
        assert info["capabilities"]["intercept_tool"] is False

        # 2) create the call the composer would create
        created = c.post(
            "/api/invocations",
            json={"task_id": "-", "agent_id": "codex", "task_input": "Reply with exactly: OK."},
            headers=USER,
        ).json()
        root, inv_id, appr_id = (
            created["task_id"],
            created["invocation_id"],
            created["approval_id"],
        )

        # 3) "推进调度" without approval: nothing may launch
        c.post("/api/worker/drain", headers=USER)
        assert c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()[
            "available"
        ] is False

        # 4) approve, then drain
        c.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        c.post("/api/worker/drain", headers=USER)

        # 5) exactly what renderTree()/renderResults() read
        view = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        served = {t["id"]: t for t in view["tasks"]}[root]
        assert served["status"] == "succeeded", "the task must not read 'draft'"
        assert served["stored_status"] == "succeeded"
        assert view["invocations"][0]["status"] == "succeeded"

        res = c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        assert res["available"] is True
        summary = res["summary"]
        assert summary["thread_id"] == "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce"
        assert summary["final_message"] == "OK", "the panel shows the reply verbatim"
        assert summary["usage"]["input_tokens"] == 14481
        assert summary["usage"]["output_tokens"] == 5
        assert summary["event_types"]["turn.completed"] == 1
        assert res["evidence"]["exit_code"] == 0
        assert res["evidence"]["event_count"] >= 4
        assert res["evidence"]["timed_out"] is False

