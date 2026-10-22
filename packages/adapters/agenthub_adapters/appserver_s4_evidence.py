"""S4 interrupt evidence. No model text, command text or output is retained.

The verdict is built from separate parts, never from what the model says:

* ``interrupt`` - whether our single ``turn/interrupt`` was sent, what
  triggered it, and the server's answer (``{}`` or an error code). The tested
  build answers only when the turn is actually aborted, but an answer alone
  never settles anything.
* ``main_turn`` - this turn's ``turn/completed``, matched by thread and turn
  id, its status, and whether it came after the interrupt.
* ``tool_activity`` - any tool item, tool output or server request. The fixed
  scope has none; when one appears its end state is reported as ``none``,
  ``ended`` or ``unknown`` and the main turn's status is not taken as proof that
  it stopped (the tested build emits no completion for in-progress items on
  abort).
* ``text_format`` - message and delta counts only; it never decides.

Local cleanup (own process, work directory, descendant processes) is judged by
the probe runner, separately.

Verdicts: **FAIL** on a protocol contradiction (foreign or unbound ids, a
second terminal, item/turn events after the terminal, an outgoing message the
probe refused). **PASS** only when exactly one interrupt was sent while the
turn was running, it was answered with ``{}``, the matched terminal is
``interrupted`` and no tool activity was seen. Everything else is
**INCONCLUSIVE**, with reason codes.
"""
from collections import Counter

from .appserver_s2_evidence import S2Evidence

INTERRUPT = "turn/interrupt"
TERMINAL_STATUSES = ("completed", "failed", "interrupted")
PLAIN_ITEM_TYPES = ("userMessage", "reasoning", "agentMessage")
REASONING_DELTAS = ("item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
                    "item/reasoning/textDelta")
#: Trusted end states for tool-like items: a plain ``item/completed`` or the
#: native-child end states of the interrupt-stop contract.
ITEM_END_STATES = ("completed", "failed", "declined", "interrupted", "errored", "shutdown")


class S4Evidence(S2Evidence):
    MAX_EVENTS = 4096

    def __init__(self, thread_id):
        super().__init__(thread_id, expected_reply="")
        self.audit = []               # structural events: methods, ids, statuses only
        self.reasons = []             # FAIL-class codes, first occurrence order
        self.notes = []               # INCONCLUSIVE-class observations
        self.turn_events = []         # {"method", "turn_id", "status", "index"}
        self.seen_turn_ids = set()
        self.messages = {}            # agentMessage item id -> counts (no text)
        self.reasoning_deltas = 0
        self.tools = {}               # tool item id -> {"type", "process_seen", "end"}
        self.tool_output_events = 0
        self.server_requests = []     # method names, never answered
        self.interrupt = {"sent": False, "trigger": None, "audit_index": None,
                          "response": None, "response_code": None, "response_after_terminal": None}
        self.terminal_index = None
        self.usage = None
        self.settings_verified = False
        self.first_delta_seen = False
        self.process_tree = None      # filled in by the probe (local cleanup)

    # -- helpers -------------------------------------------------------------

    def _violate(self, code):
        self.violation = True
        if code not in self.reasons:
            self.reasons.append(code)

    def _note(self, code):
        if code not in self.notes:
            self.notes.append(code)

    def _bind(self, method, params):
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = turn.get("id") if method in ("turn/started", "turn/completed") else params.get("turnId")
        thread_id = params.get("threadId")
        if not thread_id or not turn_id:
            self._violate("unbound_event")
            return False
        if thread_id != self.thread_id:
            self._violate("foreign_thread")
            return False
        if self.turn_id and turn_id != self.turn_id:
            self._violate("foreign_turn")
            return False
        self.seen_turn_ids.add(turn_id)
        return True

    @property
    def terminal_seen(self):
        return self.terminal_index is not None

    @property
    def turn_started(self):
        return any(e["method"] == "turn/started" for e in self.turn_events)

    # -- notifications ---------------------------------------------------------

    def observe(self, method, params):
        if not isinstance(method, str) or not isinstance(params, dict):
            self._violate("malformed_notification")
            return
        if len(self.audit) >= self.MAX_EVENTS:
            self._violate("event_limit")
            return
        bound = method.startswith(("item/", "turn/")) or method in ("thread/tokenUsage/updated", "error")
        if not bound:
            self.audit.append({"kind": "notification", "method": method})  # e.g. status, rate limits
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
            if not isinstance(item_id, str) or not item_id or not isinstance(delta, str):
                self._violate("malformed_delta")
                return
            message = self.messages.setdefault(item_id, {"deltas": 0, "chars": 0, "completed": False})
            message["deltas"] += 1
            message["chars"] += len(delta)
            self.first_delta_seen = True
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
            event = {"method": method, "turn_id": turn.get("id"), "status": turn.get("status"),
                     "index": len(self.audit) - 1}
            self.turn_events.append(event)
            self.audit[-1]["status"] = event["status"]
            if method == "turn/completed":
                self.terminal_index = event["index"]
                self.audit[-1]["after_interrupt"] = self.interrupt["sent"]
            return
        if method.startswith("turn/"):
            self._note(f"unexpected_notification:{method}")
            return
        if method not in ("item/started", "item/completed"):
            # Tool output, terminal interaction, file diffs...: tool activity.
            self.tool_output_events += 1
            self._note(f"tool_notification:{method}")
            return
        item = params.get("item")
        if not isinstance(item, dict):
            self._violate("malformed_item")
            return
        kind, item_id = item.get("type"), item.get("id")
        self.audit[-1].update(item_type=kind, item_id=item_id)
        if kind == "agentMessage" and method == "item/completed":
            self.messages.setdefault(item_id, {"deltas": 0, "chars": 0, "completed": False})["completed"] = True
        if kind in PLAIN_ITEM_TYPES:
            return
        tool = self.tools.setdefault(item_id, {"type": kind if isinstance(kind, str) else "?",
                                               "process_seen": False, "end": None})
        if item.get("processId") is not None:
            tool["process_seen"] = True
        status = item.get("status") or item.get("kind")
        if method == "item/completed":
            tool["end"] = status if status in ITEM_END_STATES else "completed_item"
        self.audit[-1]["tool_status"] = status if isinstance(status, str) else None

    # -- the probe's own actions -----------------------------------------------

    def record_server_request(self, method):
        """A server request is never answered; it only makes the probe interrupt."""
        label = method[:80] if isinstance(method, str) else "?"
        self.audit.append({"kind": "server_request", "method": label})
        self.server_requests.append(label)

    def can_interrupt(self):
        """Only once, only for the bound turn, only after it started and before its terminal."""
        return bool(not self.interrupt["sent"] and not self.violation and self.turn_id
                    and self.turn_started and not self.terminal_seen)

    def interrupt_trigger(self, *, timer_expired=False):
        """Why the probe should interrupt now, or None (never twice, never late)."""
        if not self.can_interrupt():
            return None
        if self.server_requests:
            return "server_request"
        if self.tools or self.tool_output_events:
            return "tool_activity"
        if self.first_delta_seen:
            return "first_delta"
        return "timer" if timer_expired else None

    def mark_interrupt_sent(self, trigger):
        """Recorded BEFORE the write: a partial write still counts as the one attempt."""
        if self.interrupt["sent"]:
            self._violate("duplicate_interrupt_attempt")
            return False
        self.interrupt.update(sent=True, trigger=trigger, audit_index=len(self.audit))
        self.audit.append({"kind": "interrupt_sent", "trigger": trigger})
        return True

    def record_interrupt_response(self, *, error_code=None):
        if self.interrupt["response"] is not None:
            self._violate("duplicate_interrupt_response")
            return
        self.interrupt["response"] = "error" if error_code is not None else "ok"
        self.interrupt["response_code"] = error_code if isinstance(error_code, (int, str)) else None
        self.interrupt["response_after_terminal"] = self.terminal_seen
        self.audit.append({"kind": "interrupt_response", "result": self.interrupt["response"],
                           "code": self.interrupt["response_code"]})

    # -- verdict -------------------------------------------------------------

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
        sent_before_terminal = bool(self.interrupt["sent"] and (
            self.terminal_index is None or self.interrupt["audit_index"] < self.terminal_index))
        main_turn = {"matched": matched, "status": status, "terminal_confirmed": terminal_confirmed,
                     "one_turn_started": len(starts) == 1 and starts[0]["status"] == "inProgress",
                     "terminal_after_interrupt": bool(matched and sent_before_terminal),
                     "usage_received": self.usage is not None}

        unfinished = [t for t in self.tools.values() if t["end"] is None]
        state = ("none" if not (self.tools or self.tool_output_events) else
                 "unknown" if unfinished or (self.tool_output_events and not self.tools) else "ended")
        tool_activity = {"state": state, "server_requests": list(self.server_requests),
                         "server_requests_answered": 0, "output_notifications": self.tool_output_events,
                         "items": [{"type": t["type"], "process_seen": t["process_seen"], "end": t["end"]}
                                   for t in self.tools.values()]}
        text_format = {"decides_verdict": False, "assistant_messages": len(self.messages),
                       "completed_messages": sum(m["completed"] for m in self.messages.values()),
                       "delta_count": sum(m["deltas"] for m in self.messages.values()),
                       "delta_chars": sum(m["chars"] for m in self.messages.values()),
                       "reasoning_deltas": self.reasoning_deltas}

        checks = {"effective_settings_verified": self.settings_verified,
                  "one_turn_started": main_turn["one_turn_started"],
                  "one_interrupt_sent_while_running": sent_before_terminal,
                  "interrupt_answered_ok": self.interrupt["response"] == "ok",
                  "matched_terminal_interrupted": bool(terminal_confirmed and status == "interrupted"),
                  "terminal_after_interrupt": main_turn["terminal_after_interrupt"],
                  "no_tool_activity": state == "none" and not self.server_requests,
                  "no_violation": not violation}
        if violation:
            verdict = "FAIL"
        elif all(checks.values()) and not self.notes:
            verdict = "PASS"
        else:
            verdict = "INCONCLUSIVE"
            if not self.settings_verified:
                reasons.append("settings_not_verified")
            if not starts:
                reasons.append("turn_not_started")
            if not self.interrupt["sent"]:
                reasons.append(f"turn_completed_before_interrupt:{status}" if matched else "interrupt_not_sent")
            elif not sent_before_terminal:
                reasons.append("interrupt_after_terminal")
            if self.interrupt["sent"]:
                if self.interrupt["response"] is None:
                    reasons.append("interrupt_not_answered")
                elif self.interrupt["response"] == "error":
                    reasons.append("interrupt_rejected")
            if self.interrupt["sent"] and not matched:
                reasons.append("terminal_not_matched")
            elif self.interrupt["sent"] and sent_before_terminal and status != "interrupted":
                reasons.append(f"interrupt_not_effective:{status}")
            if state == "unknown":
                reasons.append("tool_process_unknown")
            elif state == "ended":
                reasons.append("tool_activity_observed")
            reasons.extend(f"server_request_unanswered:{m}" for m in dict.fromkeys(self.server_requests))
            reasons.extend(self.notes)
        reasons = list(dict.fromkeys(reasons))
        legacy = {"completed": "finished", "failed": "failed",
                  "interrupted": "cancelled"}.get(status, "unconfirmed") if terminal_confirmed else "unconfirmed"
        return {"passed": verdict == "PASS", "probe_verdict": verdict, "reason_codes": reasons,
                "interrupt": {k: v for k, v in self.interrupt.items() if k != "audit_index"},
                "main_turn": main_turn, "tool_activity": tool_activity, "text_format": text_format,
                "process_tree": self.process_tree, "checks": checks,
                "terminal_confirmed": terminal_confirmed, "verdict": legacy,
                "thread_id": self.thread_id, "turn_id": self.turn_id, "usage": self.usage,
                "violation": violation, "audit": self.audit,
                "event_sequence": [e["method"] for e in self.audit if "method" in e],
                "event_counts": dict(Counter(e["method"] for e in self.audit if "method" in e))}
