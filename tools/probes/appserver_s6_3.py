"""S6-3: native resume through the Hub - two calls, at most two model turns. Offline by default.

Everything goes through the product path of S6-2 (in-process API -> approval ->
Worker -> app-server adapter); nothing calls the adapter directly.

1. A new conversation. Call 1 (independent, a NEW thread) plants a random code
   word and must complete normally; its own approval carries the supervised
   consent. Its app-server process must be gone from the process table before
   call 2 is submitted.
2. Call 2 (``context_mode=native_resume``, bound by the kernel to call 1's
   thread, in call 1's work directory) asks for the code word; it has its own
   approval and consent. Its input is the fixed recall prompt: the code word and
   the planting text never appear in it, which the adapter's record of the
   input it actually sent (a SHA-256) confirms.

PASS needs an actual ``thread/resume`` of the planted thread whose answer
contains the planted turn, a new turn id, the code word in the reply, both
calls settled with their slots released and local processes gone. A resume
that works but a reply without the code word is INCONCLUSIVE; tool or native
child activity stops the check (INCONCLUSIVE, never a recall success). The one
legitimate usage replay of the planted turn is recorded apart. Only the code
word's SHA-256 and the match booleans are written; no retry, no third call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import appserver_s6_2 as S62  # noqa: E402  (shared path setup, environment, preflight)

REPO = S62.REPO
RUN_ROOT = REPO / "var" / "appserver-s6-3"
WORK_ROOT = S62.WORK_ROOT
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # as S5: no 0/O, 1/I/L, U
#: The S5 prompts (the verified shape), with an S6 code word.
PLANT_PROMPT = ("This is a memory test. Remember this code word for later in this conversation: {code}. "
                "Do not use any tools, commands, files or subagents. Reply with exactly: STORED")
RECALL_PROMPT = ("What is the code word I asked you to remember earlier in this conversation? "
                 "Do not use any tools, commands, files or subagents. Reply with only the code word.")
PLANT_REPLY = "STORED"
MAX_MODEL_TURNS = 2
#: How long call 1's app-server process may take to leave the process table.
PROCESS_EXIT_WAIT_SECONDS = 10.0
STUB_SCENARIOS = ("recall", "recall-forgetful", "recall-tool", "recall-native", "plant-tool",
                  "recall-same-turn-id", "recall-wrong-thread", "recall-replay-twice")
RECORD_KEYS = ("thread_id", "turn_id", "previous_turn_id", "turn_status", "turn_requested", "usage",
               "replayed_usage", "usage_anomalies", "tool_process_state", "tool_activity", "tool_approvals",
               "declined_requests", "unanswered_requests", "native_agents", "foreign_events", "late_events",
               "resumed", "opening_method", "input_sha256", "resume_check", "turn_id_reused", "process_id",
               "workdir", "local_process_exit_confirmed", "descendants", "verdict", "terminal", "detail")


def make_code_word(choice: Callable = secrets.choice) -> str:
    """``S6-`` plus 10 symbols: about 49 bits, not guessable without the history."""
    return "S6-" + "".join(choice(ALPHABET) for _ in range(10))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def process_alive(pid: int) -> Optional[bool]:
    """True/False from the process table; None when it cannot be read."""
    from agenthub_adapters import proc_tree

    table = proc_tree.process_table()
    if table is None:
        return None
    return any(row[0] == pid for row in table)


def wait_exited(pid: int, timeout: float, alive: Callable[[int], Optional[bool]]) -> Optional[bool]:
    deadline = time.monotonic() + timeout
    while True:
        state = alive(pid)
        if state is False:
            return True
        if state is None:
            return None
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


class Stop(Exception):
    """End the check here; ``verdict`` says how the result is classified."""

    def __init__(self, code: str, verdict: str = "INCONCLUSIVE") -> None:
        super().__init__(code)
        self.code = code
        self.verdict = verdict


def plant_codes(phase: dict) -> list[str]:
    """Call 1 must be a clean, completed planting turn on a new thread."""
    record = phase["record"]
    codes = []
    if phase["status"] != "succeeded":
        codes.append(f"plant:status_{phase['status']}")
    if not phase["reply_is_stored"]:
        codes.append("plant:reply_not_stored")
    if record.get("opening_method") != "thread/start":
        codes.append("plant:not_a_new_thread")
    if record.get("tool_process_state") != "none" or record.get("tool_approvals") \
            or record.get("declined_requests") or record.get("unanswered_requests"):
        codes.append("plant:tool_activity")
    if record.get("native_agents"):
        codes.append("plant:native_agent")
    if record.get("foreign_events") or record.get("late_events") or record.get("usage_anomalies"):
        codes.append("plant:unbound_events")
    if not record.get("usage"):
        codes.append("plant:no_usage")
    if not record.get("local_process_exit_confirmed"):
        codes.append("plant:local_exit_unconfirmed")
    if (record.get("descendants") or {}).get("after_grace"):
        codes.append("plant:descendants_survived")
    if not phase["native_session_recorded"]:
        codes.append("plant:no_native_session")
    if phase["held_slots"]:
        codes.append("plant:quota_slot_held")
    if not isinstance(record.get("process_id"), int):
        codes.append("plant:no_process_id")
    return codes


def recall_codes(plant: dict, recall: dict, *, recall_text: str) -> tuple[list, list]:
    """(FAIL-class, INCONCLUSIVE-class) codes for call 2. Structure first, then the answer."""
    p, r = plant["record"], recall["record"]
    fail, inconclusive = [], []
    if r.get("opening_method") != "thread/resume":
        fail.append("recall:not_resumed")
    if r.get("thread_id") != p.get("thread_id"):
        fail.append("recall:wrong_thread")
    if r.get("turn_id_reused") or (r.get("turn_id") and r.get("turn_id") == p.get("turn_id")):
        fail.append("recall:turn_id_reused")
    if r.get("previous_turn_id") != p.get("turn_id"):
        fail.append("recall:previous_turn_mismatch")
    if not (r.get("resume_check") or {}).get("previous_turn_present"):
        fail.append("recall:history_not_confirmed")
    if r.get("input_sha256") != digest(recall_text) or recall["input_contains_code"]:
        fail.append("recall:input_not_the_recall_prompt")
    if r.get("usage_anomalies") or not r.get("usage") or (
            r.get("replayed_usage") is not None and r.get("usage") == r.get("replayed_usage")):
        fail.append("recall:replay_usage_miscounted")
    if r.get("workdir") != p.get("workdir"):
        fail.append("recall:workdir_changed")
    if recall["status"] == "failed" and not r.get("turn_requested"):
        fail.append("recall:refused_before_turn")
    if r.get("tool_process_state") != "none" or r.get("tool_approvals") \
            or r.get("declined_requests") or r.get("unanswered_requests"):
        inconclusive.append("recall:tool_activity")
    if r.get("native_agents"):
        inconclusive.append("recall:native_agent")
    if recall["status"] not in ("succeeded", "failed"):
        inconclusive.append(f"recall:status_{recall['status']}")
    if not r.get("local_process_exit_confirmed"):
        inconclusive.append("recall:local_exit_unconfirmed")
    if (r.get("descendants") or {}).get("after_grace"):
        inconclusive.append("recall:descendants_survived")
    if recall["held_slots"]:
        inconclusive.append("quota_slot_held")
    if recall["status"] == "succeeded" and not recall["reply_contains_code"]:
        inconclusive.append("recall:answer_missing")
    return fail, inconclusive


def _call(client, headers, cid, *, text: str, key: str, context_mode: str, approve: bool) -> dict:
    """Submit one turn (its own approval), approve it with the boundary consent, run it."""
    submitted = client.post(f"/api/conversations/{cid}/invocations", headers=headers, json={
        "text": text, "agent_id": "codex", "client_request_id": key,
        "execution_mode": "supervised", "context_mode": context_mode})
    if submitted.status_code != 200:
        return {"submitted": False, "submit_status": submitted.status_code,
                "submit_code": (submitted.json().get("detail") or {}).get("code")}
    turn = submitted.json()
    out = {"submitted": True, "invocation_id": turn["invocation_id"], "approval_id": turn["approval_id"],
           "resume_thread_id": turn.get("resume_thread_id"), "context_mode": turn.get("context_mode")}
    if approve:
        consent = client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=headers,
                              json={"accept_unmanaged_native_agents": True})
        out["approve_status"] = consent.status_code
    out["_run"] = lambda: client.post(f"/api/invocations/{turn['invocation_id']}/run",
                                      params={"wait": "true"}, headers=headers)
    return out


def run_probe(*, real: bool = False, approved: bool = False, acknowledged: bool = False,
              scenario: str = "recall", codex_home: str = "", run_root: Path = RUN_ROOT,
              work_root: Path = WORK_ROOT,
              # Offline fault injection for the counterexample tests; not on the CLI.
              _code: Optional[str] = None, _recall_prompt: Optional[str] = None,
              _approve_recall: bool = True, _recall_context: str = "native_resume",
              _alive: Callable[[int], Optional[bool]] = process_alive) -> int:
    if real and not (approved and acknowledged):
        print("REFUSED: S6-3 needs separate approval of two model turns and acknowledgement "
              "of the supervised boundary.")
        return 2  # before any file, credential check or process
    if real and scenario != "recall":
        raise ValueError("real mode has no scenarios")
    if not real and scenario not in STUB_SCENARIOS:
        raise ValueError(f"unknown stub scenario {scenario!r}")
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="real-" if real else f"stub-{scenario}-", dir=run_root))
    data_dir = Path(tempfile.mkdtemp(prefix="agenthub-s6-3-data-", dir=work_root))
    code = _code or make_code_word()
    plant_text = PLANT_PROMPT.format(code=code)
    recall_text = _recall_prompt if _recall_prompt is not None else RECALL_PROMPT
    result: dict[str, Any] = {
        "mode": "real" if real else "stub", "scenario": scenario, "verdict": "INCONCLUSIVE",
        "reason_codes": [], "data_dir": str(data_dir), "code_sha256": digest(code),
        "plant_prompt_sha256": digest(PLANT_PROMPT), "recall_prompt_sha256": digest(recall_text),
        "model_turns": 0, "plant": None, "recall": None, "process_exit": None,
        "recall_contains": None, "recall_exact": None,
    }
    env = {
        "AGENT_HUB_ADAPTER": "codex", "AGENT_HUB_CODEX_TRANSPORT": "app-server",
        "AGENT_HUB_APPSERVER_STUB": None if real else scenario,
        "AGENT_HUB_CODEX_TIMEOUT": str(S62.TIMEOUT_SECONDS),
        "AGENT_HUB_USER_TOKEN": secrets.token_hex(16),  # in-process app only; never stored
    }
    if real and codex_home:
        env["AGENT_HUB_CODEX_HOME"] = codex_home
    from agenthub_kernel import config

    saved_data_dir = config.DATA_DIR
    db = None
    try:
        with S62._environment(env):
            config.DATA_DIR = str(data_dir)  # work dirs, store and DB outside the repository
            from fastapi.testclient import TestClient

            from agenthub_kernel.adapters import adapter_binding, build_adapter
            from agenthub_kernel.db import Database, InvocationRow, QuotaReservationRow
            from agenthub_rt.api import create_app
            from sqlalchemy import func, select

            adapter = build_adapter("codex")
            result["binding_identity"] = adapter_binding(adapter)
            if real:
                try:
                    result["preflight"] = S62.preflight_real(adapter, codex_home=config.codex_home())
                except S62.Refused as exc:
                    raise Stop(f"preflight:{exc}") from exc
            headers = {"Authorization": f"Bearer {os.environ['AGENT_HUB_USER_TOKEN']}"}
            db = Database(f"sqlite:///{(data_dir / 'agent-hub.db').as_posix()}")

            def held() -> int:
                with db.session() as s:
                    return s.scalar(select(func.count()).select_from(QuotaReservationRow)
                                    .where(QuotaReservationRow.released_at.is_(None)))

            def status_of(invocation_id: str) -> str:
                with db.session() as s:
                    return s.get(InvocationRow, invocation_id).status

            def phase(call: dict, cid: str) -> dict:
                """Run an approved call and collect what the Hub stored (reply kept in memory only)."""
                starts = adapter.start_count
                began = time.monotonic()
                ran = call.pop("_run")()
                record = (adapter.store.get(call["invocation_id"]) or {}).get("payload") or {}
                messages = client.get(f"/api/conversations/{cid}/messages", headers=headers).json()["messages"]
                replies = [m for m in messages if m["role"] == "assistant"
                           and (m.get("invocation") or {}).get("invocation_id") == call["invocation_id"]]
                text = replies[0]["text"] if replies else ""
                return {**call, "run_status": ran.status_code, "seconds": round(time.monotonic() - began, 2),
                        "launches": adapter.start_count - starts, "status": status_of(call["invocation_id"]),
                        "held_slots": held(), "record": {k: record.get(k) for k in RECORD_KEYS},
                        "native_session_recorded": bool(replies and ((replies[0].get("result") or {})
                                                                     .get("native_session") or {}).get("thread_id")),
                        "_reply": text}

            app = create_app(db=db, adapter=adapter)
            with TestClient(app) as client:
                cid = client.post("/api/conversations", headers=headers,
                                  json={"title": "S6-3"}).json()["conversation_id"]
                # ---- call 1: plant on a new thread ----
                plant_call = _call(client, headers, cid, text=plant_text, key="s6-3-plant",
                                   context_mode="independent", approve=True)
                if not plant_call.get("submitted") or plant_call.get("approve_status") != 200:
                    raise Stop("plant:not_started")
                plant = phase(plant_call, cid)
                plant["reply_is_stored"] = plant.pop("_reply").strip() == PLANT_REPLY
                result["plant"] = plant
                result["model_turns"] += int(bool(plant["record"].get("turn_requested")))
                codes = plant_codes(plant)
                if codes:
                    result["reason_codes"] = codes
                    raise Stop(codes[0])
                exited = wait_exited(plant["record"]["process_id"], PROCESS_EXIT_WAIT_SECONDS, _alive)
                result["process_exit"] = {"pid": plant["record"]["process_id"], "exited": exited}
                if exited is not True:
                    raise Stop("plant:process_not_exited" if exited is False else "plant:process_exit_unverifiable")
                # ---- call 2: recall on the resumed thread ----
                if code in recall_text or plant_text in recall_text:
                    raise Stop("recall:input_contains_code")  # never sent
                recall_call = _call(client, headers, cid, text=recall_text, key="s6-3-recall",
                                    context_mode=_recall_context, approve=_approve_recall)
                if not recall_call.get("submitted"):
                    raise Stop(f"recall:not_submitted:{recall_call.get('submit_code')}")
                if recall_call.get("resume_thread_id") != plant["record"]["thread_id"]:
                    raise Stop("recall:not_resumed", verdict="FAIL")  # nothing sent: refused before run
                if _approve_recall and recall_call.get("approve_status") != 200:
                    raise Stop("recall:not_approved")
                starts = adapter.start_count
                recall = phase(recall_call, cid)
                if recall["run_status"] != 200:
                    result["recall"] = {k: v for k, v in recall.items() if k != "_reply"}
                    raise Stop("recall:not_approved" if not _approve_recall else "recall:not_started")
                reply = recall.pop("_reply")
                recall["reply_contains_code"] = code in reply
                recall["reply_exact"] = reply.strip() == code
                recall["input_contains_code"] = code in recall_text
                result["recall"] = recall
                result["model_turns"] += int(bool(recall["record"].get("turn_requested")))
                result["recall_contains"], result["recall_exact"] = recall["reply_contains_code"], recall["reply_exact"]
                fail, inconclusive = recall_codes(plant, recall, recall_text=recall_text)
                workdir = plant["record"].get("workdir")
                if not (workdir and Path(workdir).is_dir() and not any(Path(workdir).iterdir())):
                    inconclusive.append("workdir_not_empty")
                if result["model_turns"] > MAX_MODEL_TURNS or adapter.start_count - starts > 1:
                    fail.append("turn_budget_exceeded")
                result["reason_codes"] = fail + inconclusive
                result["verdict"] = "FAIL" if fail else ("INCONCLUSIVE" if inconclusive else "PASS")
    except Stop as stop:
        result["verdict"] = stop.verdict
        if stop.code not in result["reason_codes"]:
            result["reason_codes"].append(stop.code)
    finally:
        config.DATA_DIR = saved_data_dir
        if db is not None:
            db.engine.dispose()
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"S6-3 {result['verdict']} {result['reason_codes']}")
    print(f"Evidence: {run_dir / 'result.json'}")
    return 0 if result["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=STUB_SCENARIOS, default="recall")
    parser.add_argument("--approve-two-model-turns", action="store_true")
    parser.add_argument("--acknowledge-supervised-boundary", action="store_true")
    parser.add_argument("--codex-home", default="")
    args = parser.parse_args()
    return run_probe(real=args.mode == "real", approved=args.approve_two_model_turns,
                     acknowledged=args.acknowledge_supervised_boundary, scenario=args.scenario,
                     codex_home=os.path.abspath(args.codex_home) if args.codex_home else "")


if __name__ == "__main__":
    raise SystemExit(main())
