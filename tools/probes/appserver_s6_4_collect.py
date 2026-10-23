"""S6-4: judge one web run (tool decline through /chat) from what the Hub stored.

The run itself is driven in the browser at /chat against ``serve.py --adapter
codex --codex-transport app-server`` (see docs/appserver-s6-plan.md §12). This
script only READS the run's data directory afterwards - the kernel database
(``agent-hub.db``, opened read-only) and the app-server execution store - and
writes ``var/appserver-s6-4/<label>/result.json`` with structural facts and a
verdict. The prompt, the reply and command output are never written; the
fixed command and the expected reply are compared in memory.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for sub in ("packages/contracts", "packages/adapters"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

from agenthub_adapters.appserver_adapter import PINNED_APPSERVER  # noqa: E402
from agenthub_adapters.appserver_s3_evidence import COMMAND as FIXED_COMMAND  # noqa: E402
from agenthub_adapters.appserver_s3_evidence import EXPECTED_REPLY  # noqa: E402

RUN_ROOT = REPO / "var" / "appserver-s6-4"
APPROVAL = "item/commandExecution/requestApproval"
PINNED_BINDING = (f"codex-app-server@{PINNED_APPSERVER['version']};sha256={PINNED_APPSERVER['sha256']};"
                  f"size={PINNED_APPSERVER['size']}")


def _rows(db: Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def collect(data_dir: Path, *, expected_inner: str = FIXED_COMMAND, expected_reply: str = EXPECTED_REPLY,
            expected_binding: str = PINNED_BINDING) -> dict:
    hub, store = data_dir / "agent-hub.db", data_dir / "appserver-executions.db"
    convs = _rows(hub, "select id from conversations")
    invs = _rows(hub, "select id, status, snapshot from invocations")
    approvals = _rows(hub, "select invocation_id, status from approvals")
    cards = _rows(hub, "select id, invocation_id, method, status, delivery, decision_sent, "
                       "available_decisions, details from tool_approvals")
    replies = _rows(hub, "select text, invocation_id from chat_messages where role = 'assistant'")
    events = _rows(hub, "select seq, type, payload from conversation_events order by seq")
    held = _rows(hub, "select count(*) as n from quota_reservations where released_at is null")[0]["n"]
    records = _rows(store, "select invocation_id, status, payload from appserver_executions")

    inv = invs[0] if len(invs) == 1 else {}
    snapshot = _json(inv.get("snapshot")) or {}
    record = _json(next((r["payload"] for r in records if r["invocation_id"] == inv.get("id")), None)) or {}
    card = cards[0] if len(cards) == 1 else {}
    details = _json(card.get("details")) or {}
    view = details.get("command_view") or {}
    reply = next((r["text"] for r in replies if r["invocation_id"] == inv.get("id")), "")
    workdir = record.get("workdir")
    descendants = record.get("descendants")
    tools = record.get("tool_activity") or []
    order = [e["type"] for e in events]

    def at(kind: str, **match: Any) -> Optional[int]:
        for index, e in enumerate(events):
            payload = _json(e["payload"]) or {}
            if e["type"] == kind and all(payload.get(k) == v for k, v in match.items()):
                return index
        return None

    facts = {
        "conversations": len(convs), "invocations": len(invs),
        "execution_mode": snapshot.get("execution_mode", "strict"),
        "binding_matches": snapshot.get("adapter_version") == expected_binding,
        "approval_status": approvals[0]["status"] if len(approvals) == 1 else None,
        "invocation_status": inv.get("status"), "held_slots": held,
        "cards": len(cards), "card_method": card.get("method"), "card_status": card.get("status"),
        "card_delivery": card.get("delivery"), "card_decision_sent": card.get("decision_sent"),
        "card_available_decisions": _json(card.get("available_decisions")),
        "provider_available_decisions": details.get("provider_available_decisions"),
        "command_view_parsed": view.get("parsed") is True,
        "inner_command_is_fixed": view.get("inner_command") == expected_inner,
        "reason_marked_model": details.get("reason_source") == "model" if "reason" in details else None,
        "reply_exact": reply.strip() == expected_reply, "reply_contains": expected_reply in reply,
        "turn_status": record.get("turn_status"), "terminal": record.get("terminal"),
        "opening_method": record.get("opening_method"),
        "accept_suppressed": record.get("accept_suppressed") or [],
        "auto_declined": record.get("declined_requests") or [],
        "unanswered": record.get("unanswered_requests") or [],
        "tool_items": [{k: t.get(k) for k in ("item_type", "status", "process_id_reported", "completed",
                                               "lifecycle_seen", "output_events")} for t in tools],
        "tool_process_state": record.get("tool_process_state"),
        "native_agents": len(record.get("native_agents") or []),
        "foreign_events": record.get("foreign_events"), "late_events": record.get("late_events"),
        "usage_anomalies": record.get("usage_anomalies"), "usage_present": bool(record.get("usage")),
        "local_process_exit_confirmed": record.get("local_process_exit_confirmed"),
        "descendants_checked": isinstance(descendants, dict) and isinstance(descendants.get("before_close"), list)
        and isinstance(descendants.get("after_grace"), list),
        "descendants_survived": bool((descendants or {}).get("after_grace")),
        "workdir_empty": (workdir is not None and Path(workdir).is_dir() and not any(Path(workdir).iterdir())),
        "event_order_ok": None not in (at("tool_approval.requested"), at("tool_approval.status", to="declined"),
                                       at("tool_approval.delivery", delivery="sent"), at("stream.turn_ended"),
                                       at("invocation.status", to="succeeded"))
        and at("tool_approval.requested") < at("tool_approval.status", to="declined")
        < at("tool_approval.delivery", delivery="sent") < at("stream.turn_ended")
        < at("invocation.status", to="succeeded"),
        "event_types": order,
    }
    fail, inconclusive = judge(facts)
    facts["reason_codes"] = fail + inconclusive
    facts["verdict"] = "FAIL" if fail else ("INCONCLUSIVE" if inconclusive else "PASS")
    return facts


def judge(f: dict) -> tuple[list, list]:
    """(FAIL-class, INCONCLUSIVE-class). FAIL = a boundary was broken; INCONCLUSIVE = not shown."""
    fail, inconclusive = [], []
    if f["invocations"] > 1 or f["conversations"] > 1:
        fail.append("budget_exceeded")
    if f["execution_mode"] != "supervised":
        fail.append("not_supervised")
    if not f["binding_matches"]:
        fail.append("binding_mismatch")
    if f["card_decision_sent"] == "accept" or f["accept_suppressed"]:
        fail.append("accept_path_reached")
    if not f["workdir_empty"] or any(t["process_id_reported"] for t in f["tool_items"]):
        fail.append("execution_evidence")
    if f["card_status"] == "declined" and not (f["card_delivery"] == "sent" and f["card_decision_sent"] == "decline"):
        fail.append("decline_not_delivered")
    if f["cards"] > 1:
        fail.append("more_than_one_card")
    if f["cards"] == 0:
        inconclusive.append("no_tool_card")
    elif f["card_method"] != APPROVAL:
        inconclusive.append("card_not_a_command_approval")
    if f["cards"] == 1:
        if f["card_status"] == "expired":
            inconclusive.append("card_expired_not_clicked")
        elif f["card_status"] != "declined":
            inconclusive.append(f"card_status_{f['card_status']}")
        if f["card_available_decisions"] != ["decline"]:
            fail.append("card_offered_more_than_decline")
        if not (f["command_view_parsed"] and f["inner_command_is_fixed"]):
            inconclusive.append("command_not_the_fixed_one")
    if f["auto_declined"] or f["unanswered"]:
        inconclusive.append("request_outside_the_card")
    if any(t["item_type"] != "commandExecution" or t["status"] != "declined" for t in f["tool_items"]) \
            or f["tool_process_state"] not in ("items_completed", "none"):
        inconclusive.append("other_tool_activity")
    if f["native_agents"]:
        inconclusive.append("native_agent")
    if f["invocation_status"] != "succeeded" or f["turn_status"] != "completed" or f["terminal"] is not True:
        inconclusive.append(f"status_{f['invocation_status']}_{f['turn_status']}")
    if not f["reply_contains"]:
        inconclusive.append("reply_not_declined")
    if f["approval_status"] != "consumed":
        inconclusive.append(f"approval_{f['approval_status']}")
    if f["foreign_events"] or f["late_events"] or f["usage_anomalies"] or not f["usage_present"]:
        inconclusive.append("event_or_usage_anomaly")
    if not f["local_process_exit_confirmed"] or not f["descendants_checked"] or f["descendants_survived"]:
        inconclusive.append("local_cleanup_unconfirmed")
    if f["held_slots"]:
        inconclusive.append("quota_slot_held")
    if f["cards"] == 1 and f["card_status"] == "declined" and not f["event_order_ok"]:
        inconclusive.append("event_order")
    return fail, inconclusive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--label", default="real")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    facts = collect(data_dir)
    facts["data_dir"] = str(data_dir)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"{args.label}-", dir=RUN_ROOT)) / "result.json"
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"S6-4 {facts['verdict']} {facts['reason_codes']}")
    print(f"Evidence: {out}")
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
