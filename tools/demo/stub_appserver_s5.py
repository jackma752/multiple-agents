"""Synthetic resume peer for S5: no shell, network, model or credential access.

One process plays phase 1 (thread/start + a planting turn) and writes the thread
to ``$CODEX_HOME/stub-threads.json`` - its "disk". A second process plays phase
2: ``thread/resume`` reads that file back, and the recall turn answers from the
stored history only. Scenarios break one link at a time.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

CODE = re.compile(r"S5-[A-Z0-9]{10}")
APPROVAL = "item/commandExecution/requestApproval"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    scenario = parser.parse_args().scenario
    store = Path(os.environ["CODEX_HOME"]) / "stub-threads.json"
    state, phase, thread, turn_id = 0, None, None, None

    def send(message):
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()

    def note(method, params):
        send({"method": method, "params": params})

    def ids(turn=None):
        return {"threadId": thread["id"], "turnId": turn or turn_id}

    def settings(writable=False):
        return {"cwd": os.getcwd(), "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite" if writable else "readOnly", "networkAccess": False}}

    def say(text, *, turn=None, item="msg-1"):
        note("item/started", {**ids(turn), "item": {"id": item, "type": "agentMessage"}})
        note("item/agentMessage/delta", {**ids(turn), "itemId": item, "delta": text})
        note("item/completed", {**ids(turn), "item": {"id": item, "type": "agentMessage", "text": text}})

    def finish(status="completed"):
        last = {"inputTokens": 30, "outputTokens": 5, "cachedInputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 35}
        note("thread/tokenUsage/updated", {**ids(), "tokenUsage": {"last": last, "total": last}})
        note("turn/completed", {"threadId": thread["id"], "turn": {"id": turn_id, "status": status, "items": []}})

    for line in sys.stdin.buffer:
        msg = json.loads(line)
        if "result" in msg or "error" in msg:
            return 3  # S5 must never answer a server request
        method, params = msg.get("method"), msg.get("params") or {}
        if state == 0 and method == "initialize":
            send({"id": msg["id"], "result": {"userAgent": "SYNTHETIC-S5"}})
            state = 1
        elif state == 1 and method == "initialized":
            state = 2
        elif state == 2 and method == "thread/start":
            if (params.get("sandbox") != "read-only" or params.get("approvalPolicy") != "on-request"
                    or params.get("approvalsReviewer") != "user"):
                return 3
            phase, thread = "plant", {"id": "stub-thread", "cwd": os.getcwd(), "turns": []}
            note("thread/started", {"thread": {"id": thread["id"]}})
            send({"id": msg["id"], "result": {"thread": {"id": thread["id"], "turns": []}, **settings()}})
            state = 3
        elif state == 2 and method == "thread/resume":
            phase = "recall"
            saved = json.loads(store.read_text(encoding="utf-8")) if store.is_file() else {}
            if scenario == "resume-error" or params.get("threadId") not in saved:
                send({"id": msg["id"], "error": {"code": -32600, "message": "thread not found: SYNTHETIC"}})
                continue
            thread = saved[params["threadId"]]
            turns = thread["turns"]
            if scenario == "no-history-returned":
                turns = []
            elif scenario == "history-without-message":
                turns = [{**t, "items": [i for i in t["items"] if i["type"] != "userMessage"]} for t in turns]
            shown = {"id": "other-thread" if scenario == "wrong-thread-resume" else thread["id"], "turns": turns,
                     "path": str(store)}
            send({"id": msg["id"], "result": {"thread": shown, **settings(scenario == "resume-writable")}})
            state = 3
        elif state == 3 and method == "turn/start":
            text = params["input"][0]["text"]
            if params.get("threadId") != thread["id"] or params.get("sandboxPolicy") != {"type": "readOnly", "networkAccess": False}:
                return 3
            if phase == "recall" and CODE.search(text):
                return 3  # the recall prompt must not carry the code word
            turn_id = "turn-1" if phase == "plant" or scenario == "same-turn-id" else "turn-2"
            note("turn/started", {"threadId": thread["id"], "turn": {"id": turn_id, "status": "inProgress", "items": []}})
            send({"id": msg["id"], "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
            state = 4
            if phase == "plant":
                if scenario == "plant-timeout":
                    continue
                if scenario == "plant-tool":
                    note("item/started", {**ids(), "item": {"id": "exec-0", "type": "commandExecution", "command": "x",
                                                             "cwd": os.getcwd(), "status": "inProgress", "processId": "55"}})
                    continue
                if scenario == "plant-provider-error":
                    note("error", {**ids(), "error": {"message": "SYNTHETIC provider error"}, "willRetry": False})
                say("STORED")
                status = "failed" if scenario == "plant-failed" else "completed"
                thread["turns"].append({"id": turn_id, "status": status, "items": [
                    {"id": "user-1", "type": "userMessage", "content": [{"type": "text", "text": text}]},
                    {"id": "msg-1", "type": "agentMessage", "text": "STORED"}]})
                store.write_text(json.dumps({thread["id"]: thread}), encoding="utf-8")
                finish(status)
                continue
            # Recall: answer from the stored history only.
            history = json.dumps(thread["turns"])
            found = CODE.search(history)
            if scenario == "recall-disconnect":
                return 0
            if scenario == "recall-tool":
                note("item/started", {**ids(), "item": {"id": "exec-1", "type": "commandExecution", "command": "x",
                                                         "cwd": os.getcwd(), "status": "inProgress", "processId": "77"}})
                continue
            if scenario == "recall-approval":
                note("item/started", {**ids(), "item": {"id": "exec-1", "type": "commandExecution", "command": "x",
                                                         "cwd": os.getcwd(), "status": "inProgress", "processId": None}})
                send({"id": "approval-1", "method": APPROVAL, "params": {**ids(), "itemId": "exec-1"}})
                continue
            if scenario == "foreign-turn":
                note("item/agentMessage/delta", {**ids("turn-x"), "itemId": "msg-2", "delta": "x"})
            if scenario == "forgetful" or not found:
                say("SYNTHETIC I do not know.", item="msg-2")
            elif scenario == "loose-recall":
                say(f"SYNTHETIC The code word is {found.group(0)}.", item="msg-2")
            else:
                say(found.group(0), item="msg-2")
            finish()
            if scenario == "late-event":
                note("item/agentMessage/delta", {**ids(), "itemId": "msg-2", "delta": "late"})
        else:
            return 3
    if scenario == "plant-exit-nonzero" and phase == "plant":
        return 1  # the turn completed, but process A does not exit cleanly
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
