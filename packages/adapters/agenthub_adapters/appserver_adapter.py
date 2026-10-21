"""Codex over app-server: an optional transport beside ``codex exec``.

One call = one app-server process (or offline stub), one thread, one turn:

    initialize -> initialized -> thread/start | thread/resume -> turn/start
    -> stream notifications until this turn's ``turn/completed``.

What it adds over exec: incremental assistant text (reported through the
Worker's ``emit`` into the conversation event stream) and native thread resume
(``snapshot.resume_thread_id``).

Admission (plan §4.3, unchanged): only the two trusted offline stub factories
are admitted, as ``simulation``. A real app-server is blocked in strict mode
for the same native-child reason as exec, and is NOT offered in supervised
mode either: real tool approval (S3), interrupt (S4) and thread resume (S5)
have not been run. Nothing here may be described as verified against Codex.

Safety rules carried over from :mod:`agenthub_adapters.appserver`:

* a tool approval request of this turn becomes a kernel card when the Worker
  supplies a ``tool_gate``; only a user-approved card is answered ``accept``,
  once. Without a gate, or for any other request, the answer is ``decline``;
* real tool interception is still unverified (S3): the provider deciding to ask
  is its behaviour, not something this adapter can force;
* only this turn's ``turn/completed`` with a terminal status settles a call;
  timeout, disconnect or an unanswered ``turn/start`` stays unconfirmed, so the
  kernel keeps the quota slot and marks the call ``unknown``;
* ``interrupt_invocation`` sends ``turn/interrupt`` for a live turn only; an
  accepted request proves nothing - the call settles ``canceled`` only when
  this turn's ``turn/completed`` says ``interrupted``. Generic ``cancel`` (no
  live turn, e.g. after a restart) stays ``unsupported``; a local stop closes
  only our own transport. Real interrupt behaviour (S4) is unverified;
* native child agents (``collabAgentToolCall`` / ``subAgentActivity`` items)
  are observed and reported, never controlled or counted by this adapter.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from typing import Any, Callable, Optional

from agenthub.contracts import (
    NATIVE_RESUME,
    STREAM_DELTA,
    STREAM_NATIVE_AGENT,
    STREAM_REQUEST_DECLINED,
    STREAM_TURN_ENDED,
    STREAM_TURN_STARTED,
    AdapterContract,
    AdapterStartResult,
    ExecutionAdmission,
    InvocationSnapshot,
    execution_admission,
)
from agenthub_adapters.appserver import (
    DELTA_AGENT_MESSAGE,
    ITEM_COMPLETED,
    ITEM_STARTED,
    TURN_COMPLETED,
    TURN_INTERRUPT,
    AppServerClient,
    AppServerDisconnected,
    AppServerError,
    AppServerResponseError,
    AppServerSession,
    AppServerTimeout,
    ApprovalPolicy,
    describe_approval,
    judge_turn_outcome,
    requires_explicit_decision,
    turn_status_of,
)
from agenthub_adapters.store import CANCELLED, FAILED, FINISHED, RUNNING, ExecutionStore

TRANSPORT = "app-server"
TOKEN_USAGE_UPDATED = "thread/tokenUsage/updated"

REAL_APPSERVER_BLOCK_REASON = (
    "app-server 传输目前只接受离线桩：原生子 Agent 不可控，真实工具审批、中断与线程恢复"
    "（S3–S5）均未验证。严格与监督模式都不会启动真实 app-server。"
)

Emit = Callable[[str, dict], None]

#: How often a pending card's status is re-read while the turn waits.
CARD_POLL_SECONDS = 0.1
#: How long to wait for the server to acknowledge ``turn/interrupt``. The
#: acknowledgement is not the stop; the terminal event is.
INTERRUPT_REQUEST_TIMEOUT = 5.0
#: Thread item types that mean a provider-native child agent (local schema).
NATIVE_AGENT_ITEM_TYPES = frozenset({"collabAgentToolCall", "subAgentActivity"})


def _ignore(_etype: str, _payload: dict) -> None:
    return None


def _utc_naive() -> dt.datetime:
    """Naive UTC, matching how the kernel stores ``expires_at``."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _fit_decision(decision: str, offered: Any) -> str:
    """Keep within the provider's ``availableDecisions``; never upgrade to accept."""
    if not isinstance(offered, list) or decision in offered:
        return decision
    for fallback in ("decline", "cancel"):
        if fallback in offered:
            return fallback
    return "decline"


class CodexAppServerFactory:
    """Real ``codex app-server --stdio`` launcher. Never admitted today.

    Kept so configuration can name the transport and so admission has a
    concrete type to refuse; it is the argv the S1/S2 probes used.
    """

    def __init__(self, *, codex_path: str, codex_home: str, expected_identity: Optional[dict],
                 proxy: Optional[str] = None, ca_bundle: Optional[str] = None) -> None:
        self.codex_path = codex_path
        self.codex_home = codex_home
        self.expected_identity = expected_identity
        self.proxy = proxy
        self.ca_bundle = ca_bundle

    def identity_matches(self) -> tuple[bool, str]:
        from agenthub_adapters.codex import file_identity

        if not self.codex_path or not os.path.isfile(self.codex_path):
            return False, "executable missing"
        if not self.expected_identity:
            return True, "no expected identity recorded (not pinned)"
        actual = file_identity(self.codex_path)
        for key in ("size", "sha256"):
            if self.expected_identity.get(key) != actual.get(key):
                return False, f"{key} mismatch"
        return True, "identity matches"

    def __call__(self, invocation_id: str, workdir: str) -> Any:
        from agenthub_adapters.appserver import StdioTransport
        from agenthub_adapters.codex import SUBAGENT_FEATURE, build_env

        argv = [self.codex_path, "app-server", "--stdio", "--disable", SUBAGENT_FEATURE]
        env = build_env(codex_home=self.codex_home, proxy=self.proxy, ca_bundle=self.ca_bundle)
        return StdioTransport(argv, env=env, cwd=workdir,
                              stderr_path=os.path.join(workdir, "appserver-stderr.log"))


def _trusted_offline(factory: Any) -> bool:
    from agenthub_adapters.appserver_stub import InMemoryStubFactory, StdioStubFactory, bundled_stdio_stub_path
    import sys

    if type(factory) is InMemoryStubFactory:
        return True
    if type(factory) is StdioStubFactory:
        argv = factory.argv
        return (
            len(argv) == 4 and argv[2] == "--scenario"
            and os.path.normcase(os.path.realpath(argv[0])) == os.path.normcase(os.path.realpath(sys.executable))
            and os.path.normcase(os.path.realpath(argv[1])) == os.path.normcase(bundled_stdio_stub_path())
        )
    return False


class _Turn:
    """Everything observed about one turn; no user text is kept."""

    def __init__(self, thread_id: str, turn_id: str) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.deltas: dict[str, list[str]] = {}
        self.final_message = ""
        self.usage: dict = {}
        self.turn_status = ""
        self.terminal_seen = False
        self.foreign_events = 0
        self.declined: list[str] = []
        # request id -> (ServerRequest, card id) while the user decides.
        self.cards: dict[Any, tuple[Any, str]] = {}
        self.card_ids: list[str] = []
        self.last_card_poll = 0.0
        self.interrupt_requested = False
        self.interrupt_accepted = False
        # Provider-native child agents seen in this turn (ids/enums only).
        self.native_agents: list[dict] = []

    def ours(self, params: dict, *, turn_from_payload: bool = False) -> bool:
        """Exact binding: an event without this turn's id is never attributed."""
        if params.get("threadId") not in (None, self.thread_id):
            return False
        got = turn_id_of_params(params) if turn_from_payload else params.get("turnId")
        return got == self.turn_id


def turn_id_of_params(params: dict) -> str:
    turn = params.get("turn") or {}
    return str(turn.get("id") or "") if isinstance(turn, dict) else ""


class AppServerCodexAdapter(AdapterContract):
    """Codex via app-server. Gated exactly like the exec adapter."""

    name = "codex"
    transport = TRANSPORT
    AGENT_IDS = frozenset({"codex"})

    def __init__(self, *, transport_factory: Callable[[str, str], Any], workdir_root: str,
                 store: Optional[ExecutionStore] = None, timeout_seconds: float = 300.0,
                 request_timeout: float = 30.0, version: str = "unknown") -> None:
        self.transport_factory = transport_factory
        self.workdir_root = workdir_root
        self.store = store or ExecutionStore(path=":memory:", table="appserver_executions")
        self.timeout_seconds = timeout_seconds
        self.request_timeout = request_timeout
        self.version = version
        # thread/start sends no sandbox field yet; the server default applies
        # and has not been checked, so nothing stronger than "unknown" is shown.
        self.sandbox = "unknown"
        self.disable_subagents = True  # requested via --disable; not proof
        self._start_count = 0
        self._lock = threading.Lock()
        self._active: dict[str, Any] = {}
        # invocation id -> (AppServerClient, _Turn) while its turn is open.
        self._live: dict[str, tuple[Any, Any]] = {}
        self._stop_requests: set[str] = set()
        self._threads_in_use: set[str] = set()
        os.makedirs(workdir_root, exist_ok=True)

    # -- introspection ------------------------------------------------------

    @property
    def start_count(self) -> int:
        return self._start_count

    def capabilities(self) -> dict[str, Any]:
        return {
            "post_message": True,
            "stream": True,
            "resume_session": True,
            "cancel": False,  # no stop without a live turn (e.g. after a restart)
            "interrupt_live_turn": True,  # turn/interrupt; offline stub only
            "observe_native_agents": True,  # collab/sub-agent items; observation, not control
            "stop_local_process": True,
            "limit_dir": "unknown",
            "intercept_tool": False,
            "control_subagent": False,
            "real_usage": "unknown",
            # Offline stub behaviour only; S2 covered one real turn without
            # resume, approvals or interrupt.
            "verified": {"post_message": False, "stream": False, "resume_session": False,
                         "cancel": False, "intercept_tool": False, "control_subagent": False},
            "transport": TRANSPORT,
        }

    def execution_admission(self) -> ExecutionAdmission:
        if _trusted_offline(self.transport_factory):
            return ExecutionAdmission(True, "simulation", "offline_fixture",
                                      "离线 app-server 桩验证，不调用真实 Codex。")
        return ExecutionAdmission(False, "blocked", "appserver_real_unverified",
                                  REAL_APPSERVER_BLOCK_REASON)

    def supervised_execution_admission(self) -> ExecutionAdmission:
        return ExecutionAdmission(False, "blocked", "appserver_real_unverified",
                                  REAL_APPSERVER_BLOCK_REASON)

    def supports_native_resume(self) -> bool:
        return True

    def health(self) -> bool:
        return self.identity_matches()[0]

    def identity_matches(self) -> tuple[bool, str]:
        check = getattr(self.transport_factory, "identity_matches", None)
        if not callable(check):
            return False, "transport factory has no identity check"
        return check()

    # -- start --------------------------------------------------------------

    def supports_tool_approval(self) -> bool:
        return True

    def start(self, invocation_id: str, snapshot: InvocationSnapshot, *,
              gate_token: Optional[str] = None) -> AdapterStartResult:
        return self.start_streaming(invocation_id, snapshot, gate_token=gate_token, emit=None)

    def start_streaming(self, invocation_id: str, snapshot: InvocationSnapshot, *,
                        gate_token: Optional[str] = None,
                        emit: Optional[Emit] = None,
                        tool_gate: Any = None) -> AdapterStartResult:
        """``tool_gate`` (kernel-owned) turns each tool request into a user card.

        Without a gate every tool request is declined, as before: silence is
        never an approval.
        """
        emit = emit or _ignore
        if not gate_token:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: no kernel gate token (invocation not approved)")
        admission = execution_admission(self, snapshot.execution_mode)
        if not admission.allowed:
            return AdapterStartResult("", ok=False, terminal=True, detail=admission.reason)
        agent_id = (snapshot.agent_id or "").strip()
        if agent_id not in self.AGENT_IDS:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail=f"refused: agent_id {agent_id!r} is not served by this adapter")
        resume_id = snapshot.resume_thread_id if snapshot.context_mode == NATIVE_RESUME else None
        if resume_id:
            with self._lock:
                if resume_id in self._threads_in_use:
                    return AdapterStartResult("", ok=False, terminal=True,
                                              detail="refused: provider thread already has a turn in this process")
                self._threads_in_use.add(resume_id)
        try:
            return self._run(invocation_id, snapshot, resume_id, emit, tool_gate)
        finally:
            if resume_id:
                with self._lock:
                    self._threads_in_use.discard(resume_id)

    def _run(self, invocation_id: str, snapshot: InvocationSnapshot,
             resume_id: Optional[str], emit: Emit, gate: Any = None) -> AdapterStartResult:
        try:
            workdir = os.path.join(self.workdir_root, invocation_id)
            os.makedirs(workdir, exist_ok=True)
        except OSError as exc:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail=f"refused: launch preparation failed ({exc})")

        self._start_count += 1
        # Record before launching: a crash afterwards reads as "may be running".
        self.store.record_start(invocation_id, adapter_invocation_id="")
        with self._lock:
            if invocation_id in self._stop_requests:
                self._stop_requests.discard(invocation_id)
                self.store.set_status(invocation_id, FAILED, "local stop requested before launch")
                return AdapterStartResult("", ok=False, terminal=True,
                                          detail="local stop requested before launch; nothing started")
        try:
            transport = self.transport_factory(invocation_id, workdir)
        except OSError as exc:
            self.store.set_status(invocation_id, FAILED, f"launch failed: {type(exc).__name__}")
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail=f"could not launch app-server: {exc}"[:200])
        except Exception as exc:  # noqa: BLE001 - a process may exist
            detail = f"transport factory raised {type(exc).__name__}"
            self.store.set_status(invocation_id, RUNNING, detail)
            return AdapterStartResult("", ok=False, terminal=False, detail=detail)
        with self._lock:
            self._active[invocation_id] = transport

        client = AppServerClient(transport, request_timeout=self.request_timeout,
                                 approval_policy=ApprovalPolicy())
        session = AppServerSession(client)
        thread_id = ""
        turn: Optional[_Turn] = None
        turn_requested = False
        failure: Optional[str] = None
        timed_out = False
        try:
            client.start()
            session.initialize(client_name="agent-hub", version=self.version)
            if resume_id:
                thread_id = session.resume_thread(resume_id)
                if thread_id != resume_id:
                    raise _Refused("thread/resume returned a different thread id")
            else:
                thread_id = session.start_thread(cwd=workdir)
            if not thread_id:
                raise _Refused("server returned no thread id")
            self.store.set_adapter_id(invocation_id, thread_id)
            turn_requested = True
            turn_id = session.start_turn(snapshot.task_input, thread_id=thread_id)
            if not turn_id:
                raise AppServerError("turn/start returned no turn id")
            turn = _Turn(thread_id, turn_id)
            with self._lock:
                self._live[invocation_id] = (client, turn)
            emit(STREAM_TURN_STARTED, {"thread_id": thread_id, "turn_id": turn_id,
                                       "resumed": bool(resume_id)})
            timed_out = not self._pump(client, turn, emit, gate)
        except _Refused as exc:
            failure = f"refused before any turn: {exc}"
        except AppServerResponseError as exc:
            # The server answered with an error: definitive for that request.
            failure = f"server refused {'turn/start' if turn_requested else 'setup'}: {exc.code}"
        except (AppServerTimeout, AppServerDisconnected, AppServerError) as exc:
            if not turn_requested:
                failure = f"setup failed before any turn: {type(exc).__name__}"
            else:
                failure = None
                timed_out = isinstance(exc, AppServerTimeout)
        finally:
            with self._lock:
                self._live.pop(invocation_id, None)
            local_exit_confirmed = self._close(invocation_id, transport)
            if gate is not None and turn is not None and turn.cards:
                # Unanswered requests died with the turn/transport: void them
                # so a late click cannot "approve" something no one will run.
                try:
                    gate.void_pending()
                except Exception:  # noqa: BLE001
                    pass

        with self._lock:
            stop_requested = invocation_id in self._stop_requests
            self._stop_requests.discard(invocation_id)
        return self._settle(invocation_id, thread_id, turn, turn_requested, failure,
                            timed_out, stop_requested, local_exit_confirmed, resume_id, emit)

    def _pump(self, client: AppServerClient, turn: _Turn, emit: Emit, gate: Any = None) -> bool:
        """Follow this turn until its terminal event. False on timeout."""
        deadline = time.monotonic() + self.timeout_seconds
        wall_deadline = _utc_naive() + dt.timedelta(seconds=self.timeout_seconds)
        while time.monotonic() < deadline:
            if gate is not None and self._has_unrouted(client, turn):
                # Requests and notifications are queued separately by the
                # reader; apply what arrived before the request so its output
                # precedes the card in the event stream.
                for queued in client.notifications():
                    if self._observe(queued, turn, emit):
                        return True
            self._route_requests(client, turn, emit, gate, wall_deadline)
            self._answer_decided(client, turn, gate)
            note = client.wait_for(lambda _n: True, timeout=0.05)
            if note is None:
                if client.disconnected:
                    # Drain anything that arrived before EOF, then stop.
                    rest = client.notifications()
                    for item in rest:
                        if self._observe(item, turn, emit):
                            return True
                    return True
                continue
            if self._observe(note, turn, emit):
                return True
        return False

    def _observe(self, note: Any, turn: _Turn, emit: Emit) -> bool:
        params = note.params or {}
        if note.method in (ITEM_STARTED, ITEM_COMPLETED):
            item = params.get("item") or {}
            if isinstance(item, dict) and item.get("type") in NATIVE_AGENT_ITEM_TYPES:
                if turn.ours(params):
                    self._observe_native(item, turn, emit)
                else:
                    turn.foreign_events += 1
                return False
        if note.method == TURN_COMPLETED:
            if not turn.ours(params, turn_from_payload=True):
                turn.foreign_events += 1
                return False
            turn.turn_status = turn_status_of(note)
            turn.terminal_seen = True
            usage = (params.get("turn") or {}).get("usage")
            if isinstance(usage, dict) and not turn.usage:
                turn.usage = dict(usage)
            return True
        if note.method == DELTA_AGENT_MESSAGE:
            if not turn.ours(params):
                turn.foreign_events += 1
                return False
            piece = params.get("delta")
            if isinstance(piece, str) and piece:
                item_id = str(params.get("itemId") or "")
                turn.deltas.setdefault(item_id, []).append(piece)
                emit(STREAM_DELTA, {"item_id": item_id, "text": piece})
            return False
        if note.method == ITEM_COMPLETED:
            if not turn.ours(params):
                turn.foreign_events += 1
                return False
            item = params.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = item.get("text")
                if not isinstance(text, str):
                    text = "".join(turn.deltas.get(str(item.get("id") or ""), []))
                turn.final_message = text
            return False
        if note.method == TOKEN_USAGE_UPDATED and turn.ours(params):
            last = (params.get("tokenUsage") or {}).get("last")
            if isinstance(last, dict):
                turn.usage = dict(last)
        return False

    @staticmethod
    def _observe_native(item: dict, turn: _Turn, emit: Emit) -> None:
        """Record a provider-native child agent. Seeing it is not controlling it.

        Field names follow the local schema capture (ThreadItem variants
        ``collabAgentToolCall`` and ``subAgentActivity``). The collab call's
        ``prompt`` is deliberately not copied.
        """
        kind = item.get("type")
        if kind == "collabAgentToolCall":
            children = [t for t in (item.get("receiverThreadIds") or []) if isinstance(t, str)]
            entry = {"item_type": kind, "item_id": str(item.get("id") or ""),
                     "tool": str(item.get("tool") or ""), "status": str(item.get("status") or ""),
                     "child_thread_ids": children}
        else:
            child = item.get("agentThreadId")
            entry = {"item_type": kind, "item_id": str(item.get("id") or ""), "tool": "",
                     "status": str(item.get("kind") or ""),
                     "child_thread_ids": [child] if isinstance(child, str) else []}
        turn.native_agents.append(entry)
        emit(STREAM_NATIVE_AGENT, entry)

    def interrupt_invocation(self, invocation_id: str) -> dict[str, Any]:
        """Send ``turn/interrupt`` for a turn this process is driving.

        An accepted request is not a stop. The running ``start_streaming``
        keeps following the turn; only its ``turn/completed`` with a terminal
        status settles the call (``interrupted`` -> ``outcome="canceled"``).
        """
        with self._lock:
            live = self._live.get(invocation_id)
        if live is None:
            return {"interrupt_requested": False, "interrupt_accepted": False,
                    "remote_stop_confirmed": False, "reason": "no_live_turn"}
        client, turn = live
        turn.interrupt_requested = True
        try:
            client.request(TURN_INTERRUPT, {"threadId": turn.thread_id, "turnId": turn.turn_id},
                           timeout=INTERRUPT_REQUEST_TIMEOUT)
        except AppServerResponseError as exc:
            return {"interrupt_requested": True, "interrupt_accepted": False,
                    "remote_stop_confirmed": False, "reason": f"server_refused:{exc.code}"}
        except AppServerError as exc:
            return {"interrupt_requested": True, "interrupt_accepted": False,
                    "remote_stop_confirmed": False, "reason": type(exc).__name__}
        turn.interrupt_accepted = True
        return {"interrupt_requested": True, "interrupt_accepted": True,
                "remote_stop_confirmed": False, "reason": "awaiting_terminal_event"}

    @staticmethod
    def _has_unrouted(client: AppServerClient, turn: _Turn) -> bool:
        return any(not r.answered and r.id not in turn.cards for r in client.server_requests())

    def _route_requests(self, client: AppServerClient, turn: _Turn, emit: Emit,
                        gate: Any, wall_deadline: dt.datetime) -> None:
        """New tool requests of THIS turn become cards; everything else is declined."""
        for request in client.server_requests():
            if request.answered or request.id in turn.cards:
                continue
            params = request.params or {}
            if (gate is not None and requires_explicit_decision(request.method)
                    and turn.ours(params)):
                try:
                    card_id = gate.open(
                        method=request.method, request_key=json.dumps(request.id),
                        thread_id=turn.thread_id, turn_id=turn.turn_id,
                        item_id=str(params.get("itemId") or ""),
                        details={k: v for k, v in describe_approval(params).items()
                                 if k not in ("itemId", "threadId", "turnId",
                                              "availableDecisions")},
                        available_decisions=params.get("availableDecisions"),
                        deadline=wall_deadline,
                    )
                except Exception:  # noqa: BLE001
                    card_id = None
                if card_id:
                    turn.cards[request.id] = (request, card_id)
                    turn.card_ids.append(card_id)
                    continue
            self._decline_one(client, turn, emit, request)

    def _answer_decided(self, client: AppServerClient, turn: _Turn, gate: Any) -> None:
        """Send the user's decision for each card that left ``pending``."""
        if gate is None or not turn.cards:
            return
        now_mono = time.monotonic()
        if now_mono - turn.last_card_poll < CARD_POLL_SECONDS:
            return
        turn.last_card_poll = now_mono
        for request_id, (request, card_id) in list(turn.cards.items()):
            try:
                status = gate.poll(card_id)
            except Exception:  # noqa: BLE001
                continue
            if status == "pending":
                continue
            # Only an approved card becomes accept; declined/expired/void and
            # anything unexpected are declined.
            decision = _fit_decision("accept" if status == "approved" else "decline",
                                     (request.params or {}).get("availableDecisions"))
            ok = True
            try:
                client.answer_approval(request, decision)
            except AppServerError:
                ok = False
            try:
                gate.delivered(card_id, decision=decision, ok=ok)
            except Exception:  # noqa: BLE001
                pass
            del turn.cards[request_id]

    def _decline_one(self, client: AppServerClient, turn: _Turn, emit: Emit, request: Any) -> None:
        self._decline_requests(client, turn, emit, only=request)

    def _decline_requests(self, client: AppServerClient, turn: _Turn, emit: Emit,
                          only: Any = None) -> None:
        for request in ([only] if only is not None else client.server_requests()):
            if request.answered:
                continue
            item_id = str((request.params or {}).get("itemId") or "")
            try:
                if requires_explicit_decision(request.method):
                    client.answer_approval(request, "decline")
                    decision = "decline"
                else:
                    client.respond(request.id, error={"code": -32601,
                                                      "message": "not supported by agent-hub"})
                    request.answered = True
                    decision = "unsupported"
            except AppServerError:
                continue  # Disconnected; the pump notices on its next pass.
            turn.declined.append(request.method)
            emit(STREAM_REQUEST_DECLINED, {"method": request.method, "item_id": item_id,
                                           "decision": decision})

    def _close(self, invocation_id: str, transport: Any) -> bool:
        with self._lock:
            self._active.pop(invocation_id, None)
        try:
            transport.close()
        except Exception:  # noqa: BLE001 - AppServerTimeout: child exit unconfirmed
            return False
        alive = getattr(transport, "alive", None)
        return not alive if isinstance(alive, bool) else True

    def _settle(self, invocation_id: str, thread_id: str, turn: Optional[_Turn],
                turn_requested: bool, failure: Optional[str], timed_out: bool,
                stop_requested: bool, local_exit_confirmed: bool,
                resume_id: Optional[str], emit: Emit) -> AdapterStartResult:
        if failure is not None:
            verdict, terminal, detail = FAILED, True, failure
        elif stop_requested:
            verdict, terminal, detail = (
                RUNNING, False,
                "local transport stop requested; remote turn and native children not confirmed stopped")
        elif turn is not None and turn.terminal_seen:
            judged, terminal, detail = judge_turn_outcome(
                terminal_notification=TURN_COMPLETED, turn_status=turn.turn_status,
                interrupt_requested=turn.interrupt_requested,
                interrupt_accepted=turn.interrupt_accepted)
            verdict = {"finished": FINISHED, "failed": FAILED,
                       "cancelled": CANCELLED}.get(judged, RUNNING)
        else:
            judged, terminal, detail = judge_turn_outcome(
                transport_closed=not timed_out, timed_out=timed_out,
                interrupt_requested=bool(turn and turn.interrupt_requested),
                interrupt_accepted=bool(turn and turn.interrupt_accepted))
            verdict = RUNNING
            if turn is not None and turn.interrupt_requested and "interrupt" not in detail:
                # judge_turn_outcome reports the timeout first; the record must
                # still say an interrupt was asked for and never confirmed.
                detail += ("; interrupt "
                           f"{'accepted by the server' if turn.interrupt_accepted else 'requested'}"
                           " but no terminal turn status")
        if verdict == FINISHED and not local_exit_confirmed:
            detail += "; local app-server exit unconfirmed"

        payload = {
            "transport": TRANSPORT,
            "thread_id": thread_id,
            "turn_id": turn.turn_id if turn else "",
            "resumed": bool(resume_id),
            "turn_requested": turn_requested,
            "turn_status": turn.turn_status if turn else "",
            "final_message": turn.final_message if turn else "",
            "usage": turn.usage if turn else {},
            "declined_requests": list(turn.declined) if turn else [],
            "tool_approvals": list(turn.card_ids) if turn else [],
            "interrupt_requested": bool(turn and turn.interrupt_requested),
            "interrupt_accepted": bool(turn and turn.interrupt_accepted),
            "native_agents": list(turn.native_agents) if turn else [],
            "foreign_events": turn.foreign_events if turn else 0,
            "local_process_exit_confirmed": local_exit_confirmed,
            "verdict": verdict,
            "terminal": terminal,
            "detail": detail,
        }
        if thread_id and terminal and turn_requested:
            payload["native_session"] = {"transport": TRANSPORT, "thread_id": thread_id}
        self.store.set_payload(invocation_id, payload)
        self.store.set_status(invocation_id, verdict, detail[:400])
        if turn is not None:
            emit(STREAM_TURN_ENDED, {"turn_status": turn.turn_status, "verdict": verdict,
                                     "terminal": terminal})
        if verdict == FINISHED:
            return AdapterStartResult(thread_id, ok=True, terminal=True, detail=detail[:200])
        if verdict == CANCELLED:
            # The provider itself reported the turn interrupted: a confirmed stop.
            return AdapterStartResult(thread_id, ok=False, terminal=True, detail=detail[:200],
                                      outcome="canceled")
        if verdict == FAILED:
            return AdapterStartResult(thread_id, ok=False, terminal=True, detail=detail[:200])
        return AdapterStartResult(thread_id, ok=False, terminal=False, detail=detail[:200])

    # -- stop / cancel / status --------------------------------------------

    def cancel(self, adapter_invocation_id: str) -> Any:
        return {"ok": False, "reason": "unsupported",
                "detail": "turn/interrupt is unverified (S4); not claiming a stop"}

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self.cancel(invocation_id)

    def stop_local_invocation(self, invocation_id: str) -> dict:
        """Close only our own transport. Never a remote or native-child stop."""
        with self._lock:
            self._stop_requests.add(invocation_id)
            transport = self._active.get(invocation_id)
        if transport is None:
            return {"local_stop_requested": True, "local_process_exited": False,
                    "remote_stop_confirmed": False, "reason": "no_owned_process_or_launch_pending"}
        try:
            transport.close()
            exited = True
        except Exception:  # noqa: BLE001
            exited = False
        alive = getattr(transport, "alive", None)
        if isinstance(alive, bool):
            exited = exited and not alive
        return {"local_stop_requested": True, "local_process_exited": exited,
                "remote_stop_confirmed": False, "reason": "local_entry_process_only"}

    def get_status(self, adapter_invocation_id: str) -> Any:
        record = self.store.get(adapter_invocation_id)
        return {"ok": record is not None, "record": record}

    def lookup_by_invocation(self, invocation_id: str) -> Optional[dict[str, Any]]:
        return self.store.get(invocation_id)

    def last_result(self, invocation_id: str) -> Optional[dict[str, Any]]:
        record = self.store.get(invocation_id)
        if not record:
            return None
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        result = {
            "status": record.get("status"),
            "thread_id": payload.get("thread_id", ""),
            "final_message": payload.get("final_message", ""),
            "usage": payload.get("usage", {}),
        }
        if isinstance(payload.get("native_session"), dict):
            result["native_session"] = payload["native_session"]
        return result


class _Refused(Exception):
    """A setup answer we will not continue with; no turn was requested."""
