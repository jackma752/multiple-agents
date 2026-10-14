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
===================  ==========================================================
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
)

#: The approval request this stub exercises. The others in APPROVAL_METHODS behave
#: identically on the wire, so one is enough to pin the client's behaviour.
APPROVAL_REQUEST = "item/commandExecution/requestApproval"


class ScriptedAppServer:
    """Deterministic app-server behaviour over a pair of byte streams."""

    def __init__(self, scenario: str, *, framing: str = "lines",
                 thread_id: str = "stub-thread-0001",
                 turn_id: str = "stub-turn-0001",
                 on_sent: Optional[Callable[[dict], None]] = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; known: {list(SCENARIOS)}")
        self.scenario = scenario
        self.framing = framing
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.on_sent = on_sent
        self.sent: list[dict] = []
        self.received: list[dict] = []
        self._answered_approvals: list[dict] = []
        self._lock = threading.Lock()
        self._next_server_id = 0
        self._stop = threading.Event()
        self.turn_status = "inProgress"
        self._interrupt_accepted = False
        self._interrupt_declined = False

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

        if method == "initialize":
            # Whether the real server requires this is UNVERIFIED; the stub simply
            # answers it so the client's optional handshake path can be exercised.
            self.notify_supported = True
            self.reply(request_id, {"serverInfo": {"name": "stub-appserver", "version": "0.0.1"}})
            return

        if method in (THREAD_START, THREAD_RESUME):
            thread = {"id": self.thread_id, "turns": []}
            # Schema: the notification carries `thread` (no top-level threadId).
            self.notify(THREAD_STARTED, {"thread": thread})
            self.reply(request_id, {
                "thread": thread,
                "cwd": "C:\\stub",
                "model": "stub-model",
                "modelProvider": "stub",
                "sandbox": "read-only",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
            })
            return

        if method == TURN_START:
            self.turn_status = "inProgress"
            self.notify(TURN_STARTED, {"threadId": self.thread_id,
                                       "turn": {"id": self.turn_id, "items": [],
                                                "status": self.turn_status}})
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
            self.reply(request_id, {})
            self._interrupt_declined = True
            return

        if str(method).startswith("__"):
            # A realistic server errors on an unknown method.
            self.reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})
            return

        # Any other request: acknowledge so the client is not left hanging.
        self.reply(request_id, {})

    def _finish_turn(self, status: str) -> None:
        self.turn_status = status
        self.notify(TURN_COMPLETED, {
            "threadId": self.thread_id,
            "turn": {
                "id": self.turn_id,
                "items": [],
                "status": status,
                "usage": {"inputTokens": 11, "outputTokens": 2},
            },
        })

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

    def _wait_for_response(self, request_id: Any, *, timeout: float) -> Optional[dict]:
        """Read client messages for up to ``timeout`` seconds, looking for one answer."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                message, _framing = read_message(self._in, self.framing)
            except Exception:  # noqa: BLE001 - EOF from the client
                return None
            self.received.append(message)
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message
            self.handle(message)
        return None

    # -- introspection for tests -------------------------------------------
    def answered_approvals(self) -> list[dict]:
        return list(self._answered_approvals)

    def sent_methods(self) -> list[str]:
        return [m.get("method") or m.get("__script__") or "response"
                for m in self.sent]


def make_pair(scenario: str, **kwargs: Any) -> tuple[ScriptedAppServer, dict]:
    """Build a connected (client-streams, server) pair for in-process tests."""
    client_out = QueueStream()   # server -> client
    client_in = QueueStream()    # client -> server
    server = ScriptedAppServer(scenario, **kwargs).attach(client_out, client_in)
    streams = {"reader": client_out, "writer": client_in}
    return server, streams


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

    server = ScriptedAppServer(scenario)
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
