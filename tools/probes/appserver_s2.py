"""Fixed one-turn protocol probe. Default is an offline Python process.

Real mode requires separate authorization for a model turn AND acknowledgement
that native child control is unverified. This is not the Hub execution adapter.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "packages/contracts"),
               str(REPO / "packages/adapters"), str(REPO / "apps/worker")]
import appserver_s1 as S1
from agenthub_adapters import appserver as A
from agenthub_adapters.appserver_s2_evidence import S2Evidence

PROMPT = "Reply with exactly: OK. Do not use tools, create subagents, or modify any files."
RUN_ROOT = REPO / "var/appserver-s2"
WORK_ROOT = Path(tempfile.gettempdir())
SCENARIOS = ("happy", "turn-failed", "no-usage", "wrong-turn", "approval", "tool", "disconnect", "timeout")


class S2Client(A.AppServerClient):
    CLIENT_NAME = "agent-hub-s2"
    PROMPT = PROMPT
    EVIDENCE_CLASS = S2Evidence
    def __init__(self, transport, *, workdir: str, timeout: float):
        super().__init__(transport, request_timeout=min(timeout, 20))
        self.workdir = workdir
        self.deadline = time.monotonic() + timeout
        self.guard = threading.RLock()
        self.sent = []
        self.acked = set()
        self.evidence = None
        self.observed_threads = []
        self.violation = False
        self.turn_attempts = 0

    def thread_params(self):
        return {"cwd": self.workdir, "sandbox": "read-only", "approvalPolicy": "untrusted"}

    def turn_params(self):
        return {"threadId": self.evidence.thread_id,
                "input": [{"type": "text", "text": self.PROMPT}],
                "approvalPolicy": "untrusted",
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}

    def _send(self, message):
        with self.guard:
            index = len(self.sent)
            method, params = message.get("method"), message.get("params")
            request = "id" in message and "result" not in message and "error" not in message
            valid = (
                index == 0 and request and method == "initialize" and params == {
                    "clientInfo": {"name": self.CLIENT_NAME, "version": "0.1.0"}}
            ) or (
                index == 1 and "id" not in message and method == "initialized" and params == {}
                and self.sent[0]["id"] in self.acked
            ) or (
                index == 2 and request and method == "thread/start" and params == self.thread_params()
            ) or (
                index == 3 and request and method == "turn/start" and self.evidence is not None
                and self.sent[2]["id"] in self.acked and params == self.turn_params()
            )
            if not valid or self.violation or time.monotonic() >= self.deadline:
                self.violation = True
                raise A.AppServerProtocolError("S2 rejected outgoing message before transmission")
            # Record attempts BEFORE writing: a partial write is not retryable.
            self.sent.append({"method": method, "id": message.get("id")})
            if method == "turn/start":
                self.turn_attempts += 1
            super()._send(message)

    def _dispatch(self, message):
        with self.guard:
            method = message.get("method")
            if "id" in message and method:
                # S2 never answers tools, approvals, elicitation or auth refresh.
                self.violation = True
                raise A.AppServerProtocolError("unexpected server request during S2")
            if "result" in message:
                self.acked.add(message.get("id"))
            if "error" in message:
                self.violation = True
            params = message.get("params") or {}
            if method == "thread/started":
                seen_thread = params.get("thread", {}).get("id")
                self.observed_threads.append(seen_thread)
                if not seen_thread or (self.evidence is not None and seen_thread != self.evidence.thread_id):
                    self.violation = True
            if isinstance(method, str) and method.startswith(("turn/", "item/")) and self.turn_attempts != 1:
                self.violation = True
            if method and self.evidence is not None:
                self.evidence.observe(method, params)
                if self.evidence.summary()["violation"]:
                    self.violation = True
            elif isinstance(method, str) and method.startswith(("turn/", "item/")):
                self.violation = True
            if self.violation:
                raise A.AppServerProtocolError("S2 evidence violation")
            super()._dispatch(message)

    def request(self, method, params=None, *, timeout=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise A.AppServerTimeout("S2 deadline")
        return super().request(method, params, timeout=min(remaining, self.request_timeout))

    def exercise(self):
        self.start()
        A.AppServerSession(self).initialize(client_name=self.CLIENT_NAME, version="0.1.0")
        result = self.request("thread/start", self.thread_params())
        self.validate_thread_settings(result)
        thread_id = result.get("thread", {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise A.AppServerProtocolError("missing thread id")
        with self.guard:
            self.evidence = self.EVIDENCE_CLASS(thread_id)
            if any(t != thread_id for t in self.observed_threads):
                self.violation = True
                raise A.AppServerProtocolError("unexpected thread before turn")
        result = self.request("turn/start", self.turn_params())
        with self.guard:
            self.evidence.bind_turn(result.get("turn", {}).get("id"))
        terminal_tail = None
        while time.monotonic() < self.deadline:
            with self.guard:
                self.tick()
                summary = self.evidence.summary()
            if summary["violation"] or self.disconnected:
                break
            if summary["terminal_confirmed"]:
                # Usage is independent and may arrive after the terminal event.
                # Keep observing a bounded tail; never send another RPC.
                if terminal_tail is None:
                    terminal_tail = min(self.deadline, time.monotonic() + 1)
                if time.monotonic() >= terminal_tail:
                    break
            time.sleep(0.01)
        else:
            if not self.evidence.summary()["terminal_confirmed"]:
                raise A.AppServerTimeout("S2 terminal event deadline")

    def tick(self):
        """S2 sends no further messages; specialized probes may restrict responses."""

    def validate_thread_settings(self, result):
        """Specialized probes may require effective settings before starting a turn."""

    def close(self):
        super().close()
        if self._reader:
            self._reader.join(timeout=1)
            if self._reader.is_alive():
                raise A.AppServerTimeout("S2 reader did not stop")


def run_probe(*, real=False, approved=False, acknowledge_risk=False, codex_home="", scenario="happy",
              _client_class=S2Client, _label="S2", _scenarios=SCENARIOS, _run_root=None, _stub_script=None,
              _identity=None):
    if real and not (approved and acknowledge_risk):
        print(f"REFUSED: {_label} needs separate one-turn approval and acknowledgement of unverified native control.")
        return 2  # BEFORE files, credentials, preflight or process creation
    if scenario not in _scenarios or (real and scenario != "happy"):
        raise ValueError(f"invalid {_label} scenario")
    run_root = _run_root or RUN_ROOT
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="real-" if real else "stub-", dir=run_root))
    # Keep model cwd outside the repository so it does not inherit repository
    # instructions merely because the diagnostic evidence lives under var/.
    workdir = Path(tempfile.mkdtemp(prefix=f"agenthub-{_label.lower()}-work-", dir=WORK_ROOT))
    result = {"mode": "real" if real else "stub", "passed": False,
              "process_started": False, "process_stopped": None,
              "terminal_confirmed": False, "verdict": "unconfirmed", "turn_attempts": 0,
              "usage_source": "server" if real else "synthetic_fixture",
              "native_subagent_control": "unverified", "error_type": None}
    child = client = None
    try:
        if real:
            # S2 itself keeps the S1 identity (None); a later probe passes its own.
            argv, env = (S1.preflight(codex_home, _identity) if _identity
                         else S1.preflight(codex_home))
            argv.extend(["--disable", "multi_agent"])
        else:
            argv = [sys.executable, str(_stub_script or REPO / "tools/demo/stub_appserver_s2.py"), "--scenario", scenario]
            env = dict(os.environ)
            env["CODEX_HOME"] = str(workdir)
        child = A.StdioTransport(argv, env=env, cwd=str(workdir), stderr_path=str(run_dir / "stderr.log"))
        result.update(process_started=True, pid=child.pid)
        client = _client_class(child, workdir=str(workdir), timeout=60 if real else 2)
        client.exercise()
    except (A.AppServerError, S1.CheckFailed, OSError, ValueError, TypeError, AttributeError) as exc:
        result["error_type"] = type(exc).__name__  # never echo remote text
    finally:
        if child:
            try:
                (client or child).close()
                result["process_stopped"] = not child.alive and child.returncode is not None
                result["exit_code"] = child.returncode
            except (A.AppServerError, OSError, TimeoutError) as exc:
                result["process_stopped"] = False
                result["cleanup_error_type"] = type(exc).__name__
        if client:
            result["sent"] = client.sent
            result["turn_attempts"] = client.turn_attempts
            result["client_violation"] = client.violation
            result["reader_error_type"] = type(client._error).__name__ if client._error else None
            if client.evidence:
                result.update(client.evidence.summary())
        try:
            result["workdir_empty"] = not any(workdir.iterdir())
        except OSError as exc:
            # Losing access to the workdir must not discard the audit record of
            # a model turn which may already have happened.
            result["workdir_empty"] = None
            result["workdir_check_error_type"] = type(exc).__name__
        result["workdir"] = str(workdir)
        result["workdir_removed"] = False
        if result["workdir_empty"] and (result["process_stopped"] or not result["process_started"]):
            try:
                workdir.rmdir()  # Own fresh directory only, never recursive deletion.
                result["workdir_removed"] = True
            except OSError as exc:
                result["cleanup_error_type"] = type(exc).__name__
        result["passed"] = bool(result["passed"] and result["process_stopped"]
                                and result.get("exit_code") == 0 and result["workdir_empty"]
                                and result["turn_attempts"] == 1 and not result["error_type"]
                                and not result.get("client_violation") and not result.get("reader_error_type")
                                and not result.get("cleanup_error_type"))
        result["manual_review_required"] = bool(result["turn_attempts"] and
                                                 (not result["terminal_confirmed"] or result.get("client_violation")
                                                  or not result["process_stopped"]))
        if result.get("workdir_check_error_type") or result.get("cleanup_error_type"):
            result["manual_review_required"] = True
        if hasattr(_client_class, "finalize_result"):
            _client_class.finalize_result(result)
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{_label} PASSED" if result["passed"] else f"{_label} {result.get('probe_verdict', 'FAILED')}")
        print(json.dumps({k: result[k] for k in ("mode", "turn_attempts", "process_stopped",
                                                "terminal_confirmed", "verdict", "manual_review_required")}))
        print(f"Evidence: {run_dir / 'result.json'}")
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=SCENARIOS, default="happy")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--approve-one-model-turn", action="store_true")
    parser.add_argument("--acknowledge-unverified-native-control", action="store_true")
    parser.add_argument("--codex-home", default=None)
    args = parser.parse_args()
    if args.list:
        print("S2: one fixed prompt, one thread, one turn, read-only; no retries or model changes.")
        print(PROMPT)
        print("Native subagents may bypass the disable flag. Detection is not prevention.")
        print("Requests for tools/approvals abort; local process exit does not prove remote cancellation.")
        print("No production adapter admission is enabled; real mode requires separate approval.")
        return 0
    home = args.codex_home or os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AgentHub", "codex-home")
    return run_probe(real=args.mode == "real", approved=args.approve_one_model_turn,
                     acknowledge_risk=args.acknowledge_unverified_native_control,
                     codex_home=os.path.abspath(home), scenario=args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
