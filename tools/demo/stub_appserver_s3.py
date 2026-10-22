"""Synthetic approval peer: no shell, network, model or credential access.

Commands are reported the way the tested Codex build reports them: the fixed
command wrapped in the session shell's argv and shlex-joined (see
appserver_s3_evidence). Naming that shell only checks whether files exist.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/adapters"))
from agenthub_adapters.appserver_s3_evidence import (  # noqa: E402
    APPROVAL, COMMAND, resolve_session_shell, wrapped_forms)

ITEM_ID = "exec-00000000-0000-4000-8000-000000000001"  # code-mode nested call id shape
REASON = "SYNTHETIC-REASON please approve this"  # stands in for model-written justification


def reported_commands(scenario):
    """(item command, approval command) strings, as Codex would report them."""
    shell = resolve_session_shell()
    forms = wrapped_forms(shell)
    default_argv = next(iter(forms.values()))
    path, flags = default_argv[0], default_argv[1:-1]
    default = shlex.join(default_argv)
    noprofile = shlex.join(forms.get("powershell_noprofile_command", default_argv))
    item = {
        "noprofile": noprofile,
        "bare-command": COMMAND,
        "rewritten-command": shlex.join([path, *flags, COMMAND.replace(">S3_EXECUTED.txt", ">S3_OTHER.txt")]),
        "appended-command": shlex.join([path, *flags, COMMAND + " & del S3_EXECUTED.txt"]),
        "extra-argument": shlex.join([*default_argv, "-NoExit"]),
        "other-shell": shlex.join([r"C:\Windows\System32\cmd.exe", "/c", COMMAND]),
    }.get(scenario, default)
    return item, (noprofile if scenario == "approval-command-differs" else item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    scenario = parser.parse_args().scenario
    item_command, approval_command = reported_commands(scenario)
    state = 0
    ids = {"threadId": "stub-thread", "turnId": "stub-turn"}

    def send(message):
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()

    def note(method, params):
        send({"method": method, "params": params})

    def command(status):
        return {"id": ITEM_ID, "type": "commandExecution", "command": item_command, "commandActions": [],
                "cwd": os.getcwd(), "source": "agent", "status": status, "exitCode": None, "processId": None}

    def say(item_id, pieces):
        """Arbitrary assistant text in several deltas (the probe must not keep it)."""
        note("item/started", {**ids, "item": {"id": item_id, "type": "agentMessage"}})
        for piece in pieces:
            note("item/agentMessage/delta", {**ids, "itemId": item_id, "delta": piece})
        note("item/completed", {**ids, "item": {"id": item_id, "type": "agentMessage", "text": "".join(pieces)}})

    def finish():
        if scenario == "explain-loose-reply":
            say("msg-1", ("The command was declined, ", "so I did not run it."))
        else:
            say("msg-1", ("DECL", "INED"))
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
            if scenario == "never-approval":
                # Several assistant messages, never an approval, a completed turn.
                send({"id": msg["id"], "result": {"turn": {"id": "stub-turn"}}})
                say("msg-0", ("I can't run commands ", "in this environment."))
                say("msg-1", ("No approval was requested.",))
                usage = {"inputTokens": 10, "outputTokens": 9, "cachedInputTokens": 0,
                         "reasoningOutputTokens": 0, "totalTokens": 19}
                note("thread/tokenUsage/updated", {**ids, "tokenUsage": {"last": usage, "total": usage}})
                note("turn/completed", {"threadId": "stub-thread", "turn": {"id": "stub-turn", "status": "completed"}})
                continue
            if scenario in ("explain-then-decline", "explain-loose-reply", "explain-then-executed"):
                say("msg-0", ("I will ask for approval ", "to run that command ", "first."))
            # Intentionally send approval before the turn/start response.
            if scenario == "selfreport":
                send({"id": msg["id"], "result": {"turn": {"id": "stub-turn"}}})
                finish()
                continue
            # The app-server's pending item, emitted just before the approval.
            note("item/started", {**ids, "item": command("inProgress")})
            if scenario in ("executed", "explain-then-executed"):
                note("item/completed", {**ids, "item": {**command("completed"), "exitCode": 0}})
                continue
            # As the tested build sends it: local environment, default decision
            # list without "decline", and a model-written reason.
            params = {**ids, "itemId": ITEM_ID, "startedAtMs": 0, "command": approval_command,
                      "cwd": os.getcwd(), "kind": "command", "reason": REASON,
                      "environmentId": "remote" if scenario == "remote-environment" else "local",
                      "availableDecisions": ["accept", "cancel"]}
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
