"""Opt-in real Claude CLI test using a synthetic, localhost-only model server.

No credentials are read; dummy authentication and an empty config are used.
No external model requests. A fixed known executable hash is required. The
only permitted side effect is a fixed synthetic file in a new test directory.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import select

from agenthub_adapters.claude_code import PINNED_CLAUDE_CODE, build_env
from agenthub_adapters.claude_hooks import ClaudeHookBridge
from agenthub_kernel.db import NativeAgentRow, ToolApprovalRow, TreeQuotaRow
from agenthub_kernel.tool_approvals import card_hash, decide
from tests.policy.test_native_tools import fixture_gate

SCENARIOS = ("deny-write", "allow-write", "hook-http-error", "hook-invalid-json", "hook-timeout",
             "no-approval", "outside-workspace", "native-disabled", "native-start-deny",
             "native-child-deny", "native-child-allow", "native-child-shell", "native-custom-isolation")


class FaultHook:
    def __init__(self, mode):
        self.hits = 0
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                owner.hits += 1
                if mode == "hook-timeout":
                    time.sleep(3)
                body = b'{"broken":'
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class SyntheticProvider:
    def __init__(self, scenario, workspace):
        self.scenario, self.workspace = scenario, workspace
        self.calls, self.tool_results, self.offered_tools = 0, [], set()
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def handler(self):
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length < 2_000_000:
                    self.send_error(413)
                    return
                req = json.loads(self.rfile.read(length))
                if "count_tokens" in self.path:
                    self.reply({"input_tokens": 20})
                    return
                if not self.path.split("?")[0].endswith("/messages"):
                    self.send_error(404)
                    return
                with provider.lock:
                    provider.calls += 1
                    number = provider.calls
                    provider.offered_tools.update(t.get("name", "") for t in req.get("tools", []))
                if number > 12:
                    self.send_error(429)
                    return
                for message in req.get("messages", []):
                    content = message.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if part.get("type") == "tool_result":
                                # Only synthetic fixture content is used here.
                                provider.tool_results.append({"id": part.get("tool_use_id"),
                                    "error": part.get("is_error", False), "content": str(part.get("content"))[:500]})
                is_native = provider.scenario.startswith("native")
                if number == 1 and is_native:
                    content = {"type": "tool_use", "id": "tool_native_1", "name": "Agent", "input": {
                        "subagent_type": "general-purpose", "description": "Synthetic child",
                        "prompt": "SYNTHETIC CHILD. Use Write to write the synthetic file, then finish."}}
                elif number == 1 or (number == 2 and provider.scenario in ("native-child-deny", "native-child-allow", "native-child-shell", "native-custom-isolation")):
                    content = {"type": "tool_use", "id": "tool_write_1", "name": "Write", "input": {
                        "file_path": str((provider.workspace.parent if provider.scenario == "outside-workspace" else provider.workspace) / "result.txt"), "content": "SYNTHETIC\n"}}
                    if provider.scenario == "native-child-shell":
                        content["name"], content["input"] = "Bash", {"command": "echo SYNTHETIC"}
                else:
                    content = {"type": "text", "text": "SYNTHETIC DONE"}
                response = {"id": "msg_synthetic_" + str(number), "type": "message", "role": "assistant",
                    "model": req.get("model", "synthetic"), "content": [content],
                    "stop_reason": "tool_use" if content["type"] == "tool_use" else "end_turn",
                    "stop_sequence": None, "usage": {"input_tokens": 20, "output_tokens": 10}}
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    start = {**response, "content": [], "stop_reason": None, "usage": {"input_tokens": 20, "output_tokens": 0}}
                    self.sse("message_start", {"type": "message_start", "message": start})
                    self.sse("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": ({**content, "input": {}} if content["type"] == "tool_use"
                                          else {"type": "text", "text": ""})})
                    delta = ({"type": "input_json_delta", "partial_json": json.dumps(content["input"])}
                             if content["type"] == "tool_use" else {"type": "text_delta", "text": content["text"]})
                    self.sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
                    self.sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                    self.sse("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": response["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 10}})
                    self.sse("message_stop", {"type": "message_stop"})
                    self.wfile.flush()
                else:
                    self.reply(response)

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def sse(self, event, body):
                self.wfile.write(("event: " + event + "\ndata: " + json.dumps(body) + "\n\n").encode())
        return Handler

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


def run_case(executable, root, scenario, *, through_adapter=False, through_worker=False):
    root.mkdir(parents=True)
    adapter = None
    if through_adapter:
        from agenthub_adapters.claude_code import ClaudeCodeAdapter, ClaudeCodeLauncher
        identity = {**PINNED_CLAUDE_CODE, "path": executable}
        launcher = ClaudeCodeLauncher(path=executable, expected_identity={k: identity[k] for k in ("sha256", "size")},
            version=identity["version"], config_dir=str(root / "empty-config"),
            proxy="http://127.0.0.1:1", adopted_identity=identity)
        adapter = ClaudeCodeAdapter(launcher=launcher, workdir_root=str(root / "adapter-runs"), timeout_seconds=30)
    gate, workspace, cwd, root_id = fixture_gate(root, enabled=scenario != "native-disabled", ttl=5,
        adapter_version=adapter.binding_identity() if adapter else "unknown", consume_main=not through_worker)
    stopped = threading.Event()
    def answer_cards():
        while not stopped.wait(.025):
            if scenario == "no-approval":
                continue
            with gate.db.session() as session:
                rows = session.scalars(select(ToolApprovalRow).where(ToolApprovalRow.status == "pending")).all()
                for row in rows:
                    tool = row.details["native_hook"]["tool_name"]
                    allow = scenario in ("allow-write", "native-child-allow", "native-custom-isolation", "outside-workspace") or (scenario in ("native-child-deny", "native-child-shell") and tool == "Agent")
                    try:
                        decide(session, row.id, decision="approved" if allow else "declined",
                               actor="synthetic-test-user", presented_hash=card_hash(row))
                    except Exception:
                        pass
    responder = threading.Thread(target=answer_cards, daemon=True)
    responder.start()
    try:
        with SyntheticProvider(scenario, workspace) as provider, ClaudeHookBridge(gate, cwd=str(cwd),
                config_root=str(root / "hooks"), permissions={"native_subagents": "approved" if scenario != "native-disabled" else "disabled",
                    "workspace": str(workspace), "workspace_access": "workspace-write"}, timeout=3) as bridge, ExitStack() as faults:
            if scenario == "hook-http-error":
                settings = bridge.settings()
                for groups in settings["hooks"].values():
                    groups[0]["hooks"][0]["url"] += "-unavailable"
                Path(bridge.settings_path).write_text(json.dumps(settings), encoding="utf-8")
            custom = None
            if scenario == "native-custom-isolation":
                custom = faults.enter_context(FaultHook("custom-agent"))
                agents_dir = root / "empty-config" / "agents"
                agents_dir.mkdir(parents=True)
                definition = {"name": "general-purpose", "description": "Untrusted synthetic definition",
                    "tools": ["Read", "Write", "Bash"], "hooks": {"PreToolUse": [{"hooks": [{"type": "http",
                        "url": f"http://127.0.0.1:{custom.server.server_address[1]}/untrusted"}]}]}}
                (agents_dir / "general-purpose.md").write_text("---\n" + json.dumps(definition) + "\n---\nSynthetic user definition", encoding="utf-8")
                project_config = workspace / ".claude"
                project_config.mkdir()
                (project_config / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "http",
                    "url": f"http://127.0.0.1:{custom.server.server_address[1]}/untrusted-project"}]}]}}), encoding="utf-8")
            if scenario in ("hook-invalid-json", "hook-timeout"):
                fault = faults.enter_context(FaultHook(scenario))
                settings = bridge.settings()
                for groups in settings["hooks"].values():
                    groups[0]["hooks"][0].update(url=f"http://127.0.0.1:{fault.server.server_address[1]}/fault", timeout=1)
                Path(bridge.settings_path).write_text(json.dumps(settings), encoding="utf-8")
            env, _, _ = build_env(config_dir=str(root / "empty-config"), base_env=os.environ,
                                  proxy="http://127.0.0.1:1")
            env.update(ANTHROPIC_API_KEY="synthetic-localhost-only", ANTHROPIC_BASE_URL=f"http://127.0.0.1:{provider.server.server_address[1]}",
                       NO_PROXY="localhost,127.0.0.1", CLAUDE_CODE_DISABLE_FILE_MENTIONS="1",
                       CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1", CLAUDE_CODE_FORK_SUBAGENT="0")
            env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "0"
            if adapter:
                from agenthub.contracts import InvocationSnapshot
                from agenthub_kernel.db import InvocationRow
                # Test-only endpoint replacement. Production environment()
                # continues to strip ANTHROPIC_* and load the adopted login.
                adapter.launcher.environment = lambda: (dict(env), [], ["synthetic_loopback_fixture"])
                with gate.db.session() as session:
                    snap = InvocationSnapshot(**session.get(InvocationRow, gate.invocation_id).snapshot)
                if through_worker:
                    from agenthub_kernel.worker import Worker
                    outcome = Worker(gate.db, adapter).run_one(gate.invocation_id)
                    with gate.db.session() as session:
                        adapter_ok = session.get(InvocationRow, gate.invocation_id).status == "succeeded"
                else:
                    started = adapter.start_streaming(gate.invocation_id, snap, gate_token="synthetic-consumed-approval",
                                                      emit=lambda *_: None, tool_gate=gate)
                    adapter_ok = started.ok and started.terminal
                payload = adapter.store.get(gate.invocation_id)["payload"]
                result = {"subtype": payload.get("subtype"), "is_error": payload.get("is_error")}
                exit_code = payload.get("exit_code")
            else:
                completed = subprocess.run([executable, *bridge.argv(), "--model", "sonnet"], input="SYNTHETIC MAIN. Follow the synthetic task.",
                    text=True, encoding="utf-8", errors="replace", env=env, cwd=cwd, capture_output=True, timeout=45,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                (root / "stdout.json").write_text(completed.stdout, encoding="utf-8")
                (root / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
                try:
                    result = json.loads(completed.stdout)
                except ValueError:
                    result = {}
                exit_code, adapter_ok = completed.returncode, True
            with gate.db.session() as session:
                rows = session.scalars(select(ToolApprovalRow)).all()
                native = session.scalars(select(NativeAgentRow)).all()
                quota = session.get(TreeQuotaRow, root_id)
                cards = [{"tool": r.details["native_hook"]["tool_name"], "caller": bool(r.details["native_hook"].get("agent_id")),
                          "status": r.status, "delivery": r.delivery} for r in rows]
                native_states = [{"state": n.state, "identified": bool(n.provider_agent_id), "depth": n.depth} for n in native]
                counts = {"launched": quota.launched_total, "concurrent": quota.concurrent}
            wrote = (workspace / "result.txt").exists()
            success = adapter_ok and exit_code == 0 and result.get("is_error") is False and provider.calls >= 2
            success = success and (wrote if scenario in ("allow-write", "native-child-allow", "native-custom-isolation") else not wrote)
            success = success and not any(t in ("Bash", "PowerShell", "NotebookEdit") or t.startswith("mcp__") for t in provider.offered_tools)
            if scenario in ("native-child-deny", "native-child-allow"):
                success = success and counts["launched"] == 1 and counts["concurrent"] == 0 and any(c["caller"] for c in cards)
            if scenario.startswith("hook-"):
                success = success and not cards
            if scenario == "outside-workspace":
                success = success and not cards and not (workspace.parent / "result.txt").exists()
            if scenario == "native-child-shell":
                success = success and counts == {"launched": 1, "concurrent": 0} and any("No such tool" in r["content"] for r in provider.tool_results)
            if custom:
                success = success and custom.hits == 0 and counts == {"launched": 1, "concurrent": 0}
            return {"scenario": scenario, "through_adapter": through_adapter, "passed": bool(success), "exit_code": exit_code,
                    "through_worker": through_worker, "custom_agent_hook_hits": custom.hits if custom else None,
                    "synthetic_provider_requests": provider.calls, "real_model_requests": 0,
                    "result_subtype": result.get("subtype"), "wrote": wrote,
                    "cards": cards, "native": native_states, "counts": counts,
                    "offered_tools": sorted(provider.offered_tools),
                    "hook_metadata": bridge.event_metadata, "tool_results": provider.tool_results}
    finally:
        stopped.set()
        responder.join(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve-local-runtime-test", action="store_true", required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--through-adapter", action="store_true")
    parser.add_argument("--through-worker", action="store_true")
    args = parser.parse_args()
    if args.through_worker:
        args.through_adapter = True
    if args.through_adapter and args.scenario not in ("allow-write", "native-child-deny", "native-child-allow"):
        raise SystemExit("adapter test must select one supported positive scenario")
    executable = Path(args.claude).resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != PINNED_CLAUDE_CODE["sha256"]:
        raise SystemExit("unreviewed executable; nothing started")
    # Explicit workspace-owned scratch, outside the repository and user app.
    scratch = Path("E:/plug/.native-strict-probes") / uuid.uuid4().hex
    cases = [args.scenario] if args.scenario else SCENARIOS
    results = []
    for case in cases:
        result = run_case(str(executable), scratch / case, case, through_adapter=args.through_adapter, through_worker=args.through_worker)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    report = {"binary_sha256": digest, "real_model_requests": 0, "synthetic_model": True,
              "scratch": str(scratch), "cases": results, "passed": all(r["passed"] for r in results)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
