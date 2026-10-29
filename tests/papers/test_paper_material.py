"""Wrong papers, broken files, unreachable addresses, no results, forged evidence, citations, quality."""

from __future__ import annotations

import json

from agenthub_kernel.papers.service import RETRY_LIMIT
from tools.demo.paper_fixtures import FixtureWeb, feed, make_pdf, supplement_pdf

from tests.papers.conftest import Hub, waiting


def _files(view, round_=1):
    batch = next(b for b in view["supplement"]["batches"] if b["round"] == round_)
    return {f["origin"]: f for f in batch["files"]}


def test_batch_reports_every_file_problem_without_counting_it_as_supplied(hub):
    hub.prove()
    task = hub.create()
    hub.run(task, until=waiting)
    c, d, e = (hub.paper_id(task, s) for s in ("2601.00103", "2601.00104", "2601.00105"))
    r = hub.upload(task, [
        ("wrong.pdf", supplement_pdf("2601.00102")),            # another paper, targeted at C
        ("broken.pdf", b"%PDF-1.4 not really a pdf"),            # corrupt, targeted at D
        ("scan.pdf", make_pdf(["", ""], text=False)),            # scanned-looking, targeted at E
        ("d-full.pdf", supplement_pdf("2601.00104")),            # correct, untargeted: matched by content
        ("d-again.pdf", supplement_pdf("2601.00104")),           # identical content again
    ], targets=[c, d, e, "", ""])
    assert r.status_code == 200, r.text
    req = hub.view(task)["supplement"]["request_id"]
    # an address that does not answer
    assert hub.api("POST", f"/api/papers/{task}/supplement/refs", json={
        "request_id": req, "kind": "url", "value": "https://files.example.org/missing.pdf", "target_paper_id": c,
    }).status_code == 200
    loopback = hub.api("POST", f"/api/papers/{task}/supplement/refs", json={
        "request_id": req, "kind": "url", "value": "http://127.0.0.1:80/c.pdf", "target_paper_id": c})
    assert loopback.status_code == 409 and loopback.json()["detail"]["code"] == "resource_refused"
    outside = hub.api("POST", f"/api/papers/{task}/supplement/refs", json={
        "request_id": req, "kind": "local", "value": "C:\\Windows\\win.ini", "target_paper_id": c})
    assert outside.status_code == 409
    hub.submit(task)
    view = hub.run(task, until=lambda v: v["supplement"]["status"] == "awaiting_more_choice")
    files = _files(view)
    assert files["wrong.pdf"]["status"] == "mismatch"
    assert files["broken.pdf"]["status"] == "parse_failed"
    assert files["scan.pdf"]["status"] == "parse_failed" and "扫描" in files["scan.pdf"]["detail"]
    assert files["d-full.pdf"]["status"] == "accepted" and files["d-full.pdf"]["matched_paper_id"] == d
    assert files["d-again.pdf"]["status"] == "duplicate"
    assert files["https://files.example.org/missing.pdf"]["status"] == "fetch_failed"
    remaining = {i["paper_id"] for i in view["supplement"]["items"] if i["state"] == "missing"}
    assert remaining == {c, e}  # one follow-up question lists all that is still missing


def test_url_and_local_supplements_inside_the_approved_rules(tmp_path):
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "c.pdf").write_bytes(supplement_pdf("2601.00103"))
    hub = Hub(tmp_path).open()
    try:
        hub.prove()
        task = hub.create(target=4)
        hub.run(task, until=waiting)
        c, d = hub.paper_id(task, "2601.00103"), hub.paper_id(task, "2601.00104")
        req = hub.view(task)["supplement"]["request_id"]
        hub.web.extra["https://files.example.org/d.pdf"] = (302, "", b"https://cdn.example.org/d.pdf")
        hub.web.extra["https://cdn.example.org/d.pdf"] = (200, "application/pdf", supplement_pdf("2601.00104"))
        for kind, value, target in (("local", str(tmp_path / "local" / "c.pdf"), c),
                                    ("url", "https://files.example.org/d.pdf", d)):
            r = hub.api("POST", f"/api/papers/{task}/supplement/refs",
                        json={"request_id": req, "kind": kind, "value": value, "target_paper_id": target})
            assert r.status_code == 200, r.text
        hub.submit(task)
        view = hub.run(task)
        assert view["status"] == "completed" and view["supplement"]["decision"] == "all_supplied"
        origins = {p["source_id"]: (p["document"] or {}).get("origin") for p in view["papers"] if p["included"]}
        assert origins["2601.00103"] == "local" and origins["2601.00104"] == "url"
    finally:
        hub.close()


def test_redirect_to_a_private_address_is_refused(tmp_path):
    web = FixtureWeb()
    web.extra["https://arxiv.org/pdf/2601.00101v1"] = (302, "", b"http://10.0.0.5/internal.pdf")
    hub = Hub(tmp_path, web=web).open()
    try:
        hub.prove()
        task = hub.create(target=2)
        view = hub.run(task, until=waiting)
        a = next(p for p in view["papers"] if p["source_id"] == "2601.00101")
        assert a["fulltext_status"] == "missing" and a["fulltext_reason"] == "host_not_allowed"
        assert "http://10.0.0.5/internal.pdf" not in hub.fetcher.calls
    finally:
        hub.close()


def test_no_results_delivers_an_explained_insufficient_report(tmp_path):
    base = FixtureWeb()
    web = lambda url: (200, "application/atom+xml", feed([])) if "api/query" in url else base(url)  # noqa: E731
    hub = Hub(tmp_path, web=web).open()
    try:
        hub.prove()
        task = hub.create()
        view = hub.run(task)
        assert view["status"] == "completed" and view["quality_status"] == "failed"
        assert view["outcome"]["insufficient"]["reason"] == "no_results"
        assert not [s for s in view["steps"] if s["name"] in ("screen", "extract", "synthesize", "section")]
        md = hub.api("GET", f"/api/papers/{task}/artifacts/review.md").text
        assert "资料不足说明" in md and "质量状态：failed" in md
    finally:
        hub.close()


def test_forged_quotes_never_become_evidence(hub):
    hub.prove()
    task = hub.create(topic="SIM_FORGE retrieval augmented generation evaluation", target=2)
    view = hub.run(task)
    extracts = [s for s in view["steps"] if s["name"] == "extract"]
    # the first reply and both bounded repairs failed the quote check: the papers stay without evidence
    assert extracts and all(s["status"] == "failed" and len(s["invocation_ids"]) == 1 + RETRY_LIMIT
                            for s in extracts)
    assert hub.api("GET", f"/api/papers/{task}/evidence").json()["evidence"] == []
    assert view["outcome"]["insufficient"]["reason"] == "no_evidence" and view["quality_status"] == "failed"
    assert all(p["read_status"] == "failed" for p in view["papers"] if p["included"])


def test_unknown_citation_stays_visible_and_fails_quality(hub):
    hub.prove()
    task = hub.create(topic="SIM_UNKNOWN_EV retrieval augmented generation evaluation", target=2)
    view = hub.run(task)
    assert view["status"] == "completed" and view["quality_status"] == "failed"
    md = hub.api("GET", f"/api/papers/{task}/artifacts/review.md").text
    assert "[引用缺失:ev-0000000000]" in md
    report = json.loads(hub.api("GET", f"/api/papers/{task}/artifacts/research-report.json").content)
    assert report["citation_check"]["unknown_ids"] == ["ev-0000000000"]


def test_format_error_gets_one_counted_repair_call(hub):
    hub.prove()
    task = hub.create(topic="SIM_BAD_JSON retrieval augmented generation evaluation", target=2)
    view = hub.run(task)
    plan = next(s for s in view["steps"] if s["name"] == "plan")
    assert plan["status"] == "succeeded" and len(plan["invocation_ids"]) == 2
    assert view["status"] == "completed"


def test_review_revision_is_bounded_and_rechecked(hub):
    hub.prove()
    task = hub.create(topic="SIM_REVISE retrieval augmented generation evaluation", target=2)
    view = hub.run(task)
    assert view["status"] == "completed"
    assert all(s["revisions"] == 1 and s["review_status"] == "passed" for s in view["sections"]
               if s["status"] == "written")
    assert view["quality_status"] == "passed"


def test_without_a_reviewer_quality_is_unavailable_not_passed(hub):
    hub.prove(agents=("claude-code",))
    task = hub.create(target=2, review=None)
    view = hub.run(task)
    assert view["status"] == "completed" and view["quality_status"] == "unavailable"
    assert hub.ds_transport.calls == 0
    md = hub.api("GET", f"/api/papers/{task}/artifacts/review.md").text
    assert "草稿" in md


def test_paper_without_abstract_or_fulltext_is_metadata_only(hub):
    hub.prove()
    task = hub.create(target=6)
    hub.run(task, until=waiting)
    g = hub.paper_id(task, "2601.00107")
    view = hub.view(task)
    assert g in {i["paper_id"] for i in view["supplement"]["items"]}
    item = next(i for i in view["supplement"]["items"] if i["paper_id"] == g)
    assert item["abstract_available"] is False and "[年份" not in item["reference"]
    hub.choose(task, "continue")
    view = hub.run(task)
    paper = next(p for p in view["papers"] if p["paper_id"] == g)
    assert paper["read_depth"] == "metadata_only" and paper["evidence"] == 0
    assert not [s for s in view["steps"] if s["name"] == "extract" and s["target"] == g]
