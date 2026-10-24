"""P4.5: conversation titles (offline stubs only; titling never calls a model).

An untitled conversation is named locally by its first message; a title given
at creation or by a rename is never replaced; compress and continue keeps the
source's title (the list marks the link, the time is in ``created_at``).
"""

import pytest

from agenthub_kernel.conversations import short_title
from tests.http.test_conversation_stream import make_client, submit  # noqa: F401  (fixture)
from tests.http.test_conversations import USER, conversation
from tests.http.test_file_refs import files, hub  # noqa: F401  (fixtures)
from tests.http.test_stage_summary import save


@pytest.mark.parametrize("text, title", [
    ("根据以下需求整理功能要点：新增任务、标记完成", "需求整理功"),
    ("请帮我 用三点概括需求样例里的功能", "用三点概括"),
    ("：新增任务", "新增任务"),  # an empty first clause falls back to the whole text
    ("Reply with exactly: P4-ABC. Do not use tools.", "Reply with exactly"),
    ("start", "start"),
    ("  。！  ", ""),
])
def test_short_title_is_local_and_short(text, title):
    assert short_title(text) == title


def listed(client, cid):
    convs = client.get("/api/conversations", headers=USER).json()["conversations"]
    return next(c for c in convs if c["conversation_id"] == cid)


def test_an_untitled_conversation_is_named_by_its_first_message(hub):
    client, _ = hub
    cid = client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]
    assert listed(client, cid)["title"] == ""
    submit(client, cid, key="a", text="根据以下需求整理功能要点：新增任务")
    assert listed(client, cid)["title"] == "需求整理功"
    submit(client, cid, key="b", text="另一个话题")
    assert listed(client, cid)["title"] == "需求整理功"


def test_a_given_or_renamed_title_is_kept(hub):
    client, _ = hub
    cid = conversation(client)
    submit(client, cid, key="a", text="根据以下需求整理功能要点")
    assert listed(client, cid)["title"] == "test chat"
    r = client.patch(f"/api/conversations/{cid}", headers=USER, json={"title": " 需求评审 "})
    assert r.status_code == 200 and r.json()["title"] == "需求评审"
    submit(client, cid, key="b", text="再来一条")
    assert listed(client, cid)["title"] == "需求评审"
    for body in ({"title": "  "}, {"title": ""}, {"title": "x", "extra": 1}):
        assert client.patch(f"/api/conversations/{cid}", headers=USER, json=body).status_code == 422
    assert client.patch("/api/conversations/conv-missing", headers=USER, json={"title": "x"}).status_code == 404
    assert listed(client, cid)["title"] == "需求评审"


def test_a_continuation_keeps_the_source_title(hub):
    client, _ = hub
    cid = client.post("/api/conversations", headers=USER, json={}).json()["conversation_id"]
    submit(client, cid, key="a", text="根据以下需求整理功能要点")
    summary = save(client, cid)
    r = client.post(f"/api/conversations/{cid}/compress", headers=USER, json={"summary_id": summary["summary_id"]})
    assert r.status_code == 200, r.text
    new = listed(client, r.json()["conversation_id"])
    assert new["title"] == "需求整理功" and new["parent_conversation_id"] == cid
    submit(client, new["conversation_id"], key="b", text="完全不同的内容")
    assert listed(client, new["conversation_id"])["title"] == "需求整理功"
