"""Content-minimized evidence for the fixed S2 probe, not a production adapter."""
from collections import Counter


class S2Evidence:
    MAX_EVENTS = 512

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.turn_id = ""
        self.events = []
        self.violation = False

    def bind_turn(self, turn_id: str):
        if not isinstance(turn_id, str) or not turn_id or self.turn_id:
            self.violation = True
        else:
            self.turn_id = turn_id

    def observe(self, method: str, params: dict):
        if len(self.events) >= self.MAX_EVENTS or not isinstance(params, dict) or not isinstance(method, str):
            self.violation = True
            return
        if method.startswith("item/") and method not in (
            "item/started", "item/completed", "item/agentMessage/delta",
            "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded", "item/reasoning/textDelta",
        ):
            self.violation = True
        if method.startswith("turn/") and method not in ("turn/started", "turn/completed"):
            self.violation = True
        event = {"method": method}
        relevant = method.startswith(("turn/", "item/")) or method in (
            "thread/tokenUsage/updated", "error")
        if relevant:
            turn = params.get("turn") or {}
            event.update(thread_id=params.get("threadId"),
                         turn_id=turn.get("id") if isinstance(turn, dict) and method in (
                             "turn/started", "turn/completed") else params.get("turnId"))
            if not event["thread_id"] or not event["turn_id"]:
                self.violation = True
        if method in ("turn/started", "turn/completed"):
            event["status"] = turn.get("status") if isinstance(turn, dict) else None
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if not isinstance(item, dict):
                self.violation = True
                item = {}
            event.update(item_id=item.get("id"), item_type=item.get("type"))
            if event["item_type"] not in ("userMessage", "reasoning", "agentMessage"):
                self.violation = True
            if event["item_type"] == "agentMessage" and method == "item/completed":
                event["is_ok"] = item.get("text") == "OK"
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            event["item_id"] = params.get("itemId")
            # S2 expects only OK; other text is not saved.
            if isinstance(delta, str) and len(delta) <= 2 and set(delta) <= set("OK"):
                event["delta"] = delta
            else:
                self.violation = True
        if method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage") or {}
            last = usage.get("last") if isinstance(usage, dict) else None
            required = ("inputTokens", "outputTokens", "cachedInputTokens",
                        "reasoningOutputTokens", "totalTokens")
            if isinstance(last, dict) and all(type(last.get(k)) is int and last[k] >= 0 for k in required):
                event["usage"] = {k: last[k] for k in required}
                if "cacheWriteInputTokens" in last:
                    if type(last["cacheWriteInputTokens"]) is not int or last["cacheWriteInputTokens"] < 0:
                        self.violation = True
                    else:
                        event["usage"]["cacheWriteInputTokens"] = last["cacheWriteInputTokens"]
            else:
                self.violation = True
        if method == "error":
            self.violation = True
        self.events.append(event)

    def summary(self) -> dict:
        violation = self.violation
        matched = []
        for event in self.events:
            if "thread_id" not in event:
                continue
            if event["thread_id"] != self.thread_id or (self.turn_id and event["turn_id"] != self.turn_id):
                violation = True
            else:
                matched.append(event)
        started = [i for i, e in enumerate(matched) if e["method"] == "turn/started"]
        ended = [i for i, e in enumerate(matched) if e["method"] == "turn/completed"]
        status = matched[ended[0]].get("status") if len(ended) == 1 else None
        # No event without the exact turn binding can settle the probe.
        terminal = bool(self.turn_id and not violation and status in ("completed", "failed", "interrupted"))
        finals = [(i, e) for i, e in enumerate(matched) if e["method"] == "item/completed"
                  and e.get("item_type") == "agentMessage"]
        reply_ok = len(finals) == 1 and finals[0][1].get("is_ok", False)
        stream_ok = False
        if reply_ok:
            finish, final = finals[0]
            item_id = final.get("item_id")
            begins = [i for i, e in enumerate(matched) if e["method"] == "item/started"
                      and e.get("item_id") == item_id and e.get("item_type") == "agentMessage"]
            deltas = [(i, e.get("delta", "")) for i, e in enumerate(matched)
                      if e["method"] == "item/agentMessage/delta" and e.get("item_id") == item_id]
            stream_ok = bool(item_id and len(begins) == 1 and deltas
                             and all(begins[0] < i < finish for i, _ in deltas)
                             and "".join(v for _, v in deltas) == "OK")
            # Every agent-message event must belong to this one stream, not just
            # the subset which happens to match the final message's item id.
            if any(e.get("item_id") != item_id for e in matched if
                   e.get("item_type") == "agentMessage" or e["method"] == "item/agentMessage/delta"):
                violation = True
                terminal = False
                stream_ok = False
            if started and ended:
                stream_ok = bool(stream_ok and begins and started[0] < begins[0] < finish < ended[0])
        if ended and any(e["method"].startswith(("item/", "turn/")) for e in matched[ended[0] + 1:]):
            violation = True
            terminal = False
        usage = next((e["usage"] for e in reversed(matched) if "usage" in e), None)
        checks = {
            "bound_ids": bool(self.thread_id and self.turn_id),
            "one_turn_started": len(started) == 1 and matched[started[0]].get("status") == "inProgress",
            "one_terminal_completed": terminal and status == "completed",
            "exact_OK_reply": reply_ok,
            "matching_stream": stream_ok,
            "usage_received": usage is not None,
            "no_violation": not violation,
        }
        verdict = {"completed": "finished", "failed": "failed", "interrupted": "cancelled"}.get(status, "unconfirmed") if terminal else "unconfirmed"
        return {"passed": all(checks.values()), "checks": checks,
                "terminal_confirmed": terminal, "verdict": verdict,
                "thread_id": self.thread_id, "turn_id": self.turn_id,
                "usage": usage, "final_reply": "OK" if reply_ok else None,
                "violation": violation,
                "event_sequence": [e["method"] for e in self.events],
                "event_counts": dict(Counter(e["method"] for e in self.events))}
