"""Synthetic approval peer: no shell, network, model or credential access."""
import argparse
import json
import os
import sys

COMMAND = 'cmd.exe /d /c "echo S3_EXECUTED>S3_EXECUTED.txt"'
APPROVAL = "item/commandExecution/requestApproval"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    scenario = parser.parse_args().scenario
    state = 0
    ids = {"threadId": "stub-thread", "turnId": "stub-turn"}

    def send(message):
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()

    def note(method, params):
        send({"method": method, "params": params})

    def command(status):
        return {"id": "cmd-1", "type": "commandExecution", "command": COMMAND,
                "cwd": os.getcwd(), "status": status, "exitCode": None, "processId": None}

    def finish():
        note("item/started", {**ids, "item": {"id": "msg-1", "type": "agentMessage"}})
        note("item/agentMessage/delta", {**ids, "itemId": "msg-1", "delta": "DECLINED"})
        note("item/completed", {**ids, "item": {"id": "msg-1", "type": "agentMessage", "text": "DECLINED"}})
        usage = {"inputTokens": 10, "outputTokens": 3, "cachedInputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 13}
        note("thread/tokenUsage/updated", {**ids, "tokenUsage": {"last": usage, "total": usage}})
        note("turn/completed", {"threadId": "stub-thread", "turn": {
            "id": "stub-turn", "status": "failed" if scenario == "failed-turn" else "completed"}})

    for line in sys.stdin.buffer:
        msg = json.loads(line)
        if state == 4:
            if msg != {"jsonrpc": "2.0", "id": "approval-1", "result": {"decision": "decline"}}:
                return 3
            state = 5
            if scenario == "disconnect":
                return 0
            if scenario == "timeout":
                continue
            if scenario != "no-resolved":
                note("serverRequest/resolved", {"threadId": "stub-thread", "requestId": "approval-1"})
            note("item/completed", {**ids, "item": command("failed" if scenario == "failed-item" else "declined")})
            finish()
            continue
        expected = ("initialize", "initialized", "thread/start", "turn/start")
        if state >= 4 or msg.get("method") != expected[state]:
            return 3
        state += 1
        if msg["method"] == "initialize":
            send({"id": msg["id"], "result": {"userAgent": "SYNTHETIC-S3"}})
        elif msg["method"] == "thread/start":
            p = msg["params"]
            if p.get("sandbox") != "read-only" or p.get("approvalsReviewer") != "user" or p.get("approvalPolicy") != "on-request":
                return 3
            note("thread/started", {"thread": {"id": "stub-thread"}})
            send({"id": msg["id"], "result": {"thread": {"id": "stub-thread"}, "cwd": os.getcwd(),
                  "sandbox": {"type": "workspaceWrite" if scenario == "writable-sandbox" else "readOnly", "networkAccess": False},
                  "approvalsReviewer": "auto_review" if scenario == "auto-review" else "user", "approvalPolicy": "on-request"}})
        elif msg["method"] == "turn/start":
            p = msg["params"]
            if (p.get("sandboxPolicy") != {"type": "readOnly", "networkAccess": False}
                    or p.get("approvalsReviewer") != "user" or COMMAND not in p["input"][0]["text"]):
                return 3
            note("turn/started", {"threadId": "stub-thread", "turn": {"id": "stub-turn", "status": "inProgress"}})
            # Intentionally send approval before the turn/start response.
            if scenario == "selfreport":
                send({"id": msg["id"], "result": {"turn": {"id": "stub-turn"}}})
                finish()
                continue
            note("item/started", {**ids, "item": command("inProgress")})
            if scenario == "executed":
                note("item/completed", {**ids, "item": {**command("completed"), "exitCode": 0}})
                continue
            params = {**ids, "itemId": "cmd-1", "startedAtMs": 0, "command": COMMAND,
                      "cwd": os.getcwd(), "availableDecisions": ["accept", "decline", "cancel"]}
            if scenario == "wrong-turn":
                params["turnId"] = "foreign"
            if scenario == "wrong-item":
                params["itemId"] = "foreign"
            send({"id": "approval-1", "method": APPROVAL, "params": params})
            if scenario == "duplicate":
                send({"id": "approval-1", "method": APPROVAL, "params": params})
            send({"id": msg["id"], "result": {"turn": {"id": "stub-turn"}}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
