"""Minimal Codex app-server client (prototype; real S1 handshake verified).

WHY THIS EXISTS
---------------
Our shipped transport is `codex exec`: one synchronous process per call. That mode
cannot ask for approval (the CLI rejects `-a`), cannot be interrupted (there is no
cancel option), and here we could not even observe whether a write was attempted.
The app-server protocol exposes exactly those three things - approval requests,
turn interrupts, and a rich item stream - so this module prototypes that transport
WITHOUT touching the exec adapter, which stays the default.

WHAT IS VERIFIED HERE, AND WHAT IS NOT
--------------------------------------
Verified offline, against a scripted stub server: id matching, notification
ordering, delta assembly, approval round-trips (allow/decline/timeout), interrupt
handling, disconnect and timeout behaviour, and the kernel-facing rules.

Real S1 verified JSONL, initialize -> initialized -> one empty thread, and local
process shutdown on 2026-10-17. No real model turn was started. Real tool approval,
turn interruption, session resumption and native-child control remain unverified.
JSONL is the default; other framing modes are diagnostic opt-ins only.

SAFETY RULES THIS MODULE ENCODES (mirroring the exec adapter and AGENTS.md)
-------------------------------------------------------------------------
* An approval request from the server is NEVER auto-accepted. The default policy
  declines, and an explicit decision must be supplied by the caller.
* An accepted interrupt is NOT proof that the work stopped. Only a terminal turn
  event, or a confirmed-closed transport plus an explicit terminal report, may
  settle a call; otherwise the outcome is "unconfirmed", the kernel keeps the
  quota slot, and the call becomes `unknown` for a human.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

#: Wire encodings. The official stdio protocol is JSONL (one JSON object per line),
#: which is what this client uses by default and what StdioTransport writes.
#: :data:`FRAMING_LSP` exists only because a tolerant reader costs nothing and makes
#: an unexpected header visible instead of producing a parse error; it is NOT
#: auto-selected for the real server.
FRAMING_LINES = "lines"  # JSONL - the official stdio encoding
FRAMING_LSP = "lsp"      # `Content-Length: N\r\n\r\n` + N bytes (diagnostic only)
FRAMING_AUTO = "auto"    # diagnostic only: sniff the first line, never used as a default


class AppServerError(Exception):
    """Base class for transport/protocol failures."""


class HandshakeRequired(AppServerError):
    """A thread/turn request was attempted before the handshake completed.

    The protocol's client->server notification set contains exactly one entry,
    `initialized`, which is sent after the `initialize` request succeeds. Sending
    thread or turn requests before that is a protocol violation, so the session
    refuses locally (and the stub, in turn, refuses on the wire) rather than
    discovering it through a confusing server error.
    """


class AppServerDisconnected(AppServerError):
    """The server went away (EOF, closed pipe, or killed).

    Callers must treat this as "state unconfirmed", never as "the turn stopped":
    the remote work may still exist somewhere.
    """


class AppServerTimeout(AppServerError):
    """No response within the deadline. Also unconfirmed, never "stopped"."""


class AppServerProtocolError(AppServerError):
    """The peer sent something we cannot parse as a JSON-RPC message."""


class AppServerResponseError(AppServerError):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #


def encode_message(message: dict, framing: str = FRAMING_LINES) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == FRAMING_LINES:
        return body + b"\n"
    if framing == FRAMING_LSP:
        return b"Content-Length: %d\r\n\r\n" % len(body) + body
    raise ValueError(f"unknown framing: {framing!r}")


def read_message(stream: Any, framing: str = FRAMING_AUTO) -> tuple[dict, str]:
    """Read one JSON-RPC message. Returns ``(message, framing_used)``.

    Auto-detection is a diagnostic option. The production client explicitly
    selects JSONL. JSON objects start with `{`; LSP starts with a header line.
    """
    first = stream.readline()
    if not first:
        raise AppServerDisconnected("end of stream")
    if framing == FRAMING_AUTO:
        head = first.strip().lower()
        framing = FRAMING_LSP if head.startswith(b"content-length") else FRAMING_LINES
    if framing == FRAMING_LINES:
        line = first.strip()
        if not line:
            # Tolerate blank keep-alive lines rather than failing the session.
            return read_message(stream, FRAMING_LINES)
        return _decode(line), FRAMING_LINES

    # LSP: headers until a blank line, then exactly Content-Length bytes.
    length: Optional[int] = None
    header = first
    while True:
        text = header.decode("ascii", "replace").strip()
        if not text:
            break
        name, _, value = text.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise AppServerProtocolError(f"bad Content-Length: {value!r}") from exc
        header = stream.readline()
        if not header:
            raise AppServerDisconnected("end of stream inside headers")
    if length is None:
        raise AppServerProtocolError("LSP framing without Content-Length")
    body = _read_exactly(stream, length)
    return _decode(body), FRAMING_LSP


def _read_exactly(stream: Any, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        piece = stream.read(count - len(chunks))
        if not piece:
            raise AppServerDisconnected(f"end of stream after {len(chunks)}/{count} bytes")
        chunks.extend(piece)
    return bytes(chunks)


def _decode(payload: bytes) -> dict:
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppServerProtocolError(f"not JSON: {payload[:120]!r}") from exc
    if not isinstance(message, dict):
        raise AppServerProtocolError(f"not a JSON object: {payload[:120]!r}")
    return message


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #


class QueueStream:
    """A blocking, thread-safe byte stream backed by queues (no OS pipes).

    Used for the offline prototype: the sandbox denies piped stdio to native
    processes, and an in-memory stream also makes the tests deterministic. A real
    `codex app-server` needs the stdio transport below instead.

    Transport interface (what :class:`AppServerClient` needs):
    ``.reader`` is a file-like object with ``readline()``/``read(n)``;
    ``.write(bytes)`` sends to the peer; ``.close()`` ends the session.
    """

    def __init__(self) -> None:
        self._chunks: "queue.Queue[bytes]" = queue.Queue()
        self._buffer = bytearray()
        self._closed = threading.Event()

    # -- producer side ------------------------------------------------------
    def write(self, data: bytes) -> None:
        self._chunks.put(bytes(data))

    def close(self) -> None:
        self._closed.set()

    # -- inspection (public, so helpers need not touch internals) -----------
    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def has_pending(self) -> bool:
        return bool(self._buffer) or not self._chunks.empty()

    # -- consumer side (file-like, as read_message expects) -----------------
    def _fill(self) -> bool:
        # A partial line/body already in the buffer still needs another chunk.
        # Treating it as a successful fill spins forever without reading the queue.
        if self._closed.is_set() and self._chunks.empty():
            return False
        try:
            chunk = self._chunks.get(timeout=0.1)
        except queue.Empty:
            return False
        if chunk == b"":
            self._closed.set()
            return False
        self._buffer.extend(chunk)
        return True

    def readline(self) -> bytes:
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[: index + 1])
                del self._buffer[: index + 1]
                return line
            if not self._fill():
                if self._closed.is_set():
                    line = bytes(self._buffer)
                    self._buffer.clear()
                    return line          # EOF: hand back any tail
                continue

    def read(self, count: int = -1) -> bytes:
        if count < 0:
            raise ValueError("QueueStream.read requires a byte count")
        while len(self._buffer) < count:
            if not self._fill() and self._closed.is_set():
                break
        piece = bytes(self._buffer[:count])
        del self._buffer[:count]
        return piece


class StdioTransport:
    """Real transport: spawn a process and talk JSONL to its stdin/stdout.

    The official stdio encoding is JSONL (one JSON object per line), so this
    transport writes and reads that; ``framing`` is exposed only so a diagnostic
    can be explicit about what it expects.

    Process lifecycle is covered with a Python stdio stub and simulated shutdown
    failures. That does not establish real Codex server behaviour.
    """

    _EOF_TIMEOUT = 1.0
    _TERMINATE_TIMEOUT = 3.0
    _KILL_TIMEOUT = 3.0

    def __init__(self, argv: list[str], *, env: Optional[dict] = None,
                 cwd: Optional[str] = None,
                 stderr_path: Optional[str] = None,
                 framing: str = FRAMING_LINES) -> None:
        import subprocess

        self.framing = framing
        self._closed = False
        self._close_lock = threading.Lock()
        self._write_lock = threading.Lock()
        # Keep diagnostics without a separate stderr pipe or reader thread.
        self._stderr_file = open(stderr_path, "w", encoding="utf-8", errors="replace") \
            if stderr_path else subprocess.DEVNULL
        try:
            # Raw pipes have no buffered I/O lock for close() to wait on while
            # the client reader is blocked; stdin.close() also cannot flush.
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
                stderr=self._stderr_file, env=env, cwd=cwd,
            )
        except BaseException:
            self._close_handle(self._stderr_file)
            raise
        self.reader = self._proc.stdout

    @property
    def pid(self) -> int:
        return self._proc.pid

    def write(self, data: bytes) -> None:
        # A short write must not let another request interleave in the same line.
        with self._write_lock:
            if self._closed:
                raise AppServerDisconnected("transport closed")
            assert self._proc.stdin is not None
            remaining = memoryview(data)
            while remaining:
                written = self._proc.stdin.write(remaining)
                if not written:
                    raise AppServerDisconnected("stdin write made no progress")
                remaining = remaining[written:]

    @staticmethod
    def _close_handle(handle: Any) -> None:
        if hasattr(handle, "close"):
            try:
                handle.close()
            except OSError:
                pass

    def close(self) -> None:
        """Reap only our child, or raise without claiming that it has stopped."""
        import subprocess

        with self._close_lock:
            if self._closed and self._proc.poll() is not None:
                return
            self._closed = True
            try:
                self._close_handle(self._proc.stdin)
                # EOF lets a healthy server flush state and exit normally.
                try:
                    self._proc.wait(timeout=self._EOF_TIMEOUT)
                    return
                except subprocess.TimeoutExpired:
                    pass
                for stop, timeout in (
                    (self._proc.terminate, self._TERMINATE_TIMEOUT),
                    (self._proc.kill, self._KILL_TIMEOUT),
                ):
                    try:
                        stop()
                    except OSError:
                        # The process may have exited between wait and signal.
                        if self._proc.poll() is None:
                            raise
                    try:
                        self._proc.wait(timeout=timeout)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                raise AppServerTimeout("child exit unconfirmed after kill")
            finally:
                self._close_handle(self._proc.stdin)
                self._close_handle(self._proc.stdout)
                self._close_handle(self._stderr_file)

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.poll()


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


@dataclass
class Notification:
    method: str
    params: dict


@dataclass
class ServerRequest:
    """A server->client request: it NEEDS an answer, and we never auto-answer."""

    id: Any
    method: str
    params: dict
    answered: bool = False


@dataclass
class ApprovalPolicy:
    """How to answer server approval requests.

    ``default_decision`` is applied only when no rule matches. It defaults to
    ``decline``: silence must never mean approval (AGENTS.md: a prompt is not a
    safety valve, and approval must be an explicit act).

    Only :data:`SUPPORTED_DECISIONS` may appear here. In particular
    ``acceptForSession`` is NOT supported anywhere in this client, and a policy that
    names it fails loudly at construction instead of quietly widening consent.
    """

    default_decision: str = "decline"
    rules: dict[str, str] = field(default_factory=dict)   # method -> decision
    decisions: list[dict] = field(default_factory=list)    # audit trail of answers

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Refuse a policy that names a decision this client will not send."""
        for label, value in [("default_decision", self.default_decision), *self.rules.items()]:
            if value not in SUPPORTED_DECISIONS:
                raise AppServerError(
                    f"approval policy {label}={value!r} is not supported; "
                    f"this client sends only {sorted(SUPPORTED_DECISIONS)}"
                )

    def decide(self, method: str, params: dict) -> str:
        decision = self.rules.get(method, self.default_decision)
        if decision not in SUPPORTED_DECISIONS:
            raise AppServerError(
                f"approval policy would answer {method!r} with {decision!r}, which this "
                f"client refuses to send; allowed: {sorted(SUPPORTED_DECISIONS)}"
            )
        return decision


class AppServerClient:
    """Request/response + notification pump for one app-server session."""

    def __init__(self, transport: Any, *, framing: str = FRAMING_LINES,
                 request_timeout: float = 30.0,
                 approval_policy: Optional[ApprovalPolicy] = None) -> None:
        self.transport = transport
        # JSONL is the protocol's encoding; any other value must be asked for
        # explicitly (FRAMING_AUTO is for diagnostics, never a production default).
        self.framing = framing
        self.detected_framing: Optional[str] = None
        self.request_timeout = request_timeout
        self.approval_policy = approval_policy or ApprovalPolicy()
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[Any, dict] = {}
        self._notifications: "queue.Queue[Notification]" = queue.Queue()
        self._server_requests: list[ServerRequest] = []
        self._disconnected = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._reader = threading.Thread(target=self._pump, name="appserver-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self.transport.close()
        self._disconnected.set()

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    @property
    def transport_closed_confirmed(self) -> bool:
        """True only when we KNOW the session ended (EOF/close), not on timeout.

        Even then it is not evidence that remote work stopped - see
        :func:`judge_turn_outcome`.
        """
        return self._disconnected.is_set()

    # -- requests -----------------------------------------------------------
    def request(self, method: str, params: Optional[dict] = None, *,
                timeout: Optional[float] = None) -> dict:
        """Send a request and wait for the response with the SAME id.

        A late response whose id nobody waits for is dropped, not mismatched: the
        stub exercises exactly that case.
        """
        if self._disconnected.is_set():
            raise AppServerDisconnected("session already closed")
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            waiter = {"event": threading.Event(), "result": None, "error": None}
            self._pending[request_id] = waiter
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        deadline = time.time() + (self.request_timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise AppServerTimeout(f"no response to {method!r} (id={request_id})")
            if waiter["event"].wait(min(remaining, 0.05)):
                break
            if self._disconnected.is_set():
                with self._lock:
                    self._pending.pop(request_id, None)
                raise AppServerDisconnected(
                    f"session ended while waiting for {method!r} (id={request_id})"
                )
        if waiter["error"] is not None:
            code, message, data = waiter["error"]
            raise AppServerResponseError(code, message, data)
        return waiter["result"] or {}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def respond(self, request_id: Any, result: Any = None, error: Any = None) -> None:
        message: dict = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = {} if result is None else result
        self._send(message)

    # -- answering server requests (approvals) ------------------------------
    def answer_approval(self, request: ServerRequest, decision: str) -> dict:
        """Answer one approval request with an explicit decision.

        Refuses to answer twice, and refuses any decision outside
        :data:`SUPPORTED_DECISIONS` - including ``acceptForSession`` and the
        structured amendments, which would change what FUTURE commands may do
        without prompting.
        """
        if request.answered:
            raise AppServerError(f"approval {request.id} already answered")
        if decision not in SUPPORTED_DECISIONS:
            raise AppServerError(
                f"unsupported decision {decision!r}; this client sends only "
                f"{sorted(SUPPORTED_DECISIONS)}"
            )
        self.respond(request.id, {"decision": decision})
        request.answered = True
        self.approval_policy.decisions.append(
            {"id": request.id, "method": request.method, "decision": decision,
             "command": (request.params or {}).get("command")}
        )
        return {"id": request.id, "decision": decision}

    def auto_answer_pending(self) -> list[dict]:
        """Answer every unanswered server request using the policy (default: decline)."""
        answered = []
        for request in self.server_requests():
            if request.answered:
                continue
            answered.append(self.answer_approval(
                request, self.approval_policy.decide(request.method, request.params)
            ))
        return answered

    # -- notifications ------------------------------------------------------
    def notifications(self) -> list[Notification]:
        drained: list[Notification] = []
        while True:
            try:
                drained.append(self._notifications.get_nowait())
            except queue.Empty:
                return drained

    def wait_for(self, predicate: Callable[[Notification], bool], *,
                 timeout: float) -> Optional[Notification]:
        """Wait for the first notification matching ``predicate`` (order preserved).

        Notifications are never reordered: they are drained from one queue in the
        order the reader saw them, so a turn lifecycle check can rely on sequence.
        """
        deadline = time.time() + timeout
        seen: list[Notification] = []
        while time.time() < deadline:
            try:
                item = self._notifications.get(timeout=0.05)
            except queue.Empty:
                if self._disconnected.is_set():
                    return None
                continue
            seen.append(item)
            if predicate(item):
                for pending in seen:
                    if pending is not item:
                        self._notifications.put(pending)
                return item
        for pending in seen:
            self._notifications.put(pending)
        return None

    def server_requests(self) -> list[ServerRequest]:
        with self._lock:
            return list(self._server_requests)

    # -- internals ----------------------------------------------------------
    def _send(self, message: dict) -> None:
        framing = self.detected_framing or (
            FRAMING_LINES if self.framing == FRAMING_AUTO else self.framing
        )
        try:
            self.transport.write(encode_message(message, framing))
        except Exception as exc:  # noqa: BLE001
            self._fail(AppServerDisconnected(f"write failed: {exc}"))
            raise AppServerDisconnected(f"write failed: {exc}") from exc

    def _pump(self) -> None:
        # The transport OWNS the reader; the client reads through `transport.reader`.
        # (Passing the transport itself here was the first bug this test suite
        # caught: an object without readline() failed the whole session instantly.)
        reader = getattr(self.transport, "reader", None)
        if reader is None:
            self._fail(AppServerError("transport has no .reader to read from"))
            return
        try:
            while not self._disconnected.is_set():
                message, framing = read_message(reader, self.framing)
                self.detected_framing = framing
                self._dispatch(message)
        except AppServerDisconnected as exc:
            self._fail(exc)
        except AppServerProtocolError as exc:
            self._error = exc
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001
            self._error = exc
            self._fail(exc)

    def _dispatch(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            with self._lock:
                waiter = self._pending.pop(message["id"], None)
            if waiter is None:
                # No one is waiting: a duplicate or very late response. Dropping it
                # is the correct behaviour - matching it to another call would be
                # exactly the id-confusion bug this prototype must prevent.
                return
            error = message.get("error")
            if error:
                waiter["error"] = (error.get("code"), error.get("message", ""), error.get("data"))
            else:
                waiter["result"] = message.get("result")
            waiter["event"].set()
            return
        if "id" in message and "method" in message:
            with self._lock:
                self._server_requests.append(
                    ServerRequest(id=message["id"], method=message["method"],
                                  params=message.get("params") or {})
                )
            return
        if "method" in message:
            self._notifications.put(
                Notification(method=message["method"], params=message.get("params") or {})
            )
            return
        raise AppServerProtocolError(f"unrecognised message shape: {sorted(message)}")

    def _fail(self, exc: BaseException) -> None:
        self._disconnected.set()
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _request_id, waiter in pending:
            waiter["error"] = ("disconnected", str(exc), None)
            waiter["event"].set()


# --------------------------------------------------------------------------- #
# kernel-facing rules (pure)
# --------------------------------------------------------------------------- #

# Method names taken from the LOCAL schema capture
# (`var/codex-schema/ClientRequest.json`, `ServerNotification.json`,
# `ServerRequest.json`, generated by `codex app-server generate-json-schema
# --experimental`). Interface existence is proven by the schema; runtime behaviour
# is NOT - that is what the prototype and the next real verification are for.
THREAD_START = "thread/start"
THREAD_RESUME = "thread/resume"           # required: threadId
TURN_START = "turn/start"                 # required: input, threadId
TURN_INTERRUPT = "turn/interrupt"         # required: threadId, turnId
INITIALIZE = "initialize"                 # InitializeParams requires clientInfo
INITIALIZED = "initialized"               # the protocol's ONLY client notification

THREAD_STARTED = "thread/started"
TURN_STARTED = "turn/started"
TURN_COMPLETED = "turn/completed"
ITEM_STARTED = "item/started"
ITEM_COMPLETED = "item/completed"
DELTA_AGENT_MESSAGE = "item/agentMessage/delta"
DELTA_COMMAND_OUTPUT = "item/commandExecution/outputDelta"
FILE_CHANGE_PATCH_UPDATED = "item/fileChange/patchUpdated"

#: The ONLY turn lifecycle terminal notification. The outcome is in its payload:
#: `turn.status`, whose schema enum is completed | interrupted | failed | inProgress.
#: (Earlier drafts of this prototype invented a `turn/cancelled` method - the schema
#: has no such thing, which is exactly why the prototype is checked against it.)
TERMINAL_TURN_METHODS = frozenset({TURN_COMPLETED})

TURN_STATUS_COMPLETED = "completed"
TURN_STATUS_INTERRUPTED = "interrupted"
TURN_STATUS_FAILED = "failed"
TURN_STATUS_IN_PROGRESS = "inProgress"

#: The two modern approval requests plus the permission one. Any of these MUST be
#: answered by us; nothing here is ever auto-approved.
APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    # legacy APIs (Turns started via SendUserTurn/SendUserMessage)
    "execCommandApproval",
    "applyPatchApproval",
})

#: Decisions this client will send. `acceptForSession` is deliberately EXCLUDED, as
#: are the structured decisions (execpolicy / network-policy amendments): all of
#: them change what FUTURE commands in the session may do without prompting, which
#: is a larger decision than answering one prompt and must not happen by accident.
#: Anything outside this set is refused at every entry point: direct answers, policy
#: rules, and the automatic answerer (all three are covered by tests).
SUPPORTED_DECISIONS = frozenset({"accept", "decline", "cancel"})


def judge_turn_outcome(*, terminal_notification: Optional[str] = None,
                       turn_status: Optional[str] = None,
                       interrupt_requested: bool = False,
                       interrupt_accepted: bool = False,
                       transport_closed: bool = False,
                       timed_out: bool = False) -> tuple[str, bool, str]:
    """Decide ``(verdict, terminal, detail)`` for an app-server turn.

    Mirrors the exec adapter's contract so the kernel needs no second rule set:

    * a terminal ``turn/completed`` WITH a terminal status is the only thing that
      settles a call; ``inProgress`` is not terminal even though the notification
      is named "completed";
    * an accepted ``turn/interrupt`` is NOT a stop - the schema documents the
      response as an empty object with no guarantee, and the only outcome signal is
      the later ``turn/completed.turn.status == "interrupted"``;
    * a closed transport is not a stop either: the work may exist elsewhere;
    * everything else is unconfirmed -> the caller marks the call ``unknown`` and
      KEEPS the quota slot.
    """
    if terminal_notification in TERMINAL_TURN_METHODS:
        if turn_status == TURN_STATUS_COMPLETED:
            return "finished", True, "turn/completed with status completed"
        if turn_status == TURN_STATUS_FAILED:
            return "failed", True, "turn/completed with status failed"
        if turn_status == TURN_STATUS_INTERRUPTED:
            return "cancelled", True, "turn/completed with status interrupted"
        return (
            "unconfirmed", False,
            f"turn/completed carried status {turn_status!r} (not terminal): "
            "the turn may still be running",
        )

    if timed_out:
        return "unconfirmed", False, "no terminal turn event before the deadline"
    if interrupt_requested or interrupt_accepted:
        accepted = "accepted by the server " if interrupt_accepted else "requested "
        return (
            "unconfirmed", False,
            f"interrupt {accepted}but no terminal turn status: the turn may still be "
            "running, so the slot is kept and the call must be adjudicated",
        )
    if transport_closed:
        return (
            "unconfirmed", False,
            "transport closed without a terminal turn status: cannot claim the turn stopped",
        )
    return "unconfirmed", False, "turn has not produced a terminal status yet"


def requires_explicit_decision(method: str) -> bool:
    """True when a server request is an approval that must be answered by us."""
    return method in APPROVAL_METHODS


def describe_approval(params: dict) -> dict:
    """A content-free summary of an approval request, safe to log or show.

    Field names follow the schema's params; anything unknown (including any prompt
    text) is dropped rather than echoed, because an approval payload can quote user
    content and the console only needs to know WHAT is being asked.
    """
    keys = ("itemId", "command", "cwd", "reason", "availableDecisions", "approvalId",
            "threadId", "turnId", "kind", "grantRoot", "path")
    return {k: params.get(k) for k in keys if k in params}


# --------------------------------------------------------------------------- #
# session layer: the shape the kernel would consume
# --------------------------------------------------------------------------- #


@dataclass
class TurnOutcome:
    """What a caller may conclude about one turn, and nothing more."""

    verdict: str                 # finished | failed | cancelled | unconfirmed
    terminal: bool               # may the kernel settle and free the slot?
    detail: str
    turn_status: str = ""
    thread_id: str = ""
    turn_id: str = ""
    interrupt_accepted: bool = False

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "terminal": self.terminal,
            "detail": self.detail,
            "turn_status": self.turn_status,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "interrupt_accepted": self.interrupt_accepted,
        }


def turn_status_of(notification: "Notification") -> str:
    """Read `turn.status` out of a `turn/completed` payload (schema enum)."""
    turn = (notification.params or {}).get("turn") or {}
    if isinstance(turn, dict):
        return str(turn.get("status") or "")
    return ""


def turn_id_of(notification: "Notification") -> str:
    turn = (notification.params or {}).get("turn") or {}
    return str(turn.get("id") or "") if isinstance(turn, dict) else ""


class AppServerSession:
    """Thin, explicit wrapper: handshake, then one thread, one turn at a time.

    Protocol order is enforced, not assumed:

    1. ``initialize`` request, 2. ``initialized`` notification (the only client
    notification the protocol defines), 3. ``thread/start`` or ``thread/resume``,
    4. ``turn/start``. Steps 3 and 4 raise :class:`HandshakeRequired` if the
    handshake has not completed, so a mis-ordered call never reaches the wire.

    Every method that could imply "the work stopped" returns an outcome whose
    ``terminal`` flag the kernel must honour. Nothing here decides quota: that
    stays in the kernel's approval/quota gate.
    """

    def __init__(self, client: AppServerClient, *, enforce_handshake: bool = True) -> None:
        self.client = client
        self.enforce_handshake = enforce_handshake
        self.thread_id: str = ""
        self.turn_id: str = ""
        self.initialized: Optional[dict] = None
        self.handshake_complete: bool = False

    # -- handshake ----------------------------------------------------------
    def initialize(self, *, client_name: str = "agent-hub",
                   version: str = "0.1.0") -> dict:
        """Run the handshake: ``initialize`` request, then ``initialized``.

        The client notification schema has exactly one entry, ``initialized``, and
        the protocol expects it after a successful ``initialize``. Both halves are
        required here: without the notification the handshake is not complete, and
        thread/turn requests are refused.
        """
        result = self.client.request(INITIALIZE, {
            "clientInfo": {"name": client_name, "version": version},
        })
        self.client.notify(INITIALIZED)
        self.initialized = result
        self.handshake_complete = True
        return result

    def _require_handshake(self, what: str) -> None:
        if self.enforce_handshake and not self.handshake_complete:
            raise HandshakeRequired(
                f"refusing {what}: the handshake is not complete "
                "(initialize request + initialized notification)"
            )

    # -- thread -------------------------------------------------------------
    def start_thread(self, *, cwd: Optional[str] = None) -> str:
        self._require_handshake("thread/start")
        params: dict = {}
        if cwd:
            params["cwd"] = cwd
        result = self.client.request(THREAD_START, params)
        self.thread_id = _thread_id_from(result)
        return self.thread_id

    def resume_thread(self, thread_id: str) -> str:
        self._require_handshake("thread/resume")
        result = self.client.request(THREAD_RESUME, {"threadId": thread_id})
        self.thread_id = _thread_id_from(result) or thread_id
        return self.thread_id

    # -- turn ---------------------------------------------------------------
    def start_turn(self, text: str, *, thread_id: Optional[str] = None) -> str:
        self._require_handshake("turn/start")
        target = thread_id or self.thread_id
        if not target:
            raise AppServerError("start_turn without a thread id")
        result = self.client.request(TURN_START, {
            "threadId": target,
            "input": [{"type": "text", "text": text}],
        })
        turn = result.get("turn") or {}
        self.turn_id = str((turn or {}).get("id") or result.get("turnId") or "")
        return self.turn_id

    def interrupt_turn(self, *, thread_id: Optional[str] = None,
                       turn_id: Optional[str] = None) -> dict:
        """Ask to interrupt. The response being OK is NOT a stop."""
        self._require_handshake("turn/interrupt")
        target_thread = thread_id or self.thread_id
        target_turn = turn_id or self.turn_id
        if not target_thread or not target_turn:
            raise AppServerError("interrupt needs both a thread id and a turn id")
        return self.client.request(TURN_INTERRUPT,
                                   {"threadId": target_thread, "turnId": target_turn})

    def collect_deltas(self) -> str:
        """Assemble assistant text from the deltas seen so far, in arrival order."""
        text = []
        for notification in self.client.notifications():
            if notification.method == DELTA_AGENT_MESSAGE:
                piece = notification.params.get("delta")
                if isinstance(piece, str):
                    text.append(piece)
        return "".join(text)

    def await_turn_end(self, *, thread_id: Optional[str] = None,
                       turn_id: Optional[str] = None,
                       timeout: float = 60.0,
                       interrupt_requested: bool = False,
                       interrupt_accepted: bool = False) -> TurnOutcome:
        """Wait for the terminal turn status, then judge it.

        Returns an ``unconfirmed`` outcome (``terminal=False``) on timeout or
        disconnect: the kernel must then mark the call `unknown` and keep its slot.
        """
        target_thread = thread_id or self.thread_id
        target_turn = turn_id or self.turn_id

        def is_our_terminal(notification: "Notification") -> bool:
            if notification.method not in TERMINAL_TURN_METHODS:
                return False
            params = notification.params or {}
            if target_thread and params.get("threadId") not in (None, target_thread):
                return False
            got_turn = turn_id_of(notification)
            return not (target_turn and got_turn and got_turn != target_turn)

        notification = self.client.wait_for(is_our_terminal, timeout=timeout)
        if notification is None:
            verdict, terminal, detail = judge_turn_outcome(
                interrupt_requested=interrupt_requested,
                interrupt_accepted=interrupt_accepted,
                transport_closed=self.client.disconnected,
                timed_out=not self.client.disconnected,
            )
            return TurnOutcome(verdict, terminal, detail, thread_id=target_thread,
                               turn_id=target_turn, interrupt_accepted=interrupt_accepted)

        status = turn_status_of(notification)
        verdict, terminal, detail = judge_turn_outcome(
            terminal_notification=notification.method,
            turn_status=status,
            interrupt_requested=interrupt_requested,
            interrupt_accepted=interrupt_accepted,
        )
        return TurnOutcome(verdict, terminal, detail, turn_status=status,
                           thread_id=target_thread, turn_id=turn_id_of(notification) or target_turn,
                           interrupt_accepted=interrupt_accepted)


def _thread_id_from(result: dict) -> str:
    thread = result.get("thread")
    if isinstance(thread, dict):
        return str(thread.get("id") or "")
    return str(result.get("threadId") or "")
