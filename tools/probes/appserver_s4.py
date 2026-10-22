"""One-turn interrupt probe. Offline by default; one bound ``turn/interrupt`` at most.

The turn is a long plain-text answer. As soon as it is visibly running (first
message delta, any tool activity or server request, or a fallback timer) the
probe sends exactly one ``turn/interrupt`` for the bound thread and turn, then
follows the turn to its own ``turn/completed``. It never answers a server
request and never sends a second interrupt, another turn or a retry.

What the model writes never decides anything. The result keeps the main turn's
terminal, tool activity, and local cleanup (own process, work directory and its
descendant processes) apart.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import appserver_s2 as S2
import appserver_s3 as S3
from agenthub_adapters.appserver_s4_evidence import S4Evidence, INTERRUPT

A = S2.A
PROMPT = ("Write a numbered list of 400 different English nouns, one per line, as plain text. "
          "Answer directly: do not use any tools, code execution, commands, files, web search or subagents, "
          "and do not ask questions.")
SCENARIOS = ("happy", "late-response", "timer", "response-no-terminal", "interrupt-error", "completed-first",
             "completed-race", "failed-terminal", "wrong-turn-terminal", "foreign-thread", "duplicate-terminal",
             "late-delta", "disconnect", "timeout", "tool-item", "server-request", "auto-review")
RUN_ROOT = S2.REPO / "var/appserver-s4"


# --------------------------------------------------------------------------- #
# descendant processes (local cleanup evidence; read-only, Windows only)
# --------------------------------------------------------------------------- #

def process_table():
    """``[(pid, parent_pid, image name)]`` for every process, or None if unavailable."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        rows = []
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return rows
    finally:
        kernel32.CloseHandle(snapshot)


def descendants(root_pid, table):
    """Processes whose parent chain reaches ``root_pid`` (the root itself excluded)."""
    children = {}
    for pid, parent, name in table:
        if pid != parent:
            children.setdefault(parent, []).append((pid, name))
    found, frontier = [], [root_pid]
    while frontier:
        for pid, name in children.get(frontier.pop(), []):
            if all(pid != seen for seen, _ in found) and pid != root_pid:
                found.append((pid, name))
                frontier.append(pid)
    return found


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #

class S4Client(S2.S2Client):
    CLIENT_NAME = "agent-hub-s4"
    PROMPT = PROMPT
    EVIDENCE_CLASS = S4Evidence
    #: Interrupt anyway this long after the turn started without visible output.
    FALLBACK_SECONDS = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings_verified = False
        self.reasons = []  # client-side codes (protocol names only, never text)
        self.interrupt_id = None
        self.turn_seen_at = None

    def _abort(self, code, message):
        if code not in self.reasons:
            self.reasons.append(code)
        if self.evidence is not None:
            self.evidence._violate(code)
        self.violation = True
        raise A.AppServerProtocolError(message)

    def thread_params(self):
        return {**super().thread_params(), "approvalPolicy": "on-request", "approvalsReviewer": "user"}

    def turn_params(self):
        return {**super().turn_params(), "approvalPolicy": "on-request", "approvalsReviewer": "user"}

    def validate_thread_settings(self, result):
        sandbox, cwd = result.get("sandbox"), result.get("cwd")
        self.settings_verified = bool(
            result.get("approvalsReviewer") == "user" and result.get("approvalPolicy") == "on-request"
            and isinstance(sandbox, dict) and sandbox.get("type") == "readOnly"
            and sandbox.get("networkAccess", False) is False
            and set(sandbox) <= {"type", "networkAccess"}
            and isinstance(cwd, str) and os.path.isabs(cwd)
            and os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(self.workdir))
        if not self.settings_verified:
            self._abort("settings_mismatch", "S4 effective settings do not match")

    def _send(self, message):
        if "result" in message or "error" in message:
            # Approvals, elicitations and auth refreshes all stay unanswered.
            self._abort("outgoing_response_rejected", "S4 never answers server requests")
        if message.get("method") == INTERRUPT:
            # The only interrupt path is send_interrupt(), which writes directly.
            self._abort("outgoing_interrupt_rejected", "S4 sends its interrupt only through send_interrupt")
        if message.get("method") == "turn/start" and not self.settings_verified:
            self._abort("settings_not_verified", "S4 effective settings not verified")
        return super()._send(message)

    def send_interrupt(self, trigger):
        """Exactly one ``turn/interrupt`` for the bound turn, without blocking the loop."""
        with self.guard:
            evidence = self.evidence
            if (evidence is None or self.interrupt_id is not None or len(self.sent) != 4
                    or self.turn_attempts != 1 or not evidence.can_interrupt() or self.violation
                    or self.disconnected or time.monotonic() >= self.deadline):
                self._abort("outgoing_interrupt_rejected", "S4 sends one bound interrupt while the turn runs")
            with self._lock:
                self._next_id += 1
                request_id = self._next_id
                self._pending[request_id] = {"event": threading.Event(), "result": None, "error": None}
            self.interrupt_id = request_id
            message = {"jsonrpc": "2.0", "id": request_id, "method": INTERRUPT,
                       "params": {"threadId": evidence.thread_id, "turnId": evidence.turn_id}}
            # Record the attempt BEFORE writing: a partial write is not retried.
            evidence.mark_interrupt_sent(trigger)
            self.sent.append({"method": INTERRUPT, "id": request_id, "trigger": trigger})
            A.AppServerClient._send(self, message)

    def _dispatch(self, message):
        if "method" in message and "id" in message:
            with self.guard:
                if self.evidence is None:
                    self._abort("server_request_before_turn", "server request before the S4 turn")
                self.evidence.record_server_request(message.get("method"))
            A.AppServerClient._dispatch(self, message)  # kept, never answered
            return
        if self.interrupt_id is not None and message.get("id") == self.interrupt_id and (
                "result" in message or "error" in message):
            with self.guard:
                error = message.get("error")
                code = (error.get("code") if isinstance(error, dict) else "?") if error else None
                self.evidence.record_interrupt_response(error_code=code)
                if not error and message.get("result") != {}:
                    self.evidence._note("interrupt_response_unexpected_shape")
            A.AppServerClient._dispatch(self, message)
            return
        super()._dispatch(message)

    def tick(self):
        evidence = self.evidence
        if evidence is None or self.interrupt_id is not None:
            return
        if evidence.turn_started and self.turn_seen_at is None:
            self.turn_seen_at = time.monotonic()
        expired = self.turn_seen_at is not None and time.monotonic() - self.turn_seen_at >= self.FALLBACK_SECONDS
        trigger = evidence.interrupt_trigger(timer_expired=expired)
        if trigger:
            self.send_interrupt(trigger)

    def exercise(self):
        try:
            super().exercise()
        finally:
            if self.evidence:
                self.evidence.settings_verified = self.settings_verified
                if self.disconnected and not self.evidence.terminal_seen and "disconnect_before_terminal" not in self.reasons:
                    self.reasons.append("disconnect_before_terminal")

    def close(self):
        """Close our own process; record its descendants before and after."""
        root = getattr(self.transport, "pid", None)
        before_table = process_table() if root else None
        before = descendants(root, before_table) if before_table is not None else None
        try:
            super().close()
        finally:
            after_table = process_table() if before is not None else None
            if self.evidence is not None:
                if before is None or after_table is None:
                    self.evidence.process_tree = {"available": False}
                else:
                    alive = {(pid, name) for pid, _, name in after_table}
                    survivors = [name for pid, name in before if (pid, name) in alive]
                    self.evidence.process_tree = {"available": True,
                                                  "descendants_before_close": sorted(n for _, n in before),
                                                  "descendants_after_close": sorted(survivors)}

    @staticmethod
    def finalize_result(result):
        """One verdict, with main turn, tool activity and local cleanup kept apart."""
        codes = list(result.get("reason_codes") or [])
        for code in result.get("client_reasons") or []:
            if code not in codes:
                codes.append(code)
        tree = result.get("process_tree") or {}
        local = {"process_started": result.get("process_started"), "process_stopped": result.get("process_stopped"),
                 "exit_code": result.get("exit_code"), "workdir_empty": result.get("workdir_empty"),
                 "workdir_removed": result.get("workdir_removed"),
                 "process_tree_available": bool(tree.get("available")),
                 "descendants_before_close": tree.get("descendants_before_close"),
                 "descendants_after_close": tree.get("descendants_after_close")}
        result["local_cleanup"] = local
        runner = []
        if result.get("workdir_empty") is False:
            runner.append("workdir_not_empty")  # the read-only run wrote something
        elif result.get("workdir_empty") is None:
            runner.append("workdir_unreadable")
        error = result.get("error_type")
        if error:
            runner.append({"AppServerTimeout": "timeout", "AppServerDisconnected": "disconnect",
                           "CheckFailed": "preflight_failed"}.get(error, f"error:{error}"))
        if result.get("process_started") and not result.get("process_stopped"):
            runner.append("process_not_stopped")
        elif result.get("process_started") and result.get("exit_code") != 0:
            runner.append(f"process_exit:{result.get('exit_code')}")
        if result.get("reader_error_type") and not error:
            runner.append(f"reader_error:{result['reader_error_type']}")
        if result.get("cleanup_error_type"):
            runner.append(f"cleanup_error:{result['cleanup_error_type']}")
        if result.get("process_started") and result.get("turn_attempts") != 1:
            runner.append(f"turn_attempts:{result.get('turn_attempts')}")
        if result.get("process_started") and not tree.get("available"):
            runner.append("process_tree_unavailable")
        if tree.get("descendants_after_close"):
            runner.append("descendants_survived_close")
        for code in runner:
            if code not in codes:
                codes.append(code)
        refused = any(c.startswith("outgoing_") or c == "server_request_before_turn"
                      for c in result.get("client_reasons") or [])
        if result.get("workdir_empty") is False or result.get("probe_verdict") == "FAIL" or refused:
            result["probe_verdict"] = "FAIL"
            result["manual_review_required"] = True
        elif result["passed"] and not tree.get("descendants_after_close") and tree.get("available"):
            result["probe_verdict"] = "PASS"
            codes = []
        else:
            result["probe_verdict"] = "INCONCLUSIVE"
        result["passed"] = result["probe_verdict"] == "PASS"
        if result["probe_verdict"] != "PASS" and not codes:
            codes.append("unclassified")  # never a silent non-PASS
        result["reason_codes"] = codes
        print(json.dumps({"probe_verdict": result["probe_verdict"], "reason_codes": codes,
                          "main_turn_status": (result.get("main_turn") or {}).get("status"),
                          "interrupt_response": (result.get("interrupt") or {}).get("response"),
                          "tool_state": (result.get("tool_activity") or {}).get("state")}))


# --------------------------------------------------------------------------- #
# offline schema facts (no binary is executed)
# --------------------------------------------------------------------------- #

def schema_facts(capture: Path) -> dict:
    """The protocol facts S4 relies on, from an existing generate-json-schema capture."""
    import appserver_s3_identity as ID
    v2 = ID._defs(ID._load(capture / "codex_app_server_protocol.v2.schemas.json"))
    client = ID._methods(ID._load(capture / "ClientRequest.json"))
    notifications = ID._methods(ID._load(capture / "ServerNotification.json"))
    params = v2.get("TurnInterruptParams") or {}
    facts = {
        "request:turn/interrupt": INTERRUPT in client,
        "interrupt_params:threadId,turnId (required)":
            {"threadId", "turnId"} <= ID._props(params) and {"threadId", "turnId"} <= set(params.get("required", [])),
        "interrupt_response:object": (v2.get("TurnInterruptResponse") or {}).get("type") == "object",
        "turn_status has inProgress,interrupted,completed,failed":
            {"inProgress", "interrupted", "completed", "failed"} <= ID._enum_values(v2.get("TurnStatus"), v2),
        "turn_completed:threadId,turn": {"threadId", "turn"} <= ID._props(v2.get("TurnCompletedNotification")),
        "turn:id,status": {"id", "status"} <= ID._props(v2.get("Turn")),
        **{f"notification:{m}": m in notifications
           for m in ("turn/started", "turn/completed", "item/agentMessage/delta", "item/started",
                     "item/completed", "serverRequest/resolved", "thread/tokenUsage/updated")},
    }
    return {"compatible": all(facts.values()), "facts_checked": len(facts),
            "missing": sorted(k for k, v in facts.items() if not v), "facts": facts}


def run_probe(**kwargs):
    return S2.run_probe(**kwargs, _client_class=S4Client, _label="S4", _scenarios=SCENARIOS,
                        _run_root=RUN_ROOT, _stub_script=S2.REPO / "tools/demo/stub_appserver_s4.py",
                        _identity=S3.S3_CODEX_IDENTITY)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=SCENARIOS, default="happy")
    parser.add_argument("--approve-one-model-turn", action="store_true")
    parser.add_argument("--acknowledge-unverified-native-control", action="store_true")
    parser.add_argument("--codex-home", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "AgentHub", "codex-home"))
    parser.add_argument("--schema-facts", metavar="CAPTURE_DIR")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(PROMPT)
        print("One turn; read-only; user reviewer; exactly one bound turn/interrupt; no answers, retries or resume.")
        print("Only the turn's own turn/completed(interrupted) after the interrupt can pass; text never decides.")
        print("Tool activity and local cleanup are reported separately; native control is unverified.")
        return 0
    if args.schema_facts:
        report = schema_facts(Path(args.schema_facts))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["compatible"] else 5
    return run_probe(real=args.mode == "real", approved=args.approve_one_model_turn,
                     acknowledge_risk=args.acknowledge_unverified_native_control,
                     codex_home=os.path.abspath(args.codex_home), scenario=args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
