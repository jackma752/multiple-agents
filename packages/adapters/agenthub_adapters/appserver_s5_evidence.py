"""S5 resume evidence: plant a random code word, cold-resume in a new process, ask for it back.

``thread/resume`` succeeding proves nothing on its own. The judgment needs:

* the resumed thread is the planted one, and the resume response carries the
  planted turn (completed) with the planted user message in it;
* the recall turn, a NEW turn on that thread in a NEW app-server process
  started only after the first one exited, completes and its reply contains
  the code word, which appears nowhere in the recall prompt.

The code word is random and our own; it is compared in memory only. Nothing the
model writes is stored: only counts and booleans (and the code word's SHA-256).
"""
from collections import Counter
import hashlib
import json
import secrets

from .appserver_s2_evidence import S2Evidence

ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U
PLANT_PROMPT = ("This is a memory test. Remember this code word for later in this conversation: {code}. "
                "Do not use any tools, commands, files or subagents. Reply with exactly: STORED")
RECALL_PROMPT = ("What is the code word I asked you to remember earlier in this conversation? "
                 "Do not use any tools, commands, files or subagents. Reply with only the code word.")
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
PLAIN_ITEM_TYPES = ("userMessage", "reasoning", "agentMessage")
REASONING_DELTAS = ("item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
                    "item/reasoning/textDelta")


def make_code_word(choice=secrets.choice):
    """``S5-`` plus 10 symbols: about 49 bits, not guessable without the history."""
    return "S5-" + "".join(choice(ALPHABET) for _ in range(10))


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inspect_resume(result, *, thread_id, planted_turn_id, code_word):
    """Structural facts of a ``thread/resume`` response; never its text."""
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    turns = [t for t in thread.get("turns") or [] if isinstance(t, dict)]
    planted = next((t for t in turns if t.get("id") == planted_turn_id), None)
    items = [i for i in (planted or {}).get("items") or [] if isinstance(i, dict)]
    message = any(i.get("type") == "userMessage" and code_word in json.dumps(i, ensure_ascii=False) for i in items)
    return {"rejected": False, "thread_id_matches": thread.get("id") == thread_id,
            "turns_returned": len(turns), "planted_turn_present": planted is not None,
            "planted_turn_status": planted.get("status") if planted else None,
            "planted_message_present": message,
            "history_returned": bool(planted and planted.get("status") == "completed" and message),
            "rollout_path_reported": bool(isinstance(thread.get("path"), str) and thread.get("path"))}


class TurnEvidence(S2Evidence):
    """One turn on a known thread. The final reply is kept in memory only."""
    MAX_EVENTS = 4096

    def __init__(self, thread_id, *, phase):
        super().__init__(thread_id, expected_reply="")
        self.phase = phase
        self.audit = []
        self.reasons = []        # FAIL-class
        self.notes = []          # INCONCLUSIVE-class
        self.turn_events = []
        self.seen_turn_ids = set()
        self.messages = {}
        self.reasoning_deltas = 0
        self.tools = {}
        self.tool_output_events = 0
        self.server_requests = []
        self.terminal_index = None
        self.usage = None
        self.settings_verified = False
        self.stop_requested = False
        self._final_text = None  # never serialized

    def _violate(self, code):
        self.violation = True
        if code not in self.reasons:
            self.reasons.append(code)

    def _note(self, code):
        if code not in self.notes:
            self.notes.append(code)

    @property
    def terminal_seen(self):
        return self.terminal_index is not None

    def _bind(self, method, params):
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id") if method in ("turn/started", "turn/completed") else params.get("turnId")
        if not params.get("threadId") or not turn_id:
            self._violate("unbound_event")
            return False
        if params["threadId"] != self.thread_id:
            self._violate("foreign_thread")
            return False
        if self.turn_id and turn_id != self.turn_id:
            self._violate("foreign_turn")
            return False
        self.seen_turn_ids.add(turn_id)
        return True

    def _tool(self, label):
        self._note("tool_activity_observed")
        self.stop_requested = True  # stop at once: a tool could read the history from disk

    def observe(self, method, params):
        if not isinstance(method, str) or not isinstance(params, dict):
            self._violate("malformed_notification")
            return
        if len(self.audit) >= self.MAX_EVENTS:
            self._violate("event_limit")
            return
        if not (method.startswith(("item/", "turn/")) or method in ("thread/tokenUsage/updated", "error")):
            self.audit.append({"kind": "notification", "method": method})
            return
        if method == "error":
            self.audit.append({"kind": "notification", "method": method})
            self._note("provider_error")
            return
        if not self._bind(method, params):
            self.audit.append({"kind": "notification", "method": method})
            return
        if self.terminal_seen and method.startswith(("item/", "turn/")):
            self.audit.append({"kind": "notification", "method": method, "after_terminal": True})
            self._violate("duplicate_terminal" if method == "turn/completed" else "event_after_terminal")
            return
        if method in REASONING_DELTAS:
            self.reasoning_deltas += 1
            return
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta")
            if not isinstance(item_id, str) or not isinstance(delta, str):
                self._violate("malformed_delta")
                return
            message = self.messages.setdefault(item_id, {"deltas": 0, "chars": 0, "completed": False})
            message["deltas"] += 1
            message["chars"] += len(delta)
            return
        self.audit.append({"kind": "notification", "method": method})
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") if isinstance(usage, dict) else None
            required = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens", "totalTokens")
            if isinstance(last, dict) and all(type(last.get(k)) is int and last[k] >= 0 for k in required):
                self.usage = {k: last[k] for k in required}
            else:
                self._note("malformed_usage")
            return
        if method in ("turn/started", "turn/completed"):
            turn = params.get("turn") or {}
            event = {"method": method, "turn_id": turn.get("id"), "status": turn.get("status")}
            self.turn_events.append(event)
            self.audit[-1]["status"] = event["status"]
            if method == "turn/completed":
                self.terminal_index = len(self.audit) - 1
            return
        if method.startswith("turn/"):
            self._note(f"unexpected_notification:{method}")
            return
        if method not in ("item/started", "item/completed"):
            self.tool_output_events += 1
            self._tool(method)
            return
        item = params.get("item")
        if not isinstance(item, dict):
            self._violate("malformed_item")
            return
        kind, item_id = item.get("type"), item.get("id")
        self.audit[-1].update(item_type=kind, item_id=item_id)
        if kind == "agentMessage" and method == "item/completed":
            self.messages.setdefault(item_id, {"deltas": 0, "chars": 0, "completed": False})["completed"] = True
            self._final_text = item.get("text") if isinstance(item.get("text"), str) else None
        if kind not in PLAIN_ITEM_TYPES:
            self.tools.setdefault(item_id, {"type": kind if isinstance(kind, str) else "?",
                                            "ended": method == "item/completed"})
            self._tool(kind)

    def record_server_request(self, method):
        label = method[:80] if isinstance(method, str) else "?"
        self.audit.append({"kind": "server_request", "method": label})
        self.server_requests.append(label)
        self.stop_requested = True

    def recall(self, code_word):
        """In memory: (reply contains the code word, reply is exactly the code word)."""
        if not isinstance(self._final_text, str):
            return None, None
        return code_word in self._final_text, self._final_text.strip() == code_word

    def summary(self):
        violation = self.violation
        reasons = list(self.reasons)
        if self.turn_id and any(t != self.turn_id for t in self.seen_turn_ids):
            violation = True
            if "foreign_turn" not in reasons:
                reasons.append("foreign_turn")
        starts = [e for e in self.turn_events if e["method"] == "turn/started"]
        ends = [e for e in self.turn_events if e["method"] == "turn/completed"]
        status = ends[0]["status"] if len(ends) == 1 else None
        matched = bool(self.turn_id and len(ends) == 1 and ends[0]["turn_id"] == self.turn_id)
        terminal_confirmed = bool(matched and not violation and status in TERMINAL_STATUSES)
        tools_seen = bool(self.tools or self.tool_output_events or self.server_requests)
        return {"phase": self.phase, "thread_id": self.thread_id, "turn_id": self.turn_id,
                "matched": matched, "status": status, "terminal_confirmed": terminal_confirmed,
                "one_turn_started": len(starts) == 1 and starts[0]["status"] == "inProgress",
                "usage_received": self.usage is not None, "usage": self.usage,
                "settings_verified": self.settings_verified,
                "tool_activity": {"seen": tools_seen, "server_requests": list(self.server_requests),
                                  "server_requests_answered": 0, "output_notifications": self.tool_output_events,
                                  "items": list(self.tools.values())},
                "text_format": {"decides_verdict": False, "assistant_messages": len(self.messages),
                                "completed_messages": sum(m["completed"] for m in self.messages.values()),
                                "delta_count": sum(m["deltas"] for m in self.messages.values()),
                                "delta_chars": sum(m["chars"] for m in self.messages.values()),
                                "reasoning_deltas": self.reasoning_deltas},
                "violation": violation, "reasons": reasons, "notes": list(self.notes),
                "audit": self.audit,
                "event_counts": dict(Counter(e["method"] for e in self.audit if "method" in e))}


def judge(result):
    """PASS / FAIL / INCONCLUSIVE with reason codes, from both phases and the process relation."""
    plant = result.get("phases", {}).get("plant") or {}
    recall = result.get("phases", {}).get("recall") or {}
    relation = result.get("process_relation") or {}
    resume = recall.get("resume") or {}
    fail, codes = [], []

    for name, phase in (("plant", plant), ("recall", recall)):
        evidence = phase.get("evidence") or {}
        fail += [f"{name}:{c}" for c in evidence.get("reasons", [])]
        fail += [f"{name}:{c}" for c in phase.get("client_reasons", [])
                 if c.startswith(("outgoing_", "server_request_before")) or c in ("resume_thread_mismatch",)]
    if plant.get("turn_id") and recall.get("turn_id") == plant.get("turn_id"):
        fail.append("recall_turn_reused")
    if result.get("workdir_empty") is False:
        fail.append("workdir_not_empty")

    def phase_codes(name, phase, *, need_usage):
        out = []
        if not phase:
            return [f"{name}:not_run"]
        evidence = phase.get("evidence") or {}
        out += [f"{name}:{c}" for c in phase.get("client_reasons", []) if f"{name}:{c}" not in fail]
        if phase.get("error_type"):
            out.append(f"{name}:" + {"AppServerTimeout": "timeout", "AppServerDisconnected": "disconnect",
                                      "CheckFailed": "preflight_failed",
                                      "AppServerResponseError": "request_rejected"}.get(phase["error_type"],
                                                                                        f"error:{phase['error_type']}"))
        if phase.get("reader_error_type") and not phase.get("error_type"):
            out.append(f"{name}:reader_error:{phase['reader_error_type']}")
        if not evidence.get("settings_verified"):
            out.append(f"{name}:settings_not_verified")
        if not evidence.get("matched"):
            out.append(f"{name}:terminal_not_matched")
        elif evidence.get("status") != "completed":
            out.append(f"{name}:turn_status:{evidence.get('status')}")
        if evidence and not evidence.get("one_turn_started"):
            out.append(f"{name}:turn_not_started_once")
        if (evidence.get("tool_activity") or {}).get("seen"):
            out.append(f"{name}:tool_activity_observed")
        out += [f"{name}:server_request_unanswered:{m}"
                for m in dict.fromkeys((evidence.get("tool_activity") or {}).get("server_requests", []))]
        out += [f"{name}:{c}" for c in evidence.get("notes", []) if c != "tool_activity_observed"]
        if need_usage and evidence and not evidence.get("usage_received"):
            out.append(f"{name}:usage_missing")
        if not phase.get("process_stopped"):
            out.append(f"{name}:process_not_stopped")
        elif phase.get("exit_code") != 0:
            out.append(f"{name}:process_exit:{phase.get('exit_code')}")
        tree = phase.get("process_tree") or {}
        if not tree.get("available"):
            out.append(f"{name}:process_tree_unavailable")
        elif tree.get("descendants_after_close"):
            out.append(f"{name}:descendants_survived_close")
        if phase.get("turn_attempts") != 1:
            out.append(f"{name}:turn_attempts:{phase.get('turn_attempts')}")
        return out

    codes += phase_codes("plant", plant, need_usage=False)
    if plant and not relation.get("first_process_exited_before_second"):
        codes.append("first_process_alive_before_resume" if relation else "resume_not_attempted")
    if relation.get("first_process_exited_before_second") and relation.get("same_pid"):
        codes.append("process_identity_ambiguous")
    codes += phase_codes("recall", recall, need_usage=True)
    if recall:
        if resume.get("rejected"):
            codes.append("resume_rejected")
        elif not resume.get("thread_id_matches"):
            codes.append("resume_thread_mismatch")
        elif not resume.get("history_returned"):
            codes.append("history_not_returned" if not resume.get("planted_turn_present")
                         else "planted_message_missing" if not resume.get("planted_message_present")
                         else f"planted_turn_status:{resume.get('planted_turn_status')}")
        contains = (recall.get("recall") or {}).get("contains")
        if (recall.get("evidence") or {}).get("status") == "completed" and contains is not True:
            codes.append("context_not_recalled")
    if result.get("workdir_empty") is None:
        codes.append("workdir_unreadable")
    total = sum(p.get("turn_attempts", 0) or 0 for p in (plant, recall))
    if total > 2:
        fail.append(f"turn_attempts_total:{total}")

    fail = list(dict.fromkeys(fail))
    codes = [c for c in dict.fromkeys(codes) if c not in fail]
    if fail:
        return "FAIL", fail + codes
    if not codes and recall and (recall.get("recall") or {}).get("contains") is True:
        return "PASS", []
    return "INCONCLUSIVE", codes or ["unclassified"]
