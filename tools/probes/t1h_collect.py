"""Judge one T-1h web run (docs/t1h-real-acceptance.md) from what the Hub stored.

Two supervised calls in one conversation under a strict token budget, each
with the same file reference: Codex (app-server), then Claude Code. The
referenced fixture file holds a code word; each agent must answer it.

This script only READS the run's data directory - the kernel database
(read-only) and both adapters' execution stores - plus the fixture file, and
writes ``var/t1h/<label>/result.json`` with numbers, hashes and a verdict.
The code word, prompts and replies are compared in memory and never written;
only the code word's SHA-256 is. The preflight is the two-agent one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for sub in ("packages/contracts", "packages/adapters", "apps/worker"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
sys.path.insert(0, str(HERE))

from agenthub.context import check_preview, part_texts  # noqa: E402
from two_agent_collect import (  # noqa: E402
    CLAUDE_BINDING, CODEX_BINDING, _empty_dir, _json, _rows, preflight,
)

RUN_ROOT = REPO / "var" / "t1h"
AGENTS = ["codex", "claude-code"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect(data_dir: Path, *, code: str, fixture: Path, ref: str, codex_binding: str = CODEX_BINDING,
            claude_binding: str = CLAUDE_BINDING) -> dict:
    """Facts and a verdict. Only calls whose approval was consumed count: the
    opener that creates the conversation's task (so a budget can be set) is
    revoked by that budget change and never runs."""
    hub = data_dir / "agent-hub.db"
    invs = _rows(hub, "select id, status, snapshot from invocations")
    approvals = {r["invocation_id"]: r["status"] for r in _rows(hub, "select invocation_id, status from approvals")}
    msgs = _rows(hub, "select id, role, text, invocation_id, conversation_id from chat_messages order by id")
    convs = _rows(hub, "select id from conversations")
    cards = _rows(hub, "select invocation_id, decision_sent from tool_approvals")
    ledger = {r["invocation_id"]: r for r in _rows(hub, "select * from usage_ledger")}
    items = {r["invocation_id"]: r for r in _rows(hub, "select * from token_budget_items")}
    counters = _rows(hub, "select known, unsettled, reserved, unbounded from tree_token_budget")
    held = (_rows(hub, "select count(*) as n from quota_reservations where released_at is null") or [{"n": 0}])[0]["n"]
    records = {}
    for db, table in (("appserver-executions.db", "appserver_executions"),
                      ("claude-code-executions.db", "claude_code_executions")):
        records.update({r["invocation_id"]: _json(r["payload"]) or {}
                        for r in _rows(data_dir / db, f"select invocation_id, payload from {table}")})

    user = {m["invocation_id"]: m for m in msgs if m["role"] == "user"}
    reply = {m["invocation_id"]: m for m in msgs if m["role"] == "assistant"}
    invs = [i for i in invs if approvals.get(i["id"]) == "consumed"]
    invs.sort(key=lambda i: (user.get(i["id"], {}).get("id", float("inf")), i["id"]))
    fixture_lines = fixture.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")

    calls = []
    for inv in invs:
        snap = _json(inv["snapshot"]) or {}
        sent, preview = snap.get("task_input", ""), snap.get("context_preview")
        record = records.get(inv["id"], {})
        row = ledger.get(inv["id"]) or {}
        try:
            check_preview(sent, preview)
            preview_ok = True
        except (TypeError, ValueError):
            preview_ok = False
        files = [p for p in (preview or {}).get("parts", []) if p.get("kind") == "file_snippet"]
        parts, texts = (preview["parts"], part_texts(sent, preview)) if preview_ok else ([], [])
        snippet_texts = [t for p, t in zip(parts, texts) if p.get("kind") == "file_snippet"]
        expected = ["\n".join(fixture_lines[f["start_line"] - 1:f["end_line"]]) for f in files]
        item = items.get(inv["id"]) or {}
        calls.append({
            "agent": snap.get("agent_id"), "mode": snap.get("execution_mode", "strict"),
            "status": inv["status"], "approval": approvals.get(inv["id"]),
            "binding_matches": snap.get("adapter_version") == (codex_binding if snap.get("agent_id") == "codex"
                                                              else claude_binding),
            "preview_valid": preview_ok,
            "sent_equals_adapter_input": bool(record) and record.get("input_sha256") == _sha(sent),
            "file_parts": [{"path": f.get("path"), "lines": [f.get("start_line"), f.get("end_line")]} for f in files],
            "snippets_match_fixture": bool(files) and snippet_texts == expected,
            "code_in_user_text": code in (user.get(inv["id"]) or {}).get("text", ""),
            "reply_exact": (reply.get(inv["id"]) or {}).get("text", "").strip() == code,
            "record_present": bool(record), "workdir_empty": _empty_dir(record.get("workdir")),
            "tool_activity": len(record.get("tool_activity") or [])
            + sum(1 for c in cards if c["invocation_id"] == inv["id"]),
            "accept_sent": any(c["decision_sent"] == "accept" for c in cards if c["invocation_id"] == inv["id"]),
            "estimate_visible": (preview or {}).get("estimate"),
            "usage": {k: row.get(k) for k in ("completeness", "source", "input_total", "input_uncached",
                                             "cache_read", "cache_write", "output_total")},
            "budget_item": {k: item.get(k) for k in ("reservation", "reserved", "known", "unsettled",
                                                    "unbounded", "overrun", "state")},
        })
    known_sum = sum((c["usage"]["input_total"] or 0) + (c["usage"]["output_total"] or 0)
                    for c in calls if c["usage"]["completeness"] == "complete")
    item_sums = [sum((c["budget_item"][k] or 0) for c in calls) for k in ("known", "unsettled", "reserved", "unbounded")]
    facts = {
        "code_sha256": _sha(code), "ref": ref, "fixture_sha256": _sha(fixture.read_text(encoding="utf-8")),
        "conversations": len(convs), "invocations": len(invs), "agents": [c["agent"] for c in calls],
        "held_slots": held, "calls": calls,
        "budget_counters": list(counters[0].values()) if counters else None,
        "budget_item_sums": item_sums, "ledger_known_sum": known_sum,
    }
    fail, inconclusive = judge(facts)
    facts["reason_codes"] = fail + inconclusive
    facts["verdict"] = "FAIL" if fail else ("INCONCLUSIVE" if inconclusive else "PASS")
    return facts


def judge(f: dict) -> tuple[list, list]:
    """(FAIL-class, INCONCLUSIVE-class). FAIL = a boundary or an accounting rule broke."""
    fail, inconclusive = [], []
    if f["invocations"] > 2 or f["conversations"] > 1:
        fail.append("budget_exceeded")
    for i, c in enumerate(f["calls"]):
        tag = c["agent"] or f"call{i}"
        if c["mode"] != "supervised":
            fail.append(f"{tag}_not_supervised")
        if not c["binding_matches"]:
            fail.append(f"{tag}_binding_mismatch")
        if c["accept_sent"]:
            fail.append(f"{tag}_accept_path_reached")
        if not c["preview_valid"]:
            fail.append(f"{tag}_preview_invalid")
        if c["code_in_user_text"]:
            fail.append(f"{tag}_code_in_user_text")  # the file would prove nothing
        if not c["record_present"]:
            inconclusive.append(f"{tag}_record_missing")
            continue
        if not c["workdir_empty"]:
            fail.append(f"{tag}_execution_evidence")
        if not c["sent_equals_adapter_input"]:
            fail.append(f"{tag}_sent_differs_from_approved")
        if not c["snippets_match_fixture"]:
            fail.append(f"{tag}_snippet_mismatch")
        if c["status"] != "succeeded":
            inconclusive.append(f"{tag}_not_succeeded")
        if not c["reply_exact"]:
            inconclusive.append(f"{tag}_reply_not_the_code")
        if c["tool_activity"]:
            inconclusive.append(f"{tag}_tool_activity")
        if c["usage"]["completeness"] != "complete":
            inconclusive.append(f"{tag}_usage_not_complete")
        if c["budget_item"]["state"] != "settled":
            inconclusive.append(f"{tag}_budget_not_settled")
    if f["agents"] != AGENTS:
        inconclusive.append("calls_not_as_planned")
    if f["held_slots"]:
        inconclusive.append("quota_slot_held")
    if f["budget_counters"] is None:
        inconclusive.append("no_budget_counters")
    elif f["budget_counters"] != f["budget_item_sums"]:
        fail.append("budget_counters_differ_from_items")
    elif f["budget_counters"][2]:
        fail.append("reservation_left_in_flight")
    elif f["budget_counters"][0] != f["ledger_known_sum"]:
        fail.append("budget_known_differs_from_ledger")
    return fail, inconclusive


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preflight", metavar="URL", help="check the running Hub's /api/adapter, then exit")
    parser.add_argument("--data-dir")
    parser.add_argument("--fixture", help="the referenced fixture file")
    parser.add_argument("--ref", default="code.md:L1-3", help="the file reference used in both calls")
    parser.add_argument("--code", help="the run's code word (only its SHA-256 is written)")
    parser.add_argument("--label", default="real")
    args = parser.parse_args()
    if args.preflight:
        result = preflight(args.preflight, os.environ.get("AGENT_HUB_USER_TOKEN") or "dev-user-token")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("PREFLIGHT " + ("PASS" if not result["problems"] else "STOP " + str(result["problems"])))
        return 0 if not result["problems"] else 1
    if not (args.data_dir and args.code and args.fixture):
        parser.error("--data-dir, --fixture and --code are required to judge a run")
    facts = collect(Path(args.data_dir), code=args.code, fixture=Path(args.fixture), ref=args.ref)
    facts["data_dir"] = args.data_dir
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"{args.label}-", dir=RUN_ROOT)) / "result.json"
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"T-1H {facts['verdict']} {facts['reason_codes']}")
    print(f"Evidence: {out}")
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
