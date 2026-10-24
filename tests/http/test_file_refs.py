"""T-1e/T-1f: file references, from choosing to sending (offline stubs only).

Choose files or line ranges -> preview what will be sent -> approve -> the agent
receives exactly the approved text. The boundary (files root, exclusions,
resolved real path) refuses before anything is created.
"""

import os

import pytest
from sqlalchemy import func, select

from agenthub.context import part_texts
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_kernel.db import InvocationRow
from tests.http.test_conversation_stream import make_client, submit  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, approve, conversation, run

APP = "".join(f"line {i}\n" for i in range(1, 41))  # 40 lines


@pytest.fixture
def files(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(APP, encoding="utf-8")
    (root / "文档").mkdir()
    (root / "文档" / "说明 一.md").write_bytes("第一行\r\n第二行\r\n第三行\r\n".encode("utf-8"))  # CRLF
    (root / ".env").write_text("TOKEN=not-for-agents\n", encoding="utf-8")
    (root / "var").mkdir()
    (root / "var" / "run.log").write_text("runtime\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    (tmp_path / "outside.txt").write_text("outside\n", encoding="utf-8")
    return root


@pytest.fixture
def hub(make_client, files):
    factory = InMemoryStubFactory("happy")
    client = make_client(factory)
    client.app.state.files_root = str(files)
    return client, factory


def sent_to_codex(factory, index):
    start = next(m for m in factory.servers[index].received if m.get("method") == "turn/start")
    return start["params"]["input"][0]["text"]


def preview(client, refs, **extra):
    return client.post("/api/context-preview", headers=USER,
                       json={"agent_id": "codex", "text": "看一下", "file_refs": refs, **extra})


def invocations(client):
    with client.app.state.db.session() as s:
        return s.scalar(select(func.count()).select_from(InvocationRow))


def test_choose_preview_approve_send(hub):
    client, factory = hub
    refs = ["src/app.py:L10-12", '"文档/说明 一.md"']
    shown = preview(client, refs)
    assert shown.status_code == 200, shown.text
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="看一下", file_refs=refs)
    p = turn["context_preview"]
    assert p == shown.json()["context_preview"]  # the composer showed what the approval binds
    snippets = [x for x in p["parts"] if x["kind"] == "file_snippet"]
    assert [(x["path"], x["start_line"], x["end_line"]) for x in snippets] == [
        ("src/app.py", 10, 12), ("文档/说明 一.md", 1, 3)]
    texts = part_texts(turn["sent_text"], p)
    assert "line 10\nline 11\nline 12" in texts and "第一行\n第二行\n第三行" in texts
    assert "line 13" not in turn["sent_text"]  # a range never brings the whole file
    assert "--- File: src/app.py (lines 10-12 of 40) ---" in turn["sent_text"]
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 0) == turn["sent_text"] == "".join(texts)


def test_duplicate_and_overlapping_ranges_are_merged(hub, files):
    client, _ = hub
    refs = ["src/app.py:L5-8", "src/app.py:L7-10", "src/app.py:5-8", str(files / "src" / "app.py") + ":L20"]
    p = preview(client, refs).json()["context_preview"]
    assert [(x["start_line"], x["end_line"]) for x in p["parts"] if x["kind"] == "file_snippet"] == [(5, 10), (20, 20)]
    whole = preview(client, ["src/app.py:L5-8", "@src/app.py"]).json()["context_preview"]
    assert [(x["start_line"], x["end_line"]) for x in whole["parts"] if x["kind"] == "file_snippet"] == [(1, 40)]


def test_a_file_edited_after_approval_changes_nothing_that_is_sent(hub, files):
    client, factory = hub
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="看一下", file_refs=["src/app.py:L1-2"])
    approve(client, turn)
    (files / "src" / "app.py").write_text("TAMPERED\n" * 40, encoding="utf-8")
    assert run(client, turn).json()["status"] == "succeeded"
    assert sent_to_codex(factory, 0) == turn["sent_text"] and "TAMPERED" not in turn["sent_text"]


@pytest.mark.parametrize("spec, code", [
    ("../outside.txt", "file_outside_root"),
    ("src/../../outside.txt", "file_outside_root"),
    (".env", "file_excluded"),
    ("var/run.log", "file_excluded"),
    ("src/missing.py", "file_not_found"),
    ("blob.bin", "file_not_text"),
    ("src/app.py:L39-41", "range_out_of_bounds"),
    ("src/app.py:L9-3", "invalid_turn"),
])
def test_refused_references_create_nothing(hub, files, spec, code):
    client, _ = hub
    cid = conversation(client)
    before = invocations(client)
    r = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
        "text": "看一下", "agent_id": "codex", "client_request_id": "k", "file_refs": [spec]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == code, r.text
    assert invocations(client) == before
    if code != "invalid_turn":
        assert preview(client, [spec]).json()["detail"]["code"] == code


def test_an_absolute_path_outside_the_root_is_refused(hub, files):
    client, _ = hub
    r = preview(client, [str(files.parent / "outside.txt")])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "file_outside_root"


def test_a_junction_out_of_the_root_is_refused(hub, files, tmp_path):
    _winapi = pytest.importorskip("_winapi")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("outside\n", encoding="utf-8")
    try:
        _winapi.CreateJunction(str(outside), str(files / "link"))
    except OSError as exc:
        pytest.skip(f"cannot create a junction here: {exc}")
    client, _ = hub
    r = preview(client, ["link/x.txt"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "file_outside_root"


def test_without_a_files_root_references_are_refused(hub):
    client, _ = hub
    client.app.state.files_root = None
    r = preview(client, ["src/app.py"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "file_refs_disabled"


def test_the_picker_lists_only_referenceable_files(hub):
    client, _ = hub
    listed = client.get("/api/files", headers=USER).json()["files"]
    assert "src/app.py" in listed and "文档/说明 一.md" in listed
    assert not any(f in listed for f in (".env", "var/run.log"))
    assert client.get("/api/files", headers=USER, params={"q": "APP"}).json()["files"] == ["src/app.py"]
    assert client.get("/api/files").status_code == 401


def test_the_budget_counts_the_file_text(hub):
    client, _ = hub
    cid = conversation(client)
    small = submit(client, cid, key="a", text="看一下")
    big = submit(client, cid, key="b", text="看一下", file_refs=["src/app.py"])
    assert big["budget"]["estimate_visible"] == big["context_preview"]["estimate"]
    assert big["context_preview"]["estimate"] > small["context_preview"]["estimate"] + len(APP) // 4 - 1
