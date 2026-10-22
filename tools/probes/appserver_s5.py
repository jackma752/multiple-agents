"""Two-phase resume probe. Offline by default; at most two model turns in real mode.

Phase 1 (process A): a NEW dedicated thread, one turn that plants a random code
word. Process A is closed and must have exited.
Phase 2 (process B, started only after A is gone): ``thread/resume`` of that
same thread (a cold resume from the long-term home), then one recall turn whose
prompt does not contain the code word.

PASS needs the resume response to carry the planted turn and message AND the
recall reply to contain the code word. ``thread/resume`` returning success is
never enough. The probe never answers a server request, never resumes an S2-S4
thread (that would append to their recorded rollouts) and never retries.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import appserver_s2 as S2
import appserver_s3 as S3
import appserver_s4 as S4
from agenthub_adapters.appserver_s5_evidence import (
    PLANT_PROMPT, RECALL_PROMPT, TurnEvidence, digest, inspect_resume, judge, make_code_word, plant_health)

A = S2.A
S1 = S2.S1
SCENARIOS = ("happy", "loose-recall", "forgetful", "no-history-returned", "history-without-message",
             "wrong-thread-resume", "resume-error", "resume-writable", "plant-failed", "plant-timeout",
             "same-turn-id", "foreign-turn", "late-event", "recall-tool", "recall-approval", "recall-disconnect",
             # phase-1 health counterexamples: each must keep process B from starting
             "plant-provider-error", "plant-exit-nonzero", "plant-tool",
             # usage replay on resume (revision after S5 run 1)
             "no-replay", "replay-twice", "replay-after-start", "replay-other-turn", "replay-foreign-thread")
RUN_ROOT = S2.REPO / "var/appserver-s5"
STUB = S2.REPO / "tools/demo/stub_appserver_s5.py"
MAX_MODEL_TURNS = 2
#: How long a descendant may take to exit after its parent was closed.
CLOSE_GRACE_SECONDS = 3.0


class S5Client(S2.S2Client):
    CLIENT_NAME = "agent-hub-s5"

    def __init__(self, transport, *, workdir, timeout, phase, code_word, thread_id=None, planted_turn_id=None):
        super().__init__(transport, workdir=workdir, timeout=timeout)
        self.phase = phase
        self.code_word = code_word
        self.target_thread = thread_id
        self.planted_turn_id = planted_turn_id
        self.settings_verified = False
        self.reasons = []
        self.resume = None
        self.process_tree = None

    def _abort(self, code, message):
        if code not in self.reasons:
            self.reasons.append(code)
        if self.evidence is not None:
            self.evidence._violate(code)
        self.violation = True
        raise A.AppServerProtocolError(message)

    def thread_params(self):
        return {"cwd": self.workdir, "sandbox": "read-only", "approvalPolicy": "on-request",
                "approvalsReviewer": "user"}

    def opening(self):
        if self.phase == "plant":
            return "thread/start", self.thread_params()
        return "thread/resume", {"threadId": self.target_thread, **self.thread_params()}

    def prompt(self):
        return PLANT_PROMPT.format(code=self.code_word) if self.phase == "plant" else RECALL_PROMPT

    def turn_params(self):
        return {"threadId": self.evidence.thread_id, "input": [{"type": "text", "text": self.prompt()}],
                "approvalPolicy": "on-request", "approvalsReviewer": "user",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}

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
            self._abort("settings_mismatch", "S5 effective settings do not match")

    def _send(self, message):
        with self.guard:
            if "result" in message or "error" in message:
                self._abort("outgoing_response_rejected", "S5 never answers server requests")
            index = len(self.sent)
            method, params = message.get("method"), message.get("params")
            request = "id" in message
            open_method, open_params = self.opening()
            valid = (
                index == 0 and request and method == "initialize"
                and params == {"clientInfo": {"name": self.CLIENT_NAME, "version": "0.1.0"}}
            ) or (
                index == 1 and not request and method == "initialized" and params == {}
                and self.sent[0]["id"] in self.acked
            ) or (
                index == 2 and request and method == open_method and params == open_params
                and (self.phase == "plant" or bool(self.target_thread))
            ) or (
                index == 3 and request and method == "turn/start" and self.evidence is not None
                and self.settings_verified and self.sent[2]["id"] in self.acked and params == self.turn_params()
                and RECALL_PROMPT.find(self.code_word) == -1
            )
            if not valid or self.violation or time.monotonic() >= self.deadline:
                self._abort("outgoing_rejected", "S5 rejected outgoing message before transmission")
            self.sent.append({"method": method, "id": message.get("id")})
            if method == "turn/start":
                self.turn_attempts += 1
            A.AppServerClient._send(self, message)

    def _dispatch(self, message):
        if "method" in message and "id" in message:
            with self.guard:
                if self.evidence is None:
                    self._abort("server_request_before_turn", "server request before the S5 turn")
                self.evidence.record_server_request(message.get("method"))
            A.AppServerClient._dispatch(self, message)  # kept, never answered
            return
        if "error" in message and "id" in message:
            # A refused request (e.g. resume of an unknown thread) is reported by
            # request() to the caller; it is not a protocol contradiction.
            A.AppServerClient._dispatch(self, message)
            return
        super()._dispatch(message)

    def exercise(self):
        try:
            self.start()
            A.AppServerSession(self).initialize(client_name=self.CLIENT_NAME, version="0.1.0")
            method, params = self.opening()
            try:
                result = self.request(method, params)
            except A.AppServerResponseError as exc:
                if self.phase == "recall":
                    code = getattr(exc, "code", None)
                    self.resume = {"rejected": True, "error_code": code if isinstance(code, (int, str)) else None}
                raise
            thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
            thread_id = thread.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise A.AppServerProtocolError("missing thread id")
            if self.phase == "recall":
                self.resume = inspect_resume(result, thread_id=self.target_thread,
                                             planted_turn_id=self.planted_turn_id, code_word=self.code_word)
                if not self.resume["thread_id_matches"]:
                    self._abort("resume_thread_mismatch", "thread/resume returned another thread")
            self.validate_thread_settings(result)
            with self.guard:
                self.evidence = TurnEvidence(thread_id, phase=self.phase,
                                             replay_turn_id=self.planted_turn_id if self.phase == "recall" else None)
                if any(t != thread_id for t in self.observed_threads):
                    self._abort("foreign_thread_started", "a different thread started")
            result = self.request("turn/start", self.turn_params())
            with self.guard:
                self.evidence.bind_turn((result.get("turn") or {}).get("id"))
            tail = None
            while time.monotonic() < self.deadline:
                with self.guard:
                    summary = self.evidence.summary()
                    stop = self.evidence.stop_requested
                if summary["violation"] or self.disconnected or stop:
                    break
                if summary["terminal_confirmed"]:
                    tail = tail or min(self.deadline, time.monotonic() + 1)
                    if time.monotonic() >= tail:
                        break
                time.sleep(0.01)
            else:
                if not self.evidence.summary()["terminal_confirmed"]:
                    raise A.AppServerTimeout("S5 terminal event deadline")
        finally:
            if self.evidence is not None:
                self.evidence.settings_verified = self.settings_verified

    def close(self):
        """Close our own process; record its descendants before and after (never killed by name).

        A descendant counts as surviving only if it is still there after a
        bounded grace period: a child that is merely mid-exit is not a survivor.
        """
        root = getattr(self.transport, "pid", None)
        before_table = S4.process_table() if root else None
        before = S4.descendants(root, before_table) if before_table is not None else None
        try:
            super().close()
        finally:
            survivors, waited, available = [], 0.0, before is not None
            if available and before:
                start = time.monotonic()
                while True:
                    after = S4.process_table()
                    if after is None:
                        available = False
                        break
                    alive = {(pid, name) for pid, _, name in after}
                    survivors = [n for p, n in before if (p, n) in alive]
                    waited = time.monotonic() - start
                    if not survivors or waited >= CLOSE_GRACE_SECONDS:
                        break
                    time.sleep(0.1)
            if not available:
                self.process_tree = {"available": False}
            else:
                self.process_tree = {"available": True, "grace_seconds": CLOSE_GRACE_SECONDS,
                                     "waited_seconds": round(waited, 2),
                                     "descendants_before_close": sorted(n for _, n in before),
                                     "descendants_after_close": sorted(survivors)}


def pid_alive(pid):
    table = S4.process_table()
    return None if table is None else any(p == pid for p, _, _ in table)


def run_phase(name, *, argv, env, workdir, timeout, code_word, stderr_path, thread_id=None, planted_turn_id=None):
    phase = {"phase": name, "process_started": False, "process_stopped": None, "error_type": None,
             "turn_attempts": 0}
    child = client = None
    try:
        child = A.StdioTransport(argv, env=env, cwd=str(workdir), stderr_path=str(stderr_path))
        phase.update(process_started=True, pid=child.pid, started_at=time.time())
        client = S5Client(child, workdir=str(workdir), timeout=timeout, phase=name, code_word=code_word,
                          thread_id=thread_id, planted_turn_id=planted_turn_id)
        client.exercise()
    except (A.AppServerError, OSError, ValueError, TypeError, AttributeError) as exc:
        phase["error_type"] = type(exc).__name__  # never echo remote text
    finally:
        if child:
            try:
                (client or child).close()
                phase["process_stopped"] = not child.alive and child.returncode is not None
                phase["exit_code"] = child.returncode
            except (A.AppServerError, OSError, TimeoutError) as exc:
                phase["process_stopped"] = False
                phase["cleanup_error_type"] = type(exc).__name__
            phase["ended_at"] = time.time()
        if client:
            phase.update(sent=client.sent, turn_attempts=client.turn_attempts, client_reasons=list(client.reasons),
                         client_violation=client.violation, process_tree=client.process_tree,
                         reader_error_type=type(client._error).__name__ if client._error else None)
            if name == "recall":
                phase["resume"] = client.resume
            if client.evidence is not None:
                summary = client.evidence.summary()
                phase.update(evidence=summary, thread_id=summary["thread_id"], turn_id=summary["turn_id"])
                if name == "recall":
                    contains, exact = client.evidence.recall(code_word)
                    phase["recall"] = {"contains": contains, "exact": exact}
                client.evidence._final_text = None
    return phase


def run_probe(*, real=False, approved=False, acknowledge_risk=False, codex_home="", scenario="happy",
              _code_word=None):
    if real and not (approved and acknowledge_risk):
        print("REFUSED: S5 needs separate approval of two model turns and acknowledgement of unverified native control.")
        return 2  # BEFORE files, credentials, preflight or process creation
    if scenario not in SCENARIOS or (real and scenario != "happy"):
        raise ValueError("invalid S5 scenario")
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="real-" if real else "stub-", dir=RUN_ROOT))
    workdir = Path(tempfile.mkdtemp(prefix="agenthub-s5-work-", dir=S2.WORK_ROOT))  # same cwd for both phases
    code_word = _code_word or make_code_word()
    result = {"mode": "real" if real else "stub", "passed": False, "error_type": None,
              "usage_source": "server" if real else "synthetic_fixture", "native_subagent_control": "unverified",
              "max_model_turns": MAX_MODEL_TURNS, "code_word_sha256": digest(code_word),
              "code_word_length": len(code_word), "recall_prompt_contains_code_word": code_word in RECALL_PROMPT,
              "phases": {}, "process_relation": None}
    try:
        if real:
            argv, env = S1.preflight(codex_home, S3.S3_CODEX_IDENTITY)
            argv.extend(["--disable", "multi_agent"])
            timeout = 60
        else:
            stub_home = run_dir / "stub-home"  # the stub's "disk"; outside the work directory
            stub_home.mkdir()
            argv = [sys.executable, str(STUB), "--scenario", scenario]
            env = {**os.environ, "CODEX_HOME": str(stub_home)}
            timeout = 3
        common = dict(argv=argv, env=env, workdir=workdir, timeout=timeout, code_word=code_word)
        plant = run_phase("plant", stderr_path=run_dir / "stderr-plant.log", **common)
        result["phases"]["plant"] = plant
        evidence = plant.get("evidence") or {}
        # The SAME checks the final judgment applies to phase 1, before B may start:
        # terminal and status, settings, one turn, tools, notes, own process,
        # cleanup and descendant processes.
        result["plant_health"] = health = plant_health(plant)
        if health["healthy"]:
            alive = pid_alive(plant["pid"])
            relation = {"first_pid": plant["pid"], "first_process_alive_at_check": alive,
                        "first_process_exited_before_second": False}
            if alive is False:
                recall = run_phase("recall", stderr_path=run_dir / "stderr-recall.log",
                                   thread_id=evidence["thread_id"], planted_turn_id=evidence["turn_id"], **common)
                result["phases"]["recall"] = recall
                relation.update(second_pid=recall.get("pid"), same_pid=recall.get("pid") == plant["pid"],
                                first_process_exited_before_second=bool(
                                    recall.get("process_started") and recall["started_at"] >= plant["ended_at"]))
            result["process_relation"] = relation
    except S1.CheckFailed:
        result["error_type"] = "CheckFailed"
    finally:
        try:
            result["workdir_empty"] = not any(workdir.iterdir())
        except OSError as exc:
            result["workdir_empty"] = None
            result["workdir_check_error_type"] = type(exc).__name__
        result["workdir"] = str(workdir)
        result["workdir_removed"] = False
        phases_stopped = all(p.get("process_stopped") or not p.get("process_started")
                             for p in result["phases"].values())
        if result["workdir_empty"] and phases_stopped:
            try:
                workdir.rmdir()  # own fresh directory only, never recursive
                result["workdir_removed"] = True
            except OSError as exc:
                result["cleanup_error_type"] = type(exc).__name__
        verdict, codes = judge(result)
        if result["error_type"] == "CheckFailed":
            verdict, codes = "INCONCLUSIVE", ["preflight_failed"]
        result.update(probe_verdict=verdict, reason_codes=codes, passed=verdict == "PASS",
                      manual_review_required=verdict != "PASS")
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        recall = result["phases"].get("recall") or {}
        print(json.dumps({"probe_verdict": verdict, "reason_codes": codes,
                          "history_returned": (recall.get("resume") or {}).get("history_returned"),
                          "recalled": (recall.get("recall") or {}).get("contains")}))
        print("S5 PASSED" if verdict == "PASS" else f"S5 {verdict}")
        print(f"Evidence: {run_dir / 'result.json'}")
    return 0 if result["passed"] else 1


def schema_facts(capture: Path) -> dict:
    """The protocol facts S5 relies on, from an existing generate-json-schema capture (no execution)."""
    import appserver_s3_identity as ID
    v2 = ID._defs(ID._load(capture / "codex_app_server_protocol.v2.schemas.json"))
    client = ID._methods(ID._load(capture / "ClientRequest.json"))
    notifications = ID._methods(ID._load(capture / "ServerNotification.json"))
    params, response = v2.get("ThreadResumeParams") or {}, v2.get("ThreadResumeResponse") or {}
    facts = {
        "request:thread/resume": "thread/resume" in client,
        "resume_params:threadId (required)": "threadId" in ID._props(params) and "threadId" in params.get("required", []),
        "resume_params:cwd,approvalPolicy,approvalsReviewer,sandbox,excludeTurns":
            {"cwd", "approvalPolicy", "approvalsReviewer", "sandbox", "excludeTurns"} <= ID._props(params),
        "resume_response:thread,cwd,approvalPolicy,approvalsReviewer,sandbox (required)":
            {"thread", "cwd", "approvalPolicy", "approvalsReviewer", "sandbox"} <= set(response.get("required", [])),
        "thread:id,turns (required)": {"id", "turns"} <= set((v2.get("Thread") or {}).get("required", [])),
        "turn:id,status,items": {"id", "status", "items"} <= ID._props(v2.get("Turn")),
        "thread_item has userMessage,agentMessage":
            {"userMessage", "agentMessage"} <= ID._variant_types(v2.get("ThreadItem")),
        **{f"notification:{m}": m in notifications
           for m in ("turn/started", "turn/completed", "item/completed", "thread/tokenUsage/updated")},
    }
    return {"compatible": all(facts.values()), "facts_checked": len(facts),
            "missing": sorted(k for k, v in facts.items() if not v), "facts": facts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=SCENARIOS, default="happy")
    parser.add_argument("--approve-two-model-turns", action="store_true")
    parser.add_argument("--acknowledge-unverified-native-control", action="store_true")
    parser.add_argument("--codex-home", default=os.path.join(os.environ.get("LOCALAPPDATA", ""), "AgentHub", "codex-home"))
    parser.add_argument("--schema-facts", metavar="CAPTURE_DIR")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(PLANT_PROMPT.format(code="<random S5- code word>"))
        print(RECALL_PROMPT)
        print("Two processes, one new dedicated thread, at most two model turns; process A must exit before B.")
        print("PASS needs the planted turn in the resume response AND the code word in the recall reply.")
        return 0
    if args.schema_facts:
        report = schema_facts(Path(args.schema_facts))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["compatible"] else 5
    return run_probe(real=args.mode == "real", approved=args.approve_two_model_turns,
                     acknowledge_risk=args.acknowledge_unverified_native_control,
                     codex_home=os.path.abspath(args.codex_home), scenario=args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
