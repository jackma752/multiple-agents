"""Scripted app-server, for offline verification of the prototype client.

Two roles:

* :class:`ScriptedAppServer` - the server logic, driven through any stream pair, so
  tests can exercise the client fully in-process (no subprocess, no pipes, no
  network, no model call);
* :func:`run_stdio` / `tools/demo/stub_appserver.py` - the same logic over stdio,
  so the client can be pointed at a real process.

Scenarios are named and selected by ``STUB_APPSERVER_SCENARIO`` (or the constructor
argument), never by the request text, so the protocol a reviewer reads is the same
one the client always speaks. Each scenario exists to pin ONE behaviour:

===================  ==========================================================
``happy``            thread + turn, delta stream, terminal completion
``approval-allow``   a command approval request, answered ``accept``
``approval-decline`` a command approval request, answered ``decline``
``approval-silent``  a request nobody answers (must not be treated as approval)
``interrupt-accepted`` interrupt answered OK, but NO terminal event ever arrives
``interrupt-terminal`` interrupt answered OK, then a terminal cancelled event
``disconnect``       the stream ends mid-turn
``slow-terminal``    responses arrive, the terminal event never does
``duplicate-ids``    a duplicate/late response with an id nobody waits for
``out-of-order``     notifications before the response to the request that caused them
``framing-lsp``      speaks LSP framing instead of line-delimited JSON
``stream``           several deltas, a foreign-turn delta, final item with text
``demo``             paced deltas, then a command approval request; the closing
                     text reports the answer it received (for the chat page).
                     An interrupt while it waits ends the turn ``interrupted``
``native-spawn``     a native ``spawnAgent`` collab call (child thread), then OK;
                     the child never reports its own end
``native-spawn-finished`` the same, plus the child's own ``completed`` activity
``approval-hold``    a command approval request; records any answer that ever
                     arrives (a leaked accept is visible) and never ends the
                     turn; interrupts are acknowledged and ignored
``complete-on-interrupt`` the turn completes normally the moment an interrupt
                     arrives (completion wins the race)
``approval-default`` alpha.11's default decision list (accept / cancel, no
                     decline) and a PowerShell-wrapped command (S3 shape); the
                     answer it receives decides the turn's ending
``approval-file``    a proposed file diff followed by one file-change approval
``settings-mismatch`` thread/start reports a workspace-write sandbox
``resume-no-history`` thread/resume returns no earlier turns
``replay-twice``     resume sends the previous turn's usage twice
``replay-other-turn`` resume sends usage owned by an unknown turn
``replay-after-start`` the previous turn's usage arrives after turn/started
``tool-unfinished``  a commandExecution item with a process id starts and never
                     completes before the turn does
``tool-output-only`` command output notifications with no item lifecycle
``unbound-delta``    a delta without threadId, then a normal reply
``recall``           S6-3: a fresh thread replies STORED; a resumed one answers
                     the code word found in the stored earlier turn
``recall-forgetful`` the resumed turn answers UNKNOWN
``recall-tool``      the resumed turn runs a command item before answering
``recall-native``    the resumed turn spawns a native child (no end evidence)
``plant-tool``       the planting turn runs a command item
``recall-same-turn-id`` the resumed turn reuses the previous turn id
``recall-wrong-thread`` thread/resume answers with another thread id
``recall-replay-twice`` the resume sends the previous turn's usage twice
``recall-failed``    the resumed turn answers the code word, then ends failed
``recall-failed-nocode`` the resumed turn answers UNKNOWN, then ends failed
``usage-multi-request`` two model requests in one turn (S3 run 3's real numbers)
``usage-missed-update`` the same two requests; the first one's update never comes
``usage-duplicate``  the turn's one usage update is delivered twice
``usage-none``       a completed turn with no usage update at all
``usage-late``       the usage update arrives after turn/completed
``usage-cache-write`` a non-zero ``cacheWriteInputTokens`` (meaning unverified)
``native-spawn-usage`` ``native-spawn`` plus a usage update of the child thread
===================  ==========================================================

Thread answers echo the requested cwd and report ``sandbox`` as the object the
tested build returns (``{"type": "readOnly", "networkAccess": false}``) when
``read-only`` was asked for. A resume returns the turns this factory issued
earlier on the thread and, like the tested build (S5), one restored
``thread/tokenUsage/updated`` owned by the previous turn. The stub keeps no
conversation content.

Usage follows the real protocol (docs/t0-usage-baseline.md): one
``thread/tokenUsage/updated`` per model request, before ``turn/completed``,
with ``last`` (that request) and ``total`` (the thread so far, continued
across a resume); ``turn/completed`` carries none. An interrupted turn reports
none (the one real interrupt, S4, had none). Numbers are synthetic except the
S3 pair.

:class:`InMemoryStubFactory` and :class:`StdioStubFactory` are the two trusted
transport factories the app-server adapter admits as offline simulation.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, Optional

from agenthub_adapters.appserver import (
    APPROVAL_METHODS,
    DELTA_AGENT_MESSAGE,
    DELTA_COMMAND_OUTPUT,
    FRAMING_LSP,
    ITEM_COMPLETED,
    ITEM_STARTED,
    QueueStream,
    THREAD_START,
    THREAD_RESUME,
    THREAD_STARTED,
    TURN_COMPLETED,
    TURN_INTERRUPT,
    TURN_START,
    TURN_STARTED,
    TURN_STATUS_COMPLETED,
    TURN_STATUS_FAILED,
    TURN_STATUS_INTERRUPTED,
    encode_message,
    read_message,
)

SCENARIOS = (
    "happy",
    "approval-allow",
    "approval-decline",
    "approval-silent",
    "interrupt-accepted",
    "interrupt-terminal",
    "disconnect",
    "slow-terminal",
    "duplicate-ids",
    "out-of-order",
    "turn-failed",
    "framing-lsp",
    "stream",
    "demo",
    "native-spawn",
    "native-spawn-finished",
    "approval-hold",
    "complete-on-interrupt",
    "approval-default",
    "approval-file",
    "settings-mismatch",
    "resume-no-history",
    "replay-twice",
    "replay-other-turn",
    "replay-after-start",
    "tool-unfinished",
    "tool-output-only",
    "unbound-delta",
    "recall",
    "recall-forgetful",
    "recall-tool",
    "recall-native",
    "plant-tool",
    "recall-same-turn-id",
    "recall-wrong-thread",
    "recall-replay-twice",
    "recall-failed",
    "recall-failed-nocode",
    "usage-multi-request",
    "usage-missed-update",
    "usage-duplicate",
    "usage-none",
    "usage-late",
    "usage-cache-write",
    "native-spawn-usage",
)

#: Two-call memory scenarios (S6-3): the first call plants, the resumed one recalls.
RECALL_SCENARIOS = frozenset({
    "recall", "recall-forgetful", "recall-tool", "recall-native", "plant-tool",
    "recall-same-turn-id", "recall-wrong-thread", "recall-replay-twice",
    "recall-failed", "recall-failed-nocode",
})

#: ``approval-default``: the S3 shape of a wrapped command and the tested
#: build's default decision list.
DEFAULT_DECISIONS = ["accept", "cancel"]
WRAPPED_COMMAND = ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                   "-Command 'Get-ChildItem -Name'")
USAGE_FIELDS = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
                "outputTokens", "reasoningOutputTokens", "totalTokens")


def stub_turn_usage(n: int) -> dict:
    """Synthetic usage of a stub thread's n-th turn (TokenUsageBreakdown shape)."""
    inp, out = 100 + 10 * n, 2 + n
    return {"inputTokens": inp, "cachedInputTokens": 64, "cacheWriteInputTokens": 0,
            "outputTokens": out, "reasoningOutputTokens": 0, "totalTokens": inp + out}


def add_usage(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in USAGE_FIELDS}


def stub_thread_total(turns: int) -> dict:
    """The thread total after ``turns`` stub turns (all zero for none)."""
    total = {k: 0 for k in USAGE_FIELDS}
    for n in range(1, turns + 1):
        total = add_usage(total, stub_turn_usage(n))
    return total


#: What a resume after one earlier turn restores as ``last`` (the tested build
#: replays the previous turn's usage, S5).
REPLAY_USAGE = stub_turn_usage(1)
#: S3 run 3: one turn, two model requests (rollout token_count, T-0 §4.1).
S3_REQUESTS = (
    {"inputTokens": 15211, "cachedInputTokens": 12416, "cacheWriteInputTokens": 0,
     "outputTokens": 72, "reasoningOutputTokens": 0, "totalTokens": 15283},
    {"inputTokens": 15328, "cachedInputTokens": 12416, "cacheWriteInputTokens": 0,
     "outputTokens": 7, "reasoningOutputTokens": 0, "totalTokens": 15335},
)
#: ``native-spawn-usage``: what the child thread reports (never the parent's).
CHILD_USAGE = {"inputTokens": 900, "cachedInputTokens": 0, "cacheWriteInputTokens": 0,
               "outputTokens": 9, "reasoningOutputTokens": 0, "totalTokens": 909}

#: Deltas of the ``stream`` scenario; the final item text is their join.
STREAM_PIECES = ("Hel", "lo, ", "流式", " world")

#: ``demo``: what is streamed before the tool request, the command it asks to
#: run, and the closing text per answer. Fixed text - the stub has no model and
#: never looks at the user's input.
DEMO_INTRO = ("（离线桩，无模型）", "我先看一下", "工作目录里", "有哪些文件。\n")
DEMO_COMMAND = "dir C:\\stub"
DEMO_OUTCOME = {
    "accept": ("命令已获批准，", "桩模拟执行完毕。"),
    "decline": ("你拒绝了这个命令，", "我不会执行它，", "本轮到此结束。"),
}
DEMO_OTHER = ("没有收到批准，", "命令未执行。")
DEMO_DELAY = 0.25

#: The approval request this stub exercises. The others in APPROVAL_METHODS behave
#: identically on the wire, so one is enough to pin the client's behaviour.
APPROVAL_REQUEST = "item/commandExecution/requestApproval"


class ScriptedAppServer:
    """Deterministic app-server behaviour over a pair of byte streams."""

    def __init__(self, scenario: str, *, framing: str = "lines",
                 thread_id: str = "stub-thread-0001",
                 turn_id: str = "stub-turn-0001",
                 on_sent: Optional[Callable[[dict], None]] = None,
                 history: Optional[list[dict]] = None,
                 state_path: Optional[str] = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; known: {list(SCENARIOS)}")
        self.scenario = scenario
        self.framing = framing
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.on_sent = on_sent
        #: Optional file standing in for the server's thread store, so a later
        #: stub PROCESS can return earlier turns WITH their user message (offline
        #: stand-in for a cold resume). Kept beside the work dirs, never inside.
        self.state_path = state_path
        stored = self._load_state()
        #: Earlier turns of this thread, returned on resume.
        self.history = stored if stored else [dict(t) for t in (history or [])]
        self.turn_started = False
        self.resumed = False
        self.current_input = ""
        self.sent: list[dict] = []
        self.received: list[dict] = []
        self._answered_approvals: list[dict] = []
        self._lock = threading.Lock()
        self._next_server_id = 0
        self._stop = threading.Event()
        self.turn_status = "inProgress"
        self._interrupt_accepted = False
        self._interrupt_declined = False
        self._interrupted = threading.Event()  # demo: interrupt seen while waiting
        #: Handshake enforcement (offline stand-in for the real server's protocol
        #: order): a thread/turn request before `initialized` is refused on the wire.
        self.initialize_seen = False
        self.initialized_seen = False
        self.handshake_violations: list[str] = []

    # -- wiring -------------------------------------------------------------
    def attach(self, to_client: QueueStream, from_client: QueueStream) -> "ScriptedAppServer":
        self._out = to_client
        self._in = from_client
        return self

    def send(self, message: dict) -> None:
        with self._lock:
            self.sent.append(message)
        if self.on_sent is not None:
            self.on_sent(message)
        self._out.write(encode_message(message, self._effective_framing()))

    def _effective_framing(self) -> str:
        return FRAMING_LSP if self.scenario == "framing-lsp" else self.framing

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def reply(self, request_id: Any, result: Optional[dict] = None,
              error: Optional[dict] = None) -> None:
        message: dict = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        self.send(message)

    def request(self, method: str, params: dict) -> Any:
        """Server->client request (an approval). Returns the id we used."""
        self._next_server_id += 1
        request_id = f"srv-{self._next_server_id}"
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    # -- main loop ----------------------------------------------------------
    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                message, _framing = read_message(self._in, self.framing)
            except Exception:  # noqa: BLE001 - EOF or a broken client ends the run
                return
            self.received.append(message)
            try:
                self.handle(message)
            except Exception as exc:  # noqa: BLE001 - a scripted failure must not hang
                if "id" in message:
                    self.reply(message["id"], error={"code": -32603, "message": str(exc)})

    def stop(self) -> None:
        self._stop.set()

    # -- scripted behaviour -------------------------------------------------
    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        # Client notification: the protocol defines exactly one, `initialized`.
        if method == "initialized" and "id" not in message:
            self.initialized_seen = True
            return

        if method == "initialize":
            self.initialize_seen = True
            self.reply(request_id, {"serverInfo": {"name": "stub-appserver", "version": "0.0.1"}})
            return

        # Handshake gate: thread/turn requests before `initialized` are refused,
        # exactly as a protocol-conforming server would.
        needs_handshake = (THREAD_START, THREAD_RESUME, TURN_START, TURN_INTERRUPT, "thread/fork")
        if method in needs_handshake and not (self.initialize_seen and self.initialized_seen):
            self.handshake_violations.append(str(method))
            self.reply(request_id, error={
                "code": -32002,
                "message": f"{method} before the handshake completed "
                           "(initialize request + initialized notification)",
            })
            return

        if method in (THREAD_START, THREAD_RESUME):
            resume = method == THREAD_RESUME
            turns = [] if not resume or self.scenario == "resume-no-history" else list(self.history)
            thread_id = self.thread_id
            if resume and self.scenario == "recall-wrong-thread":
                thread_id = "stub-thread-other"
            thread = {"id": thread_id, "turns": turns}
            # Schema: the notification carries `thread` (no top-level threadId).
            self.notify(THREAD_STARTED, {"thread": thread})
            sandbox: Any = "read-only"
            if params.get("sandbox") == "read-only":
                sandbox = {"type": "readOnly", "networkAccess": False}
            if self.scenario == "settings-mismatch":
                sandbox = {"type": "workspaceWrite", "networkAccess": False}
            self.reply(request_id, {
                "thread": thread,
                "cwd": params.get("cwd") or "C:\\stub",
                "model": "stub-model",
                "modelProvider": "stub",
                "sandbox": sandbox,
                "approvalPolicy": params.get("approvalPolicy") or "on-request",
                "approvalsReviewer": params.get("approvalsReviewer") or "user",
            })
            if resume:
                self.resumed = True
                previous = self.history[-1]["id"] if self.history else None
                if previous and self.scenario != "replay-after-start":
                    owner = "stub-turn-unknown" if self.scenario == "replay-other-turn" else previous
                    twice = self.scenario in ("replay-twice", "recall-replay-twice")
                    for _ in range(2 if twice else 1):
                        self._replay_usage(owner)
            return

        if method == TURN_START:
            self.turn_status = "inProgress"
            self.turn_started = True
            pieces = params.get("input") or []
            self.current_input = "".join(p.get("text") or "" for p in pieces if isinstance(p, dict))
            if self.scenario == "recall-same-turn-id" and self.resumed and self.history:
                self.turn_id = self.history[-1]["id"]  # a server that reuses the previous id
            self.notify(TURN_STARTED, {"threadId": self.thread_id,
                                       "turn": {"id": self.turn_id, "items": [],
                                                "status": self.turn_status}})
            if self.scenario == "replay-after-start" and self.resumed and self.history:
                self._replay_usage(self.history[-1]["id"])
            if self.scenario == "out-of-order":
                # A notification may legitimately precede the response to the request
                # that caused it; the client must cope either way.
                self.notify(DELTA_AGENT_MESSAGE,
                            {"turnId": self.turn_id, "itemId": "item-1",
                             "threadId": self.thread_id, "delta": "before-response"})
            self.reply(request_id, {"turn": {"id": self.turn_id, "items": [],
                                             "status": self.turn_status}})
            self._drive_turn()
            return

        if method == TURN_INTERRUPT:
            # Schema: the response is an EMPTY object with no guarantee. The outcome
            # only ever arrives as a later turn/completed with status interrupted.
            if self.scenario == "interrupt-accepted":
                self.reply(request_id, {})
                self._interrupt_accepted = True
                return
            if self.scenario == "interrupt-terminal":
                self.reply(request_id, {})
                self._finish_turn(TURN_STATUS_INTERRUPTED)
                return
            if self.scenario == "demo":
                self.reply(request_id, {})
                self._interrupted.set()  # _drive_demo ends the turn interrupted
                return
            if self.scenario == "complete-on-interrupt":
                self.reply(request_id, {})
                item = {"id": "item-1", "type": "agentMessage", "text": "done"}
                self.notify(ITEM_COMPLETED, {"item": item, "threadId": self.thread_id,
                                             "turnId": self.turn_id})
                self._finish_turn(TURN_STATUS_COMPLETED)  # completion won the race
                return
            self.reply(request_id, {})
            self._interrupt_declined = True
            return

        if str(method).startswith("__"):
            # A realistic server errors on an unknown method.
            self.reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})
            return

        # Any other request: acknowledge so the client is not left hanging.
        self.reply(request_id, {})

    def _replay_usage(self, turn_id: str) -> None:
        """The restored usage of the previous turn: its ``last``, the thread total."""
        earlier = len(self.history)
        self._usage(stub_turn_usage(earlier), stub_thread_total(earlier), turn_id=turn_id)

    def _usage(self, last: dict, total: dict, *, turn_id: Optional[str] = None,
               thread_id: Optional[str] = None) -> None:
        self.notify("thread/tokenUsage/updated", {
            "threadId": thread_id or self.thread_id, "turnId": turn_id or self.turn_id,
            "tokenUsage": {"last": dict(last), "total": dict(total), "modelContextWindow": 475000},
        })

    def _report_usage(self) -> None:
        """This turn's usage updates (one per model request), per scenario."""
        scenario = self.scenario
        if scenario in ("usage-none", "usage-late"):
            return
        base = stub_thread_total(len(self.history)) if self.resumed else stub_thread_total(0)
        if scenario in ("usage-multi-request", "usage-missed-update", "approval-default"):
            first, second = S3_REQUESTS
            after_first = add_usage(base, first)
            if scenario != "usage-missed-update":
                self._usage(first, after_first)
            self._usage(second, add_usage(after_first, second))
            return
        own = stub_turn_usage(len(self.history) + 1 if self.resumed else 1)
        if scenario == "usage-cache-write":
            own = {**own, "cacheWriteInputTokens": 5}
        self._usage(own, add_usage(base, own))
        if scenario == "usage-duplicate":
            self._usage(own, add_usage(base, own))

    def _load_state(self) -> list[dict]:
        if not self.state_path:
            return []
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                threads = json.load(fh)
        except (OSError, ValueError):
            return []
        return [dict(t) for t in (threads.get(self.thread_id) or []) if isinstance(t, dict)]

    def _save_turn(self, status: str) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                threads = json.load(fh)
        except (OSError, ValueError):
            threads = {}
        turns = [t for t in threads.get(self.thread_id) or [] if t.get("id") != self.turn_id]
        turns.append({"id": self.turn_id, "status": status, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": self.current_input}]}]})
        threads[self.thread_id] = turns
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(threads, fh)

    def _recalled_code(self) -> Optional[str]:
        """What a model with this history would answer: a code word from an earlier turn."""
        import re

        for turn in self.history:
            for item in turn.get("items") or []:
                found = re.search(r"S\d-[A-Z0-9]{10}", json.dumps(item))
                if found:
                    return found.group(0)
        return None

    def _drive_recall(self) -> None:
        """Plant (a fresh thread) acknowledges; recall (a resumed one) answers from history."""
        scenario = self.scenario
        if not self.resumed:
            if scenario == "plant-tool":
                self._emit_item("item-cmd-1", "commandExecution")
            reply = "STORED"
        else:
            if scenario == "recall-tool":
                self._emit_item("item-cmd-1", "commandExecution")
            if scenario == "recall-native":
                spawn = {"id": "item-collab-1", "type": "collabAgentToolCall", "tool": "spawnAgent",
                         "receiverThreadIds": ["child-thread-1"], "agentsStates": {},
                         "status": "completed"}
                self.notify(ITEM_COMPLETED, {"item": spawn, "threadId": self.thread_id,
                                             "turnId": self.turn_id})
            forgets = scenario in ("recall-forgetful", "recall-failed-nocode")
            code = None if forgets else self._recalled_code()
            reply = code or "UNKNOWN"
        self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                          "threadId": self.thread_id, "delta": reply})
        self._emit_item("item-1", "agentMessage")
        failed = self.resumed and scenario in ("recall-failed", "recall-failed-nocode")
        self._finish_turn(TURN_STATUS_FAILED if failed else TURN_STATUS_COMPLETED)

    def _finish_turn(self, status: str) -> None:
        self._save_turn(status)
        self.turn_status = status
        if status != TURN_STATUS_INTERRUPTED:
            self._report_usage()
        # Schema: Turn has no usage field; usage only ever comes as tokenUsage/updated.
        self.notify(TURN_COMPLETED, {
            "threadId": self.thread_id,
            "turn": {"id": self.turn_id, "items": [], "status": status},
        })
        if self.scenario == "usage-late":
            own = stub_turn_usage(len(self.history) + 1 if self.resumed else 1)
            base = stub_thread_total(len(self.history)) if self.resumed else stub_thread_total(0)
            self._usage(own, add_usage(base, own))

    def _emit_item(self, item_id: str, kind: str) -> None:
        payload = {"id": item_id, "type": kind}
        self.notify(ITEM_STARTED, {"item": payload, "startedAtMs": 1,
                                   "threadId": self.thread_id, "turnId": self.turn_id})
        self.notify(ITEM_COMPLETED, {"item": payload, "completedAtMs": 2,
                                     "threadId": self.thread_id, "turnId": self.turn_id})

    def _drive_turn(self) -> None:
        scenario = self.scenario
        if scenario in ("approval-allow", "approval-decline", "approval-silent"):
            self._drive_approval()
            return
        if scenario == "interrupt-accepted" or scenario == "interrupt-terminal":
            # The turn stays open: only an interrupt may end it (or nothing at all).
            self.notify(DELTA_AGENT_MESSAGE,
                        {"turnId": self.turn_id, "itemId": "item-1",
                         "threadId": self.thread_id, "delta": "working"})
            return
        if scenario == "disconnect":
            self.notify(DELTA_AGENT_MESSAGE,
                        {"turnId": self.turn_id, "itemId": "item-1",
                         "threadId": self.thread_id, "delta": "half"})
            self.sent.append({"__script__": "disconnect"})
            self._out.close()
            return
        if scenario == "slow-terminal":
            self.notify(DELTA_AGENT_MESSAGE,
                        {"turnId": self.turn_id, "itemId": "item-1",
                         "threadId": self.thread_id, "delta": "waiting"})
            return
        if scenario == "turn-failed":
            self._finish_turn(TURN_STATUS_FAILED)
            return
        if scenario in ("approval-default", "approval-file"):
            self._drive_default_approval()
            return
        if scenario in RECALL_SCENARIOS:
            self._drive_recall()
            return
        if scenario == "tool-unfinished":
            # Ran without asking (sandboxed) and never reported its end.
            cmd = {"id": "item-cmd-9", "type": "commandExecution", "processId": "4242",
                   "status": "inProgress"}
            self.notify(ITEM_STARTED, {"item": cmd, "threadId": self.thread_id, "turnId": self.turn_id})
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                              "threadId": self.thread_id, "delta": "OK"})
            self._emit_item("item-1", "agentMessage")
            self._finish_turn(TURN_STATUS_COMPLETED)
            return
        if scenario == "tool-output-only":
            # Output notifications with no item lifecycle at all (and one for
            # another thread); the text must never be kept by the client.
            for target in (self.thread_id, self.thread_id, "other-thread"):
                self.notify(DELTA_COMMAND_OUTPUT, {"threadId": target, "turnId": self.turn_id,
                                                   "itemId": "item-cmd-7", "delta": "tool-output-secret"})
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                              "threadId": self.thread_id, "delta": "OK"})
            self._emit_item("item-1", "agentMessage")
            self._finish_turn(TURN_STATUS_COMPLETED)
            return
        if scenario == "unbound-delta":
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                              "delta": "UNBOUND"})
        if scenario == "demo":
            self._drive_demo()
            return
        if scenario == "approval-hold":
            request_id = self.request(APPROVAL_REQUEST, {
                "itemId": "item-cmd-1", "startedAtMs": 1,
                "threadId": self.thread_id, "turnId": self.turn_id,
                "command": "echo hold", "cwd": "C:\\stub",
                "availableDecisions": ["accept", "decline", "cancel"],
            })
            end = time.monotonic() + 30.0
            while time.monotonic() < end and not self._stop.is_set():
                answer = self._wait_for_response(request_id, timeout=0.2)
                if answer is not None:
                    decision = (answer.get("result") or {}).get("decision") or "error"
                    self._answered_approvals.append({"id": request_id, "decision": decision})
                    self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                                      "threadId": self.thread_id,
                                                      "delta": f"after-{decision}"})
                    return  # the turn stays open; only the client's deadline ends it
            return
        if scenario == "complete-on-interrupt":
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                              "threadId": self.thread_id, "delta": "working"})
            return
        if scenario in ("native-spawn", "native-spawn-finished", "native-spawn-usage"):
            spawn = {"id": "item-collab-1", "type": "collabAgentToolCall", "tool": "spawnAgent",
                     "senderThreadId": self.thread_id, "receiverThreadIds": ["child-thread-1"],
                     "agentsStates": {}, "status": "inProgress",
                     "prompt": "child prompt text that must not be copied"}
            self.notify(ITEM_STARTED, {"item": spawn, "threadId": self.thread_id, "turnId": self.turn_id})
            # The spawn CALL completing says nothing about the child itself.
            self.notify(ITEM_COMPLETED, {"item": {**spawn, "status": "completed"},
                                         "threadId": self.thread_id, "turnId": self.turn_id})
            if scenario == "native-spawn-finished":
                activity = {"id": "item-sub-1", "type": "subAgentActivity",
                            "agentThreadId": "child-thread-1", "agentPath": "/root/child",
                            "kind": "completed"}
                self.notify(ITEM_COMPLETED, {"item": activity, "threadId": self.thread_id,
                                             "turnId": self.turn_id})
            if scenario == "native-spawn-usage":
                # The child's own spending, on the parent's connection.
                self._usage(CHILD_USAGE, CHILD_USAGE, thread_id="child-thread-1", turn_id="child-turn-1")
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                              "threadId": self.thread_id, "delta": "OK"})
            self._emit_item("item-1", "agentMessage")
            self._finish_turn(TURN_STATUS_COMPLETED)
            return
        if scenario == "stream":
            item = {"id": "item-msg", "type": "agentMessage"}
            self.notify(ITEM_STARTED, {"item": item, "threadId": self.thread_id,
                                       "turnId": self.turn_id})
            for piece in STREAM_PIECES[:2]:
                self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-msg",
                                                  "threadId": self.thread_id, "delta": piece})
            # Another turn's output must never be attributed to this call.
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": "foreign-turn", "itemId": "item-x",
                                              "threadId": self.thread_id, "delta": "FOREIGN"})
            for piece in STREAM_PIECES[2:]:
                self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-msg",
                                                  "threadId": self.thread_id, "delta": piece})
            self.notify(ITEM_COMPLETED, {"item": {**item, "text": "".join(STREAM_PIECES)},
                                         "threadId": self.thread_id, "turnId": self.turn_id})
            self._finish_turn(TURN_STATUS_COMPLETED)
            return
        self.notify(DELTA_AGENT_MESSAGE,
                    {"turnId": self.turn_id, "itemId": "item-1",
                     "threadId": self.thread_id, "delta": "OK"})
        self._emit_item("item-1", "agentMessage")
        self._finish_turn(TURN_STATUS_COMPLETED)
        if scenario == "duplicate-ids":
            # A late duplicate response: nobody is waiting for this id.
            self.reply("srv-late", {"turn": {"id": self.turn_id, "items": [],
                                             "status": TURN_STATUS_COMPLETED}})

    def _drive_approval(self) -> None:
        self._next_server_id += 1
        request_id = f"srv-{self._next_server_id}"
        # Schema-required fields: itemId, startedAtMs, threadId, turnId.
        self.send({"jsonrpc": "2.0", "id": request_id, "method": APPROVAL_REQUEST,
                   "params": {
                       "itemId": "item-cmd-1",
                       "startedAtMs": 1,
                       "threadId": self.thread_id,
                       "turnId": self.turn_id,
                       "command": "echo stub",
                       "cwd": "C:\\stub",
                       "availableDecisions": ["accept", "decline", "cancel"],
                   }})
        if self.scenario == "approval-silent":
            # Nothing is expected: the point is that the CLIENT must not read
            # silence as approval, and must not block forever either.
            return
        # Wait for the client's answer before finishing the turn, so a turn that
        # completes without an answer is unambiguously the client's bug.
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            answer = self._wait_for_response(request_id, timeout=0.05)
            if answer is None:
                continue
            result = answer.get("result") or {}
            decision = result.get("decision") or (answer.get("error") or {}).get("message")
            self._answered_approvals.append({"id": request_id, "decision": decision})
            if decision == "accept":
                self.notify(DELTA_AGENT_MESSAGE,
                            {"turnId": self.turn_id, "itemId": "item-cmd-1",
                             "threadId": self.thread_id, "delta": "ran\n"})
                self._emit_item("item-cmd-1", "commandExecution")
                self._finish_turn(TURN_STATUS_COMPLETED)
            elif decision == "decline":
                self._finish_turn(TURN_STATUS_COMPLETED)
            else:
                self._finish_turn(TURN_STATUS_INTERRUPTED)
            return

    def _drive_default_approval(self) -> None:
        """S3 shape: pending item, then the request with the default list (no decline)."""
        file_change = self.scenario == "approval-file"
        cmd = {"id": "item-cmd-1", "type": "fileChange" if file_change else "commandExecution",
               "status": "inProgress", "processId": None}
        if file_change:
            cmd["changes"] = [{"path": "C:/stub/note.txt", "kind": {"type": "add"},
                               "diff": "--- /dev/null\n+++ note.txt\n@@ -0,0 +1 @@\n+offline approved content"}]
        else:
            cmd["command"] = WRAPPED_COMMAND
        self.notify(ITEM_STARTED, {"item": cmd, "threadId": self.thread_id, "turnId": self.turn_id})
        params = {
            "itemId": "item-cmd-1", "startedAtMs": 1, "threadId": self.thread_id,
            "turnId": self.turn_id, "cwd": "C:\\stub",
            "reason": "修改文件（离线桩）" if file_change else "列出目录（离线桩）", "environmentId": "local",
        }
        if not file_change:
            params.update(command=WRAPPED_COMMAND, availableDecisions=list(DEFAULT_DECISIONS))
        request_id = self.request("item/fileChange/requestApproval" if file_change else APPROVAL_REQUEST, params)
        decision = None
        end = time.monotonic() + 30.0
        while decision is None and time.monotonic() < end and not self._stop.is_set():
            answer = self._wait_for_response(request_id, timeout=0.1)
            if answer is not None:
                decision = (answer.get("result") or {}).get("decision") or "error"
        if decision is None:
            return  # unanswered: the turn stays open; the client's deadline decides
        self._answered_approvals.append({"id": request_id, "decision": decision})
        if decision == "cancel":
            self._finish_turn(TURN_STATUS_INTERRUPTED)
            return
        status = "completed" if decision == "accept" else "declined"
        self.notify(ITEM_COMPLETED, {"item": {**cmd, "status": status}, "threadId": self.thread_id,
                                     "turnId": self.turn_id})
        self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": "item-1",
                                          "threadId": self.thread_id, "delta": status.upper()})
        self._emit_item("item-1", "agentMessage")
        self._finish_turn(TURN_STATUS_COMPLETED)

    def _say(self, item_id: str, pieces: tuple[str, ...]) -> str:
        for piece in pieces:
            self.notify(DELTA_AGENT_MESSAGE, {"turnId": self.turn_id, "itemId": item_id,
                                              "threadId": self.thread_id, "delta": piece})
            time.sleep(DEMO_DELAY)
        return "".join(pieces)

    def _drive_demo(self) -> None:
        """Paced output, one command request, and a reply that matches the answer."""
        item = {"id": "item-msg", "type": "agentMessage"}
        self.notify(ITEM_STARTED, {"item": item, "threadId": self.thread_id,
                                   "turnId": self.turn_id})
        text = self._say("item-msg", DEMO_INTRO)
        request_id = self.request(APPROVAL_REQUEST, {
            "itemId": "item-cmd-1", "startedAtMs": 1,
            "threadId": self.thread_id, "turnId": self.turn_id,
            "command": DEMO_COMMAND, "cwd": "C:\\stub",
            "reason": "列出工作目录（离线桩演示）",
            "availableDecisions": ["accept", "decline", "cancel"],
        })
        decision = None
        end = time.monotonic() + 600.0  # the client's card expiry decides first
        while (decision is None and time.monotonic() < end and not self._stop.is_set()
               and not self._interrupted.is_set()):
            answer = self._wait_for_response(request_id, timeout=0.2)
            if answer is not None:
                decision = (answer.get("result") or {}).get("decision") or "error"
        if self._interrupted.is_set() and decision is None:
            # The request is abandoned; the turn ends interrupted with what was said.
            self.notify(ITEM_COMPLETED, {"item": {**item, "text": text},
                                         "threadId": self.thread_id, "turnId": self.turn_id})
            self._finish_turn(TURN_STATUS_INTERRUPTED)
            return
        self._answered_approvals.append({"id": request_id, "decision": decision})
        if decision == "accept":
            cmd = {"id": "item-cmd-1", "type": "commandExecution"}
            self.notify(ITEM_STARTED, {"item": cmd, "threadId": self.thread_id,
                                       "turnId": self.turn_id})
            self.notify(ITEM_COMPLETED, {"item": cmd, "threadId": self.thread_id,
                                         "turnId": self.turn_id})
        text += self._say("item-msg", DEMO_OUTCOME.get(decision or "", DEMO_OTHER))
        self.notify(ITEM_COMPLETED, {"item": {**item, "text": text},
                                     "threadId": self.thread_id, "turnId": self.turn_id})
        self._finish_turn(TURN_STATUS_COMPLETED)

    def _wait_for_response(self, request_id: Any, *, timeout: float) -> Optional[dict]:
        """Read client messages for up to ``timeout`` seconds, looking for one answer."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                message, _framing = read_message(self._in, self.framing)
            except Exception:  # noqa: BLE001 - EOF from the client
                self._stop.set()  # the client is gone; stop instead of spinning
                return None
            self.received.append(message)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            self.handle(message)
            if self._interrupted.is_set():
                return None  # don't block on another read once interrupted
        return None

    # -- introspection for tests -------------------------------------------
    def answered_approvals(self) -> list[dict]:
        return list(self._answered_approvals)

    def sent_methods(self) -> list[str]:
        return [m.get("method") or m.get("__script__") or "response"
                for m in self.sent]

    def received_methods(self) -> list[str]:
        return [str(m.get("method") or "response") for m in self.received]

    def handshake_ok(self) -> bool:
        return self.initialize_seen and self.initialized_seen


def make_pair(scenario: str, **kwargs: Any) -> tuple[ScriptedAppServer, dict]:
    """Build a connected (client-streams, server) pair for in-process tests."""
    client_out = QueueStream()   # server -> client
    client_in = QueueStream()    # client -> server
    server = ScriptedAppServer(scenario, **kwargs).attach(client_out, client_in)
    streams = {"reader": client_out, "writer": client_in}
    return server, streams


class InMemoryStubTransport:
    """Client-side transport wired to a :class:`ScriptedAppServer` thread."""

    def __init__(self, server: ScriptedAppServer, reader: QueueStream,
                 writer: QueueStream) -> None:
        self.server = server
        self.reader = reader
        self._writer = writer
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="stub-appserver", daemon=True)
        self._thread.start()

    def write(self, data: bytes) -> None:
        if self._writer.closed:
            raise OSError("stub transport closed")
        self._writer.write(data)

    def close(self) -> None:
        self.server.stop()
        self._writer.close()
        self.reader.close()
        self._thread.join(timeout=2)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class InMemoryStubFactory:
    """Trusted offline factory: one scripted server per call, no process.

    ``scenario`` may be a string or a list consumed one per call. Every call
    reuses ``thread_id`` so a later ``thread/resume`` targets the same id;
    the stub keeps no real thread history.
    """

    def __init__(self, scenario: Any = "happy", *, thread_id: str = "stub-thread-0001") -> None:
        self._scenarios = list(scenario) if isinstance(scenario, (list, tuple)) else None
        self.scenario = scenario
        self.thread_id = thread_id
        self.servers: list[ScriptedAppServer] = []

    def __call__(self, invocation_id: str, workdir: str) -> InMemoryStubTransport:
        scenario = self._scenarios.pop(0) if self._scenarios is not None else self.scenario
        history = [{"id": s.turn_id, "status": s.turn_status, "items": []}
                   for s in self.servers if s.turn_started]
        server, streams = make_pair(scenario, thread_id=self.thread_id,
                                    turn_id=f"stub-turn-{len(self.servers) + 1:04d}",
                                    history=history)
        self.servers.append(server)
        return InMemoryStubTransport(server, streams["reader"], streams["writer"])

    def identity_matches(self) -> tuple[bool, str]:
        return True, "in-memory offline stub"

    def binding_identity(self) -> str:
        return "appserver-stub:in-memory"


def bundled_stdio_stub_path() -> str:
    import os

    return os.path.realpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "tools", "demo", "stub_appserver.py"
    ))


class StdioStubFactory:
    """Trusted offline factory: the bundled stub as a real child process."""

    #: The venv's python.exe is a launcher with the real interpreter as its
    #: child; that child may still be exiting right after the launcher closed
    #: (the S5 finding). Same grace as the real factory; returns early once gone.
    close_grace_seconds = 3.0

    def __init__(self, scenario: str = "happy", *, stderr_dir: Optional[str] = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.scenario = scenario
        self.stderr_dir = stderr_dir
        self.argv = [sys.executable, bundled_stdio_stub_path(), "--scenario", scenario]
        #: Turn ids issued so far; each process is told the thread's earlier
        #: turns (ids only) so a resume can return them, as the in-memory one does.
        self.turn_ids: list[str] = []

    def __call__(self, invocation_id: str, workdir: str) -> Any:
        import os

        from agenthub_adapters.appserver import StdioTransport

        env = dict(os.environ)
        env["CODEX_HOME"] = workdir  # the stub has no auth path; isolate state
        turn_id = f"stub-turn-{len(self.turn_ids) + 1:04d}"
        env["STUB_APPSERVER_TURN_ID"] = turn_id
        env["STUB_APPSERVER_HISTORY"] = json.dumps(self.turn_ids)
        self.turn_ids.append(turn_id)
        logs = self.stderr_dir or os.path.join(os.path.dirname(os.path.abspath(workdir)), "_logs")
        os.makedirs(logs, exist_ok=True)
        # The stub's "thread store" lives beside the logs, never in the cwd.
        env["STUB_APPSERVER_STATE"] = os.path.join(logs, "stub-thread-state.json")
        stderr = os.path.join(logs, f"stub-{invocation_id}.log")
        return StdioTransport(list(self.argv), env=env, cwd=workdir, stderr_path=stderr)

    def identity_matches(self) -> tuple[bool, str]:
        return True, "bundled stdio stub"

    def binding_identity(self) -> str:
        return "appserver-stub:stdio"


def run_stdio(scenario: str) -> int:
    """Serve one scenario over real stdio (used by tools/demo/stub_appserver.py)."""

    class _Stdout:
        def write(self, data: bytes) -> None:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    class _Stdin:
        def readline(self) -> bytes:
            return sys.stdin.buffer.readline()

        def read(self, count: int = -1) -> bytes:
            return sys.stdin.buffer.read(count)

    import os

    try:
        earlier = json.loads(os.environ.get("STUB_APPSERVER_HISTORY") or "[]")
    except ValueError:
        earlier = []
    history = [{"id": t, "status": "completed", "items": []} for t in earlier if isinstance(t, str)]
    server = ScriptedAppServer(scenario, turn_id=os.environ.get("STUB_APPSERVER_TURN_ID") or "stub-turn-0001",
                               history=history, state_path=os.environ.get("STUB_APPSERVER_STATE") or None)
    server._out = _Stdout()   # noqa: SLF001 - the stdio role of the same object
    server._in = _Stdin()     # noqa: SLF001
    server.serve_forever()
    return 0


def drain(stream: QueueStream, *, idle_timeout: float = 0.5) -> bytes:
    """Read everything a stream produced until it is closed and quiet."""
    out = bytearray()
    end = time.monotonic() + idle_timeout
    while time.monotonic() < end:
        chunk = stream.read(1)
        if chunk:
            out.extend(chunk)
            end = time.monotonic() + idle_timeout
        elif stream.closed and not stream.has_pending():
            break
    return bytes(out)


def serve_bytes(scenario: str, raw_in: bytes, *, framing: str = "lines") -> bytes:
    """Feed raw bytes to the scripted server and return everything it sent.

    Pins the wire format with no process involved.
    """
    incoming = QueueStream()
    outgoing = QueueStream()
    incoming.write(raw_in)
    incoming.close()
    ScriptedAppServer(scenario, framing=framing).attach(outgoing, incoming).serve_forever()
    outgoing.close()
    return drain(outgoing)


def json_lines(payload: bytes) -> list[dict]:
    return [json.loads(line) for line in payload.splitlines() if line.strip()]
