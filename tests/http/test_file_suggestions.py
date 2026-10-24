"""T-1g: local file suggestions and the over-budget hint (offline stubs only).

Suggestions come from the message's paths, the chosen files and this
conversation's recent references; they are only suggestions (nothing is
created or added) and never cross the files boundary. Over the per-call
budget the preview says so; nothing is trimmed.
"""

from agenthub_kernel.file_suggest import WHOLE_FILE_CHARS
from tests.http.test_conversation_stream import make_client, submit  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, approve, conversation, run
from tests.http.test_file_refs import files, hub, invocations, preview  # noqa: F401  (fixtures)


def suggestions(client, text="", refs=(), cid=None):
    r = client.post("/api/file-suggestions", headers=USER,
                    json={"conversation_id": cid, "text": text, "file_refs": list(refs)})
    assert r.status_code == 200, r.text
    return r.json()["suggestions"]


def by_path(items):
    return {s["path"]: s for s in items}


def test_paths_in_the_message_are_suggested_and_excluded_ones_are_not(hub):
    client, _ = hub
    before = invocations(client)
    found = by_path(suggestions(client, 'src/app.py:20 出错了，另见 "文档/说明 一.md"，以及 .env 和 ../outside.txt'))
    assert set(found) == {"src/app.py", "文档/说明 一.md"}
    app = found["src/app.py"]
    assert (app["spec"], app["reason"]) == ("src/app.py:L5-35", "mentioned_line")  # around the line
    assert found["文档/说明 一.md"]["spec"] == "文档/说明 一.md"  # small: the whole file
    assert invocations(client) == before  # suggesting creates nothing


def test_a_traceback_and_a_bare_file_name_are_found(hub):
    client, _ = hub
    tb = suggestions(client, 'Traceback:\n  File "src/app.py", line 3, in main')
    assert [s["spec"] for s in tb] == ["src/app.py:L1-18"]
    named = suggestions(client, "app.py 里的逻辑不对")
    assert [(s["spec"], s["reason"]) for s in named] == [("src/app.py", "mentioned")]


def test_related_files_of_the_chosen_ones(hub, files):
    client, _ = hub
    (files / "tests").mkdir()
    (files / "tests" / "test_app.py").write_text("def test_app():\n    pass\n", encoding="utf-8")
    found = suggestions(client, refs=["src/app.py:L1-5"])
    assert [(s["path"], s["reason"]) for s in found] == [("tests/test_app.py", "related")]
    back = suggestions(client, refs=["tests/test_app.py"])
    assert [(s["path"], s["reason"]) for s in back] == [("src/app.py", "related")]


def test_recent_references_of_this_conversation(hub):
    client, _ = hub
    cid = conversation(client)
    turn = submit(client, cid, key="a", text="看一下", file_refs=["src/app.py:L2-4"])
    approve(client, turn)
    assert run(client, turn).json()["status"] == "succeeded"
    found = suggestions(client, cid=cid)
    assert [(s["spec"], s["reason"]) for s in found] == [("src/app.py:L2-4", "recent")]
    assert suggestions(client, refs=["src/app.py"], cid=cid) == []  # already chosen


def test_a_large_file_asks_for_a_range_instead_of_the_whole_file(hub, files):
    client, _ = hub
    (files / "src" / "big.py").write_text("x = 1\n" * (WHOLE_FILE_CHARS // 4), encoding="utf-8")
    [big] = suggestions(client, "看看 src/big.py")
    assert big["needs_range"] is True and big["spec"] is None
    [window] = suggestions(client, "src/big.py:100 有问题")
    assert window["spec"] == "src/big.py:L85-115" and window["needs_range"] is False


def test_without_a_files_root_nothing_is_suggested(hub):
    client, _ = hub
    client.app.state.files_root = None
    r = client.post("/api/file-suggestions", headers=USER, json={"text": "src/app.py"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "file_refs_disabled"


def test_over_the_per_call_budget_the_preview_asks_to_narrow_and_trims_nothing(hub):
    client, _ = hub
    cid = conversation(client)
    submit(client, cid, key="opener", text="hi")
    root = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["root_task_id"]
    r = client.patch(f"/api/tasks/{root}/policy", headers=USER,
                     json={"token_budget_per_call": 50, "token_budget_mode": "strict"})
    assert r.status_code == 200, r.text
    over = preview(client, ["src/app.py"], conversation_id=cid).json()
    assert over["budget"]["decision"]["code"] == "estimate_exceeds_per_call_budget"
    assert over["budget"]["estimate"] == over["context_preview"]["estimate"] > 50
    assert "line 1\n" in over["sent_text"] and "line 40" in over["sent_text"]  # not trimmed
    narrow = preview(client, ["src/app.py:L1-2"], conversation_id=cid).json()
    assert narrow["budget"]["decision"]["allowed"] is True
    assert preview(client, ["src/app.py"]).json()["budget"] is None  # no conversation, no budget
