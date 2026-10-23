"""Judge one two-agent web run (Codex, then Claude Code with a handoff) from what the Hub stored.

The run itself is driven in the browser at /chat against ``serve.py --adapter
multi`` (docs/multi-agent.md §5). This script only READS the run's data
directory afterwards - the kernel database (opened read-only) and both
adapters' execution stores - and writes ``var/two-agent/<label>/result.json``
with structural facts and a verdict. The code word, prompts and replies are
compared in memory and never written; only the code word's SHA-256 is.

What it shows when it passes: one Codex call answered with the code word, the
user handed that reply to Claude Code in an approved call whose input carried
it only inside the handoff frame, and Claude Code answered with the same code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for sub in ("packages/contracts", "packages/adapters", "apps/worker"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

from agenthub_adapters.appserver_adapter import PINNED_APPSERVER  # noqa: E402
from agenthub_adapters.claude_code import PINNED_CLAUDE_CODE  # noqa: E402

RUN_ROOT = REPO / "var" / "two-agent"
CODEX_BINDING = (f"codex-app-server@{PINNED_APPSERVER['version']};sha256={PINNED_APPSERVER['sha256']};"
                 f"size={PINNED_APPSERVER['size']}")
CLAUDE_BINDING = (f"claude-code@{PINNED_CLAUDE_CODE['version']};sha256={PINNED_CLAUDE_CODE['sha256']};"
                  f"size={PINNED_CLAUDE_CODE['size']}")
#: The fixed frame of conversations.handoff_block, rebuilt here to check the sent input.
FRAME = "--- Handoff: reply of {agent} (Hub message {mid}) ---\n{text}\n--- End of handoff ---"


def _rows(db: Path, sql: str) -> list[dict]:
    if not db.is_file():
        return []
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


def _empty_dir(path: Any) -> bool:
    return isinstance(path, str) and Path(path).is_dir() and not any(Path(path).iterdir())


def collect(data_dir: Path, *, code: str, codex_binding: str = CODEX_BINDING,
            claude_binding: str = CLAUDE_BINDING) -> dict:
    hub = data_dir / "agent-hub.db"
    convs = _rows(hub, "select id from conversations")
    invs = _rows(hub, "select id, status, snapshot from invocations")
    approvals = {r["invocation_id"]: r["status"] for r in _rows(hub, "select invocation_id, status from approvals")}
    msgs = _rows(hub, "select id, role, text, invocation_id, conversation_id from chat_messages order by id")
    cards = _rows(hub, "select invocation_id, status, decision_sent from tool_approvals")
    held = (_rows(hub, "select count(*) as n from quota_reservations where released_at is null") or [{"n": 0}])[0]["n"]
    codex_records = {r["invocation_id"]: _json(r["payload"]) or {}
                     for r in _rows(data_dir / "appserver-executions.db",
                                    "select invocation_id, payload from appserver_executions")}
    claude_records = {r["invocation_id"]: _json(r["payload"]) or {}
                      for r in _rows(data_dir / "claude-code-executions.db",
                                     "select invocation_id, payload from claude_code_executions")}

    reply = {m["invocation_id"]: m for m in msgs if m["role"] == "assistant"}
    user = {m["invocation_id"]: m for m in msgs if m["role"] == "user"}
    # Order calls by their user messages (autoincrement ids); creation times can tie.
    rank = {m["invocation_id"]: m["id"] for m in msgs if m["role"] == "user"}
    invs.sort(key=lambda i: (rank.get(i["id"], float("inf")), i["id"]))
    snaps = [_json(i["snapshot"]) or {} for i in invs]
    first, second = (invs + [{}, {}])[:2]
    s1, s2 = (snaps + [{}, {}])[:2]
    codex_reply = reply.get(first.get("id")) or {}
    claude_reply = reply.get(second.get("id")) or {}
    user_text = (user.get(second.get("id")) or {}).get("text", "")
    c1 = codex_records.get(first.get("id"), {})
    c2 = claude_records.get(second.get("id"), {})
    handoff = s2.get("handoff") or []
    source_text = codex_reply.get("text", "")
    expected_input = "\n\n".join([user_text, FRAME.format(agent="codex", mid=codex_reply.get("id"),
                                                           text=source_text)]) if codex_reply else None
    descendants = c1.get("descendants")

    facts = {
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "conversations": len(convs), "invocations": len(invs),
        "agents": [s.get("agent_id") for s in snaps],
        "modes": [s.get("execution_mode", "strict") for s in snaps],
        "statuses": [i.get("status") for i in invs],
        "approvals": [approvals.get(i.get("id")) for i in invs],
        "held_slots": held,
        "same_conversation": bool(codex_reply) and bool(user.get(second.get("id")))
        and codex_reply.get("conversation_id") == user[second["id"]]["conversation_id"],
        # Codex call
        "codex_binding_matches": s1.get("adapter_version") == codex_binding,
        "codex_reply_exact": source_text.strip() == code,
        "codex_handoff_none": not s1.get("handoff"),
        "codex_turn_status": c1.get("turn_status"), "codex_terminal": c1.get("terminal"),
        "codex_usage_present": bool(c1.get("usage")),
        "codex_tool_activity": len(c1.get("tool_activity") or []),
        "codex_tool_cards": sum(1 for c in cards if c["invocation_id"] == first.get("id")),
        "accept_sent": any(c["decision_sent"] == "accept" for c in cards) or bool(c1.get("accept_suppressed")),
        "codex_native_agents": len(c1.get("native_agents") or []),
        "codex_event_anomalies": (c1.get("foreign_events") or 0) + (c1.get("late_events") or 0)
        + (c1.get("usage_anomalies") or 0),
        "codex_exit_confirmed": c1.get("local_process_exit_confirmed") is True,
        # Checked (both lists recorded) and nothing left after the grace period.
        "codex_descendants_clean": isinstance(descendants, dict) and isinstance(descendants.get("before_close"), list)
        and descendants.get("after_grace") == [],
        "codex_workdir_empty": _empty_dir(c1.get("workdir")),
        # Claude Code call and the handoff
        "claude_binding_matches": s2.get("adapter_version") == claude_binding,
        "handoff_records": len(handoff),
        "handoff_points_at_codex_reply": len(handoff) == 1 and handoff[0].get("message_id") == codex_reply.get("id")
        and handoff[0].get("agent_id") == "codex"
        and handoff[0].get("sha256") == hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "sent_input_is_text_plus_frame": expected_input is not None and s2.get("task_input") == expected_input,
        "code_in_user_text": bool(code) and code in user_text,
        "claude_reply_exact": claude_reply.get("text", "").strip() == code,
        "claude_reply_contains": code in claude_reply.get("text", ""),
        "claude_usage_present": bool(c2.get("usage")),
        "claude_num_turns": c2.get("num_turns"),
        "claude_permission_denials": c2.get("permission_denials_count"),
        "claude_exit_code": c2.get("exit_code"), "claude_timed_out": c2.get("timed_out"),
        "claude_workdir_empty": _empty_dir(c2.get("workdir")),
    }
    fail, inconclusive = judge(facts)
    facts["reason_codes"] = fail + inconclusive
    facts["verdict"] = "FAIL" if fail else ("INCONCLUSIVE" if inconclusive else "PASS")
    return facts


def judge(f: dict) -> tuple[list, list]:
    """(FAIL-class, INCONCLUSIVE-class). FAIL = a boundary was broken; INCONCLUSIVE = not shown."""
    fail, inconclusive = [], []
    if f["invocations"] > 2 or f["conversations"] > 2:
        fail.append("budget_exceeded")
    if any(m != "supervised" for m in f["modes"]):
        fail.append("not_supervised")
    if f["invocations"] >= 1 and not f["codex_binding_matches"]:
        fail.append("codex_binding_mismatch")
    if f["invocations"] >= 2 and not f["claude_binding_matches"]:
        fail.append("claude_binding_mismatch")
    if f["accept_sent"]:
        fail.append("accept_path_reached")
    if f["invocations"] >= 1 and not f["codex_workdir_empty"]:
        fail.append("codex_execution_evidence")
    if f["invocations"] >= 2 and not f["claude_workdir_empty"]:
        fail.append("claude_execution_evidence")
    if f["handoff_records"] and not f["handoff_points_at_codex_reply"]:
        fail.append("handoff_source_mismatch")
    if f["handoff_records"] and not f["sent_input_is_text_plus_frame"]:
        fail.append("handoff_content_mismatch")
    if f["code_in_user_text"]:
        fail.append("code_in_user_text")  # the handoff would prove nothing
    if f["invocations"] < 2 or f["agents"][:2] != ["codex", "claude-code"]:
        inconclusive.append("calls_not_as_planned")
    if f["conversations"] < 2:
        inconclusive.append("no_second_conversation")
    if not f["same_conversation"]:
        inconclusive.append("handoff_across_conversations")
    if f["handoff_records"] == 0:
        inconclusive.append("no_handoff")
    if not f["codex_handoff_none"]:
        inconclusive.append("codex_call_had_a_handoff")
    if any(s != "succeeded" for s in f["statuses"]):
        inconclusive.append("call_not_succeeded")
    if any(a != "consumed" for a in f["approvals"]):
        inconclusive.append("approval_not_consumed")
    if f["held_slots"]:
        inconclusive.append("quota_slot_held")
    if not f["codex_reply_exact"]:
        inconclusive.append("codex_reply_not_exact")
    if f["codex_turn_status"] != "completed" or f["codex_terminal"] is not True:
        inconclusive.append("codex_turn_not_completed")
    if f["codex_tool_activity"] or f["codex_tool_cards"]:
        inconclusive.append("codex_tool_activity")
    if f["codex_native_agents"]:
        inconclusive.append("codex_native_agent")
    if f["codex_event_anomalies"] or not f["codex_usage_present"]:
        inconclusive.append("codex_event_or_usage_anomaly")
    if not f["codex_exit_confirmed"] or not f["codex_descendants_clean"]:
        inconclusive.append("codex_cleanup_unconfirmed")
    if not f["claude_reply_exact"]:
        inconclusive.append("claude_reply_not_the_code")
    if f["claude_num_turns"] != 1 or f["claude_permission_denials"] not in (0,) \
            or f["claude_exit_code"] != 0 or f["claude_timed_out"]:
        inconclusive.append("claude_run_anomaly")
    if not f["claude_usage_present"]:
        inconclusive.append("claude_usage_missing")
    return fail, inconclusive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--code", required=True, help="the run's code word (only its SHA-256 is written)")
    parser.add_argument("--label", default="real")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    facts = collect(data_dir, code=args.code)
    facts["data_dir"] = str(data_dir)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"{args.label}-", dir=RUN_ROOT)) / "result.json"
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TWO-AGENT {facts['verdict']} {facts['reason_codes']}")
    print(f"Evidence: {out}")
    return 0 if facts["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
