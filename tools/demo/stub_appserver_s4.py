"""Synthetic interrupt peer for S4: no shell, network, model or credential access.

It follows the tested build's order: a turn interrupt is answered only when the
turn is aborted, the answer goes out before ``turn/completed(interrupted)``,
and an in-progress item gets no completion of its own.
"""
import argparse
import json
import os
import sys

IDS = {"threadId": "stub-thread", "turnId": "stub-turn"}
APPROVAL = "item/commandExecution/requestApproval"
TEXT = "SYNTHETIC-TEXT never to be stored "  # stands in for model output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    scenario = parser.parse_args().scenario
    state = 0

    def send(message):
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()

    def note(method, params):
        send({"method": method, "params": params})

    def delta(text=TEXT, **ids):
        note("item/agentMessage/delta", {**IDS, **ids, "itemId": "msg-1", "delta": text})

    def terminal(status="interrupted", turn_id="stub-turn"):
        note("turn/completed", {"threadId": "stub-thread", "turn": {"id": turn_id, "status": status, "items": []}})

    def usage():
        last = {"inputTokens": 20, "outputTokens": 4, "cachedInputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 24}
        note("thread/tokenUsage/updated", {**IDS, "tokenUsage": {"last": last, "total": last}})

    for line in sys.stdin.buffer:
        msg = json.loads(line)
        if state == 4:
            if "result" in msg or "error" in msg:
                return 3  # S4 must never answer a server request
            if msg != {"jsonrpc": "2.0", "id": msg.get("id"), "method": "turn/interrupt", "params": IDS}:
                return 3
            state = 5
            rid = msg["id"]
            if scenario == "disconnect":
                return 0
            if scenario == "timeout":
                continue
            if scenario == "response-no-terminal":
                send({"id": rid, "result": {}})
                continue
            if scenario == "interrupt-error":
                send({"id": rid, "error": {"code": -32600, "message": "SYNTHETIC rejected"}})
                continue
            if scenario == "completed-race":
                send({"id": rid, "error": {"code": -32600, "message": "no active turn to interrupt"}})
                usage()
                terminal("completed")
                continue
            if scenario == "server-request":
                note("serverRequest/resolved", {"threadId": "stub-thread", "requestId": "approval-1"})
            if scenario == "late-response":
                terminal()
                send({"id": rid, "result": {}})
                continue
            send({"id": rid, "result": {}})
            if scenario == "failed-terminal":
                terminal("failed")
            elif scenario == "wrong-turn-terminal":
                terminal(turn_id="foreign-turn")
            else:
                terminal()
            usage()
            if scenario == "duplicate-terminal":
                terminal()
            if scenario == "late-delta":
                delta()
            continue
        if state == 5:
            if "result" in msg or "error" in msg or msg.get("method") == "turn/interrupt":
                return 3  # no answers, no second interrupt
            continue
        expected = ("initialize", "initialized", "thread/start", "turn/start")
        if state >= 4 or msg.get("method") != expected[state]:
            return 3
        state += 1
        if msg["method"] == "initialize":
            send({"id": msg["id"], "result": {"userAgent": "SYNTHETIC-S4"}})
        elif msg["method"] == "thread/start":
            p = msg["params"]
            if p.get("sandbox") != "read-only" or p.get("approvalsReviewer") != "user" or p.get("approvalPolicy") != "on-request":
                return 3
            note("thread/started", {"thread": {"id": "stub-thread"}})
            send({"id": msg["id"], "result": {"thread": {"id": "stub-thread"}, "cwd": os.getcwd(),
                  "sandbox": {"type": "readOnly", "networkAccess": False},
                  "approvalsReviewer": "auto_review" if scenario == "auto-review" else "user",
                  "approvalPolicy": "on-request"}})
        elif msg["method"] == "turn/start":
            p = msg["params"]
            if (p.get("sandboxPolicy") != {"type": "readOnly", "networkAccess": False}
                    or p.get("approvalsReviewer") != "user" or "400 different English nouns" not in p["input"][0]["text"]):
                return 3
            note("turn/started", {"threadId": "stub-thread", "turn": {"id": "stub-turn", "status": "inProgress", "items": []}})
            send({"id": msg["id"], "result": {"turn": {"id": "stub-turn", "status": "inProgress", "items": []}}})
            if scenario == "completed-first":
                # The turn ends before anything could trigger an interrupt.
                note("item/started", {**IDS, "item": {"id": "msg-1", "type": "agentMessage"}})
                delta()
                note("item/completed", {**IDS, "item": {"id": "msg-1", "type": "agentMessage", "text": TEXT}})
                usage()
                terminal("completed")
                state = 5
                continue
            if scenario == "foreign-thread":
                delta(threadId="foreign-thread")
            elif scenario == "tool-item":
                note("item/started", {**IDS, "item": {"id": "exec-1", "type": "commandExecution", "command": "x",
                                                       "cwd": os.getcwd(), "status": "inProgress", "processId": "4242",
                                                       "commandActions": []}})
            elif scenario == "server-request":
                note("item/started", {**IDS, "item": {"id": "exec-1", "type": "commandExecution", "command": "x",
                                                       "cwd": os.getcwd(), "status": "inProgress", "processId": None,
                                                       "commandActions": []}})
                send({"id": "approval-1", "method": APPROVAL, "params": {**IDS, "itemId": "exec-1", "startedAtMs": 0}})
            elif scenario != "timer":
                note("item/started", {**IDS, "item": {"id": "msg-1", "type": "agentMessage"}})
                delta()
                delta("more " + TEXT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
