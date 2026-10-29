"""P6 end to end on fixed material: create -> search -> screen -> fetch -> supplement -> read
-> synthesize -> outline -> write -> review -> export. Offline simulation only."""

from __future__ import annotations

import json

from tools.demo.paper_fixtures import BY_ID, supplement_pdf

from tests.papers.conftest import waiting


def test_full_flow_with_two_batches_then_continue(hub):
    hub.prove()
    before = hub.launched()
    task = hub.create()
    view = hub.run(task, until=waiting)
    assert view["status"] == "waiting_input", view["stage"]
    sup = view["supplement"]
    # One summary request listing every included paper without readable full text.
    assert sup["status"] == "awaiting_choice" and sup["policy"] == "continue_allowed"
    assert sup["remaining_seconds"] == 30 * 60
    reasons = {i["paper_id"]: i["reason"] for i in sup["items"]}
    by_source = {p["source_id"]: p for p in view["papers"]}
    assert set(reasons.values()) == {"no_fulltext_link", "corrupt", "scanned"}
    assert len(sup["items"]) == 3 and all("[" in i["reference"] and "." in i["reference"] for i in sup["items"])
    assert by_source["2601.00106"]["screen"] == "exclude" and not by_source["2601.00106"]["included"]
    assert any(m["reason"] == "same_title_and_first_author" for m in by_source["2601.00101"]["merged"])
    assert any(m["reason"] == "same_doi" for m in by_source["2601.00101"]["merged"])
    # Nothing downstream of the reading gate has started.
    assert not [s for s in view["steps"] if s["name"] in ("extract", "synthesize", "section")]

    c, d = hub.paper_id(task, "2601.00103"), hub.paper_id(task, "2601.00104")
    r = hub.upload(task, [("c.pdf", supplement_pdf("2601.00103"))], targets=[c])
    assert r.status_code == 200, r.text
    view = hub.view(task)
    assert view["supplement"]["status"] == "awaiting_files" and view["supplement"]["deadline_at"] is None
    assert hub.submit(task).status_code == 200
    view = hub.run(task, until=lambda v: v["supplement"]["status"] == "awaiting_more_choice")
    sup = view["supplement"]
    assert sup["status"] == "awaiting_more_choice" and sup["remaining"] == 2
    assert sup["batches"][0]["files"][0]["status"] == "accepted"
    hub.clock.advance(hours=2)  # no deadline once the user chose to supplement
    view = hub.run(task, passes=3, until=lambda v: False)
    assert view["supplement"]["status"] == "awaiting_more_choice"

    assert hub.choose(task, "supplement").json()["status"] == "awaiting_files"
    hub.upload(task, [("d.pdf", supplement_pdf("2601.00104"))], targets=[d])
    hub.submit(task)
    view = hub.run(task, until=lambda v: v["supplement"]["status"] == "awaiting_more_choice")
    assert view["supplement"]["remaining"] == 1 and view["supplement"]["round"] == 2
    assert hub.choose(task, "continue").json()["status"] == "resolved"

    view = hub.run(task)
    assert view["status"] == "completed", (view["stage"], view["phase"])
    assert view["execution_status"] == "completed"
    depth = {p["source_id"]: p["read_depth"] for p in view["papers"] if p["included"]}
    assert depth == {"2601.00101": "fulltext", "2601.00102": "fulltext", "2601.00103": "fulltext",
                     "2601.00104": "fulltext", "2601.00105": "abstract"}
    assert view["quality_status"] == "passed", view["outcome"].get("quality_reasons")
    names = {a["name"] for a in view["artifacts"]}
    assert names == {"review.md", "references.json", "evidence.json", "research-report.json"}
    assert hub.launched() > before

    # Evidence: real ids, verbatim quotes, locatable chunks.
    evidence = hub.api("GET", f"/api/papers/{task}/evidence").json()["evidence"]
    assert evidence and all(e["verified"] for e in evidence)
    for e in evidence:
        if e["level"] == "fulltext":
            chunk = hub.api("GET", f"/api/papers/{task}/chunks/{e['chunk_id']}").json()
            assert e["quote"].lower() in " ".join(chunk["text"].split()).lower()
            assert chunk["pdf_page"] == e["location"]["pdf_page"]
    refs = json.loads(hub.api("GET", f"/api/papers/{task}/artifacts/references.json").content)
    for ref in refs:
        fixture = BY_ID[ref["arxiv_id"]]
        assert ref["title"] == fixture["title"] and ref["authors"] == fixture["authors"]
    md = hub.api("GET", f"/api/papers/{task}/artifacts/review.md").text
    assert "[ev:" not in md and "## 参考文献" in md and "仅摘要 1 篇" in md
    report = json.loads(hub.api("GET", f"/api/papers/{task}/artifacts/research-report.json").content)
    assert report["supplement"]["decision"] == "user_continue"
    assert report["citation_check"]["unknown_ids"] == []
    assert report["usage"]["calls"] == len([i for s in report["steps"] for i in s["invocation_ids"]])
