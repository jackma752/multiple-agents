"""Pinned Codex + synthetic Responses server: inspect spawn hook coverage.

Only a new empty home and fixed synthetic tasks are used. No real credentials,
model calls, shell commands from a model, or changes to production admission.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agenthub_adapters.appserver import AppServerClient, AppServerSession, StdioTransport
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER
from agenthub_adapters.codex import build_env


class Provider:
    def __init__(self):
        self.calls, self.tools = 0, []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length < 2_000_000:
                    self.send_error(413)
                    return
                req = json.loads(self.rfile.read(length))
                owner.calls += 1
                if owner.calls > 8:
                    self.send_error(429)
                    return
                offered = [{"name": t.get("name"), "type": t.get("type"),
                                "tools": [x.get("name") for x in t.get("tools", [])]} for t in req.get("tools", [])]
                if offered:
                    owner.tools = offered
                if owner.calls == 1:
                    item = {"id": "fc_fixture", "type": "function_call", "call_id": "call_fixture",
                            "name": "spawn_agent", "namespace": "multi_agent_v1", "arguments": json.dumps({"message": "SYNTHETIC CHILD. Reply DONE.", "task_name": "fixture_child"})}
                else:
                    item = {"id": "msg_fixture", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "SYNTHETIC DONE", "annotations": []}]}
                response = {"id": "resp_fixture_" + str(owner.calls), "object": "response", "status": "completed",
                    "model": req.get("model"), "output": [item], "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for name, body in (("response.created", {"response": {**response, "status": "in_progress", "output": []}}),
                    ("response.output_item.added", {"output_index": 0, "item": item}),
                    ("response.output_item.done", {"output_index": 0, "item": item}),
                    ("response.completed", {"response": response})):
                    self.wfile.write(("event: " + name + "\ndata: " + json.dumps({"type": name, **body}) + "\n\n").encode())
                self.wfile.flush()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve-local-runtime-test", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agents-enabled", action="store_true")
    parser.add_argument("--model", default="gpt-5.4")
    args = parser.parse_args()
    pin = PINNED_APPSERVER
    digest = hashlib.sha256(Path(pin["path"]).read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        raise SystemExit("unreviewed binary; nothing started")
    root = Path("E:/plug/.native-strict-probes") / ("codex-turn-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    new_home = root / "empty-home"
    new_home.mkdir()
    guard = root / "guard.py"
    marker = root / "guard-hit.txt"
    guard.write_text("from pathlib import Path\nimport json\nPath(" + repr(str(marker)) + ").write_text('SYNTHETIC HIT')\n"
                     "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny','permissionDecisionReason':'Synthetic Hub gate'}}))\nraise SystemExit(2)\n", encoding="utf-8")
    source = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI", "CODEX", "AGENT_HUB"))}
    env = build_env(codex_home=str(new_home), proxy="http://127.0.0.1:1", base_env=source)
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
    report = {"binary_sha256": digest, "real_model_requests": 0, "agents_enabled": args.agents_enabled, "scratch": str(root)}
    with Provider() as provider:
        command = str(Path(sys.executable).as_posix()) + " " + guard.as_posix()
        config = (f'model={json.dumps(args.model)}\nmodel_provider="loopback"\nweb_search="disabled"\n'
            '[model_providers.loopback]\nname="Synthetic localhost only"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{provider.server.server_address[1]}/v1"\n'
            f'[agents]\nenabled={str(args.agents_enabled).lower()}\n[features]\nhooks=true\nmulti_agent=true\n'
            '[[hooks.PreToolUse]]\nmatcher=".*"\n[[hooks.PreToolUse.hooks]]\ntype="command"\n'
            f'command={json.dumps(command)}\ncommand_windows={json.dumps(command)}\ntimeout=3\n')
        (new_home / "config.toml").write_text(config, encoding="utf-8")
        client = AppServerClient(StdioTransport([pin["path"], "app-server", "--stdio"], env=env, cwd=str(root),
            stderr_path=str(root / "stderr.txt")), request_timeout=10)
        try:
            client.start()
            AppServerSession(client).initialize()
            result = client.request("thread/start", {"cwd": str(root), "model": args.model,
                "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandbox": "read-only"})
            thread = result.get("thread", {}).get("id")
            report["main_thread"] = bool(thread)
            client.request("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "SYNTHETIC MAIN. Follow the synthetic task."}],
                "approvalPolicy": "on-request", "approvalsReviewer": "user", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}})
            terminal = client.wait_for(lambda n: n.method == "turn/completed" and n.params.get("threadId") == thread, timeout=25)
            report["terminal_status"] = terminal.params.get("turn", {}).get("status")
            notes = client.notifications()
            (root / "notifications.json").write_text(json.dumps([{"method": n.method, "params": n.params} for n in notes], indent=2), encoding="utf-8")
            report["child_threads"] = sum(n.method == "thread/started" and n.params.get("thread", {}).get("id") != thread for n in notes)
            spawned = {child for n in notes for child in (n.params.get("item", {}).get("receiverThreadIds") or [])
                       if n.method == "item/completed" and n.params.get("item", {}).get("type") == "collabAgentToolCall"
                       and n.params.get("item", {}).get("tool") == "spawnAgent"}
            report["native_spawned_children"] = len(spawned)
        except Exception as exc:
            report["error_type"] = type(exc).__name__
        finally:
            client.close()
        report.update(synthetic_provider_requests=provider.calls, offered_tools=provider.tools,
                      pretool_spawn_hook_fired=marker.exists())
    report["scenario_verified"] = ("error_type" not in report and provider.calls >= 2
        and report.get("native_spawned_children") == 0
        and (marker.exists() if args.agents_enabled else True))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["scenario_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
