"""Judge one P4 web run (docs/p4-real-acceptance.md) from what the Hub stored.

One conversation, collaboration switch on, a hand-written two-item plan:
#1 Codex replies a code word given in its goal; #2 Claude Code, after #1,
must repeat the code it can only get from #1's handed-over result; then one
collect call hands both results to Claude Code. Every call supervised and
approved one by one.

Read-only: the kernel database and both adapters' execution stores. Writes
``var/p4/<label>/result.json`` with structural facts and a verdict; the code
word, prompts and replies are compared in memory, only the code's SHA-256 is
written. The preflight is the two-agent one.
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

from agenthub.collab import COLLECT_INSTRUCTION  # noqa: E402
from two_agent_collect import (  # noqa: E402
    CLAUDE_BINDING, CODEX_BINDING, FRAME, _empty_dir, _json, _rows, preflight,
)

RUN_ROOT = REPO / "var" / "p4"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect(data_dir: Path, *, code: str, codex_binding: str = CODEX_BINDING,
            claude_binding: str = CLAUDE_BINDING) -> dict:
    hub = data_dir / "agent-hub.db"
    invs = {r["id"]: {**r, "snapshot": _json(r["snapshot"]) or {}}
            for r in _rows(hub, "select id, task_id, status, snapshot from invocations")}
    approvals = {r["invocation_id"]: r["status"] for r in _rows(hub, "select invocation_id, status from approvals")}
    msgs = _rows(hub, "select id, role, text, invocation_id, conversation_id from chat_messages order by id")
    tasks = {r["id"]: r for r in _rows(hub, "select id, parent_task_id, depth, kind from tasks")}
    plans = _rows(hub, "select id, source from collab_plans")
    items = _rows(hub, "select id, plan_id, idx, agent_id, goal, depends_on, task_id from collab_items order by idx")
    attempts = _rows(hub, "select invocation_id, item_id, attempt from collab_attempts")
    ledger = {r["invocation_id"]: r for r in _rows(hub, "select invocation_id, completeness from usage_ledger")}
    cards = _rows(hub, "select invocation_id, decision_sent from tool_approvals")
    convs = _rows(hub, "select id from conversations")
    held = (_rows(hub, "select count(*) as n from quota_reservations where released_at is null") or [{"n": 0}])[0]["n"]
    records = {}
    for db, table in (("appserver-executions.db", "appserver_executions"),
                      ("claude-code-executions.db", "claude_code_executions")):
        records.update({r["invocation_id"]: _json(r["payload"]) or {}
                        for r in _rows(data_dir / db, f"select invocation_id, payload from {table}")})

    ran = [i for i in invs.values() if approvals.get(i["id"]) == "consumed"]
    user = {m["invocation_id"]: m for m in msgs if m["role"] == "user"}
    reply = {m["invocation_id"]: m for m in msgs if m["role"] == "assistant"}
    by_item = {a["item_id"]: a for a in attempts}
    item_inv = [by_item.get(i["id"], {}).get("invocation_id") for i in items]
    collect_inv = next((i["id"] for i in ran if (i["snapshot"].get("depth") or 0) == 0), None)

    def call(inv_id, binding):
        inv = invs.get(inv_id) or {}
        snap, record = inv.get("snapshot") or {}, records.get(inv_id, {})
        return {
            "present": bool(inv), "agent": snap.get("agent_id"), "status": inv.get("status"),
            "mode": snap.get("execution_mode", "strict"), "depth": snap.get("depth"),
            "binding_matches": snap.get("adapter_version") == binding,
            "sent_equals_adapter_input": bool(record) and record.get("input_sha256") == _sha(snap.get("task_input", "")),
            "record_present": bool(record), "workdir_empty": _empty_dir(record.get("workdir")),
            "usage_complete": (ledger.get(inv_id) or {}).get("completeness") == "complete",
            "num_turns": record.get("num_turns"), "models": record.get("models"),
            "tool_activity": len(record.get("tool_activity") or [])
            + sum(1 for c in cards if c["invocation_id"] == inv_id),
            "accept_sent": any(c["decision_sent"] == "accept" for c in cards if c["invocation_id"] == inv_id),
            "handoff": [h.get("message_id") for h in snap.get("handoff") or []],
        }

    i1, i2 = (item_inv + [None, None])[:2]
    r1, r2, rc = (reply.get(x) or {} for x in (i1, i2, collect_inv))
    goal2 = items[1]["goal"] if len(items) > 1 else ""
    facts = {
        "code_sha256": _sha(code), "conversations": len(convs), "plans": len(plans),
        "plan_source": plans[0]["source"] if plans else None,
        "items": [{"agent": i["agent_id"], "depends_on": _json(i["depends_on"]) or []} for i in items],
        "attempts_per_item": [sum(1 for a in attempts if a["item_id"] == i["id"]) for i in items],
        "calls_run": len(ran), "held_slots": held,
        "sub_task_kinds": sorted(t["kind"] for t in tasks.values() if t["depth"] == 1),
        "item1": call(i1, codex_binding), "item2": call(i2, claude_binding),
        "collect": call(collect_inv, claude_binding),
        # The order, structurally: #2's approved input carries #1's reply, which
        # only exists once #1 has finished.
        "item2_handoff_is_item1_reply": bool(r1) and call(i2, claude_binding)["handoff"] == [r1.get("id")],
        "item2_sent_is_frame_plus_goal": bool(r1) and (invs.get(i2) or {}).get("snapshot", {}).get("task_input") == "\n\n".join(
            [FRAME.format(agent="codex", mid=r1.get("id"), text=r1.get("text", "")), goal2]),
        "collect_handoff_is_both_replies": bool(r1 and r2) and call(collect_inv, claude_binding)["handoff"]
        == [r1.get("id"), r2.get("id")],
        "collect_text_is_instruction": (user.get(collect_inv) or {}).get("text") == COLLECT_INSTRUCTION,
        "code_in_item2_goal": bool(code) and code in goal2,
        "code_in_collect_text": bool(code) and code in (user.get(collect_inv) or {}).get("text", ""),
        "item1_reply_exact": r1.get("text", "").strip() == code,
        "item2_reply_exact": r2.get("text", "").strip() == code,
        "collect_reply_has_code": bool(code) and code in rc.get("text", ""),
    }
    fail, inconclusive = judge(facts)
    facts["reason_codes"] = fail + inconclusive
    facts["verdict"] = "FAIL" if fail else ("INCONCLUSIVE" if inconclusive else "PASS")
    return facts


def judge(f: dict) -> tuple[list, list]:
    """(FAIL-class, INCONCLUSIVE-class). FAIL = a boundary broke; INCONCLUSIVE = not shown."""
    fail, inconclusive = [], []
    if f["calls_run"] > 3 or f["conversations"] > 1 or f["plans"] > 1 or any(n > 1 for n in f["attempts_per_item"]):
        fail.append("budget_exceeded")
    for name in ("item1", "item2", "collect"):
        c = f[name]
        if not c["present"]:
            inconclusive.append(f"{name}_missing")
            continue
        if c["mode"] != "supervised":
            fail.append(f"{name}_not_supervised")
        if not c["binding_matches"]:
            fail.append(f"{name}_binding_mismatch")
        if c["accept_sent"]:
            fail.append(f"{name}_accept_path_reached")
        if not c["record_present"]:
            inconclusive.append(f"{name}_record_missing")
            continue
        if not c["workdir_empty"]:
            fail.append(f"{name}_execution_evidence")
        if not c["sent_equals_adapter_input"]:
            fail.append(f"{name}_sent_differs_from_approved")
        if c["status"] != "succeeded":
            inconclusive.append(f"{name}_not_succeeded")
        if c["tool_activity"]:
            inconclusive.append(f"{name}_tool_activity")
        if not c["usage_complete"]:
            inconclusive.append(f"{name}_usage_not_complete")
        if c["agent"] == "claude-code" and c["num_turns"] != 1:
            inconclusive.append(f"{name}_turns_not_one")  # on its own, whatever the usage says
    if f["item1"]["present"] and f["item1"]["depth"] != 1 or f["item2"]["present"] and f["item2"]["depth"] != 1:
        fail.append("item_not_a_sub_task")
    if f["collect"]["present"] and f["collect"]["depth"] != 0:
        fail.append("collect_not_root_level")
    if f["item2"]["present"] and not (f["item2_handoff_is_item1_reply"] and f["item2_sent_is_frame_plus_goal"]):
        fail.append("item2_input_mismatch")
    if f["collect"]["present"] and not f["collect_handoff_is_both_replies"]:
        fail.append("collect_input_mismatch")
    if f["code_in_item2_goal"] or f["code_in_collect_text"]:
        fail.append("code_outside_item1")  # the handoff would prove nothing
    if [i["agent"] for i in f["items"]] != ["codex", "claude-code"] or f["items"][1:2] and f["items"][1]["depends_on"] != [1]:
        inconclusive.append("plan_not_as_planned")
    if f["plan_source"] != "manual" or f["sub_task_kinds"] != ["user", "user"]:
        inconclusive.append("plan_not_hand_written")
    if not f["collect_text_is_instruction"]:
        inconclusive.append("collect_text_changed")
    if f["held_slots"]:
        inconclusive.append("quota_slot_held")
    for name, ok in (("item1_reply_not_the_code", f["item1_reply_exact"]),
                     ("item2_reply_not_the_code", f["item2_reply_exact"]),
                     ("collect_reply_without_code", f["collect_reply_has_code"])):
        if not ok:
            inconclusive.append(name)
    return fail, inconclusive


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preflight", metavar="URL", help="check the running Hub's /api/adapter, then exit")
    parser.add_argument("--data-dir")
    parser.add_argument("--code", help="the run's code word (only its SHA-256 is written)")
    parser.add_argument("--label", default="real")
    args = parser.parse_args()
    if args.preflight:
        result = preflight(args.preflight, os.environ.get("AGENT_HUB_USER_TOKEN") or "dev-user-token")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("PREFLIGHT " + ("PASS" if not result["problems"] else "STOP " + str(result["problems"])))
        return 0 if not result["problems"] else 1
    if not (args.data_dir and args.code):
        parser.error("--data-dir and --code are required to judge a run")
    facts = collect(Path(args.data_dir), code=args.code)
    facts["data_dir"] = args.data_dir
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"{args.label}-", dir=RUN_ROOT)) / "result.json"
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"P4 {facts['verdict']} {facts['reason_codes']}")
    print(f"Evidence: {out}")
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
