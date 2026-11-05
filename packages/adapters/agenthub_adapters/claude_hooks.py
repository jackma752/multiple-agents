"""Invocation-scoped loopback HTTP hooks. Never exposes Hub user credentials.

This is a transport for the kernel gate, not a source of authority. The server
has no approve endpoint. It accepts only this session's authenticated events
and always returns an explicit denial on a PreToolUse error.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agenthub.native import CLAUDE_HOOK_EVENTS, CLAUDE_STRICT_TOOLS, MAX_HOOK_BYTES, NATIVE_APPROVED, native_profile


def denied():
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": "Hub authorization unavailable"}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class ClaudeHookBridge:
    def __init__(self, gate, *, cwd, config_root, permissions, timeout=300):
        self.gate, self.cwd = gate, os.path.realpath(cwd)
        self.session_id = str(uuid.uuid4())
        self.token = secrets.token_urlsafe(32)
        self.timeout = max(1, min(float(timeout), 300))
        self._slots = threading.BoundedSemaphore(16)
        self._stopped = threading.Event()
        self.event_metadata = []  # bounded structural diagnostics, no bodies or credentials
        if not os.path.isabs(config_root):
            raise ValueError("hook settings root must be absolute")
        os.makedirs(config_root, exist_ok=True)
        self._directory = os.path.realpath(tempfile.mkdtemp(prefix="hook-", dir=config_root))
        if os.path.commonpath([self._directory, os.path.realpath(config_root)]) != os.path.realpath(config_root):
            raise ValueError("hook settings escaped their owned directory")
        self.settings_path = os.path.join(self._directory, "settings.json")
        self.permissions = permissions
        self.server = _Server(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/native-hook"
        self.gate.bind_native(self.cwd)
        settings = self.settings()
        with open(self.settings_path, "x", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def settings(self):
        hook = {"type": "http", "url": self.url,
                "headers": {"Authorization": "Bearer " + self.token},
                "timeout": self.timeout, "onFailure": "block"}
        hooks = {event: [{"hooks": [dict(hook)]}] for event in CLAUDE_HOOK_EVENTS}
        # No persistent broad allows. Every supported tool reaches our gate.
        deny = ["Bash", "PowerShell", "NotebookEdit", "mcp__*"]
        if native_profile(self.permissions) != NATIVE_APPROVED:
            deny.append("Agent")
        return {"hooks": hooks, "permissions": {"deny": deny},
                "disableAllHooks": False,
                "env": {"CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
                        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "1",
                        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
                        "CLAUDE_CODE_FORK_SUBAGENT": "0",
                        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "0",
                        "CLAUDE_CODE_DISABLE_FILE_MENTIONS": "1"}}

    def argv(self):
        tools = [x for x in CLAUDE_STRICT_TOOLS
                 if x != "Agent" or native_profile(self.permissions) == NATIVE_APPROVED]
        agents = {"general-purpose": {
            "description": "Hub controlled file task worker",
            "prompt": "Complete the delegated task using the available file tools and return your findings.",
            "tools": [x for x in CLAUDE_STRICT_TOOLS if x != "Agent"], "model": "inherit"}}
        directory = ["--add-dir", self.permissions["workspace"]] if self.permissions.get("workspace") else []
        return ["-p", "--output-format", "json", "--setting-sources", "", "--agents", json.dumps(agents), *directory,
                "--settings", self.settings_path, "--tools", ",".join(tools),
                "--strict-mcp-config", "--disable-slash-commands",
                "--permission-mode", "dontAsk", "--session-id", self.session_id,
                "--max-turns", "20", "--no-session-persistence"]

    def _handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # paths, headers and callback bodies must never be logged

            def do_POST(self):
                if not bridge._slots.acquire(blocking=False):
                    self.send_error(503)
                    return
                event = None
                try:
                    self.connection.settimeout(5)
                    if (bridge._stopped.is_set() or self.path != "/native-hook"
                            or self.client_address[0] != "127.0.0.1"
                            or self.headers.get("Origin")
                            or self.headers.get("Host") != f"127.0.0.1:{bridge.port}"
                            or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + bridge.token)):
                        self.send_error(403)
                        return
                    raw_length = self.headers.get("Content-Length", "")
                    if (self.headers.get("Transfer-Encoding") or not raw_length.isdecimal()
                            or not 0 < int(raw_length) <= MAX_HOOK_BYTES):
                        self.send_error(413)
                        return
                    body = self.rfile.read(int(raw_length))
                    event = json.loads(body)
                    if isinstance(event, dict) and len(bridge.event_metadata) < 100:
                        bridge.event_metadata.append({"event": event.get("hook_event_name"),
                            "agent_present": bool(event.get("agent_id")),
                            "agent_type": str(event.get("agent_type", ""))[:100],
                            "session_match": event.get("session_id") == bridge.session_id,
                            "cwd_match": isinstance(event.get("cwd"), str) and
                                os.path.normcase(os.path.realpath(event["cwd"])) == os.path.normcase(bridge.cwd)})
                    if (not isinstance(event, dict) or event.get("session_id") != bridge.session_id
                            or event.get("hook_event_name") not in CLAUDE_HOOK_EVENTS
                            or not isinstance(event.get("cwd"), str)
                            or os.path.normcase(os.path.realpath(event["cwd"])) != os.path.normcase(bridge.cwd)):
                        self.send_error(403)
                        return
                    try:
                        response = bridge.gate.native_event(event)
                        if not isinstance(response, dict):
                            response = denied()
                        if event["hook_event_name"] == "PreToolUse" and (response.get("hookSpecificOutput") or {}).get("permissionDecision") not in ("allow", "deny"):
                            response = denied()
                    except Exception:
                        response = denied()
                    data = json.dumps(response).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                    bridge.gate.native_delivered(event, decision=(response.get("hookSpecificOutput") or {}).get("permissionDecision"), ok=True)
                except Exception:
                    if isinstance(event, dict):
                        try:
                            bridge.gate.native_delivered(event, decision=(locals().get("response", {}).get("hookSpecificOutput") or {}).get("permissionDecision"), ok=False)
                        except Exception:
                            pass
                    self.close_connection = True  # onFailure:block handles transport failure
                finally:
                    bridge._slots.release()

        return Handler

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self._stopped.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        # Delete only our exact file and then the empty owned directory. Never
        # recursively remove something a different process might have added.
        try:
            os.remove(self.settings_path)
            os.rmdir(self._directory)
        except OSError:
            pass
