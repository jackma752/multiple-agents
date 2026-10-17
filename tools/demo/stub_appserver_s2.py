"""Synthetic S2 protocol peer. Python stdio only; no network, model or disk writes."""
from __future__ import annotations

import argparse
import json
import sys


USAGE = {"inputTokens": 10, "cachedInputTokens": 3, "outputTokens": 2,
         "reasoningOutputTokens": 0, "totalTokens": 12}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, choices=(
        "happy", "turn-failed", "no-usage", "wrong-turn", "approval", "tool", "disconnect", "timeout"))
    scenario = parser.parse_args().scenario
    state = 0

    def send(message):
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()

    def notify(method, params):
        send({"method": method, "params": params})

    def reply(request, result):
        send({"id": request["id"], "result": result})

    for line in sys.stdin.buffer:
        request = json.loads(line)
        method, params = request.get("method"), request.get("params", {})
        expected = ("initialize", "initialized", "thread/start", "turn/start")
        if state >= len(expected) or method != expected[state]:
            return 3
        state += 1
        if method == "initialize":
            reply(request, {"userAgent": "agent-hub-SYNTHETIC-s2"})
        elif method == "initialized":
            if "id" in request:
                return 3
        elif method == "thread/start":
            if params.get("sandbox") != "read-only" or params.get("approvalPolicy") != "untrusted":
                return 3
            notify("thread/started", {"thread": {"id": "stub-thread"}})
            reply(request, {"thread": {"id": "stub-thread"}})
        else:
            if params != {"threadId": "stub-thread", "input": [{"type": "text", "text":
                "Reply with exactly: OK. Do not use tools, create subagents, or modify any files."}],
                "approvalPolicy": "untrusted", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}:
                return 3
            ids = {"threadId": "stub-thread", "turnId": "stub-turn"}
            # Notifications can precede the matching RPC response.
            notify("turn/started", {"threadId": "stub-thread", "turn": {"id": "stub-turn", "status": "inProgress"}})
            reply(request, {"turn": {"id": "stub-turn", "status": "inProgress"}})
            if scenario == "disconnect":
                return 0
            if scenario == "timeout":
                continue  # Wait for owner to close stdin, never manufacture a terminal event.
            if scenario == "approval":
                send({"id": "approval-1", "method": "item/commandExecution/requestApproval",
                      "params": {**ids, "itemId": "command-1", "startedAtMs": 0}})
                continue
            if scenario == "tool":
                notify("item/started", {**ids, "item": {"id": "command-1", "type": "commandExecution"}})
                continue
            if scenario == "wrong-turn":
                ids["turnId"] = "foreign-turn"
            notify("item/started", {**ids, "item": {"type": "agentMessage", "id": "message-1", "text": ""}})
            for text in ("O", "K"):
                notify("item/agentMessage/delta", {**ids, "itemId": "message-1", "delta": text})
            notify("item/completed", {**ids, "item": {"type": "agentMessage", "id": "message-1", "text": "OK"}})
            if scenario != "no-usage":
                notify("thread/tokenUsage/updated", {**ids, "tokenUsage": {"last": USAGE, "total": USAGE}})
            notify("turn/completed", {"threadId": ids["threadId"], "turn": {
                "id": ids["turnId"], "status": "failed" if scenario == "turn-failed" else "completed"}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
