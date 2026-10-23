"""Codex over app-server: an optional transport beside ``codex exec``.

One call = one app-server process (or offline stub), one thread, one turn:

    initialize -> initialized -> thread/start | thread/resume -> turn/start
    -> stream notifications until this turn's ``turn/completed``.

What it adds over exec: incremental assistant text (reported through the
Worker's ``emit`` into the conversation event stream) and native thread resume
(``snapshot.resume_thread_id``).

Admission (S6, docs/appserver-s6-plan.md §4.1): the two trusted offline stub
factories are ``simulation``. A real app-server stays blocked in strict mode
(native-child control and tool pre-interception are unverified). In supervised
mode it is admitted only through :class:`CodexAppServerFactory` built from the
reviewed :data:`PINNED_APPSERVER` constants, with no environment override, the
verified argv, a proxy and a readable CA bundle, and a local deadline of at most
600 s. Supervised is a boundary waiver the user accepts per call, not a control
claim. S3-S5 each passed once through standalone probes (this run only); nothing
here has run against Codex through the Hub.

Rules this adapter enforces:

* the call's snapshot must carry this adapter's :meth:`binding_identity` (D11);
  the factory re-hashes the pinned file right before launching it;
* ``thread/start``, ``thread/resume`` and ``turn/start`` send read-only,
  on-request and reviewer ``user``; the answer of the thread request is read
  back, and any mismatch fails the call before ``turn/start`` (D2);
* a tool request of this turn becomes a kernel card when the Worker supplies a
  ``tool_gate``. The only way an answer is written is the gate's claim right
  before writing; after the call's tool decisions are closed nothing is sent,
  and a void card is never answered. A real app-server's cards offer decline
  only (accept is unverified, S7); a decline is always sent as ``decline`` and
  nothing is ever turned into ``cancel`` (D1). Without a gate every request is
  declined;
* only this turn's ``turn/completed`` with a terminal status settles a call;
  timeout, disconnect or an unanswered ``turn/start`` stays unconfirmed, so the
  kernel keeps the quota slot and marks the call ``unknown``. Events count only
  with this thread's and this turn's ids (D8); the one usage replay a resume
  sends for the previous turn is recorded apart (D9);
* ``interrupt_invocation`` sends ``turn/interrupt`` for a live turn only; an
  acknowledgement proves nothing and a slow one is "not yet acknowledged" (D5)
  - the call settles ``canceled`` only when this turn's ``turn/completed`` says
  ``interrupted``. Generic ``cancel`` stays ``unsupported``; a local stop closes
  only our own transport, and descendants are recorded, never ended (D10);
* tool items and native child agents are observed and reported, never
  controlled or counted by this adapter (D4).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import ntpath
import os
import threading
import time
from typing import Any, Callable, Optional

from agenthub.contracts import (
    NATIVE_CHILD_END_STATES,
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
from agenthub_adapters import proc_tree
from agenthub_adapters.appserver import (
    DELTA_AGENT_MESSAGE,
    ITEM_COMPLETED,
    ITEM_STARTED,
    THREAD_RESUME,
    THREAD_START,
    TURN_COMPLETED,
    TURN_INTERRUPT,
    TURN_START,
    TURN_STARTED,
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

#: The reviewed app-server identity (D7). Not shared with exec and never read
#: from the environment; a change here is a code review decision and changes
#: every new call's binding identity (D11). Re-pinning: plan §4.2.
PINNED_APPSERVER = {
    "path": os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "OpenAI", "Codex", "bin", "6257d9e8f63c0f2b", "codex.exe"),
    "sha256": "00c0f1e4099246fddcd67ec620ca8ed50827c7d70e113b7224f7a2933cf2d67c",
    "size": 342187312,
    "version": "codex-cli 0.163.0-alpha.11",
}
#: The argv S3-S5 ran with. ``--disable multi_agent`` turns off Collab only and
#: is not consulted when the model declares a multi-agent version (plan §2.1):
#: it is sent as a request, never shown as "sub-agents disabled".
APPSERVER_ARGV_TAIL = ("app-server", "--stdio", "--disable", "multi_agent")

#: Settings sent with thread/start and thread/resume, then read back (D2).
THREAD_SETTINGS = {"sandbox": "read-only", "approvalPolicy": "on-request", "approvalsReviewer": "user"}
#: Policy repeated on every turn/start.
TURN_POLICY = {"approvalPolicy": "on-request", "approvalsReviewer": "user",
               "sandboxPolicy": {"type": "readOnly", "networkAccess": False}}
#: What a decline-only card offers: the user can refuse, nothing else (plan §4.4).
DECLINE_ONLY = ["decline"]

REAL_APPSERVER_BLOCK_REASON = (
    "严格模式不启动真实 app-server：原生子 Agent 的事前控制与工具事前拦截均未验证"
    "（S3–S5 只各验证了一条窄路径，只认定当次）。"
)
SUPERVISED_REASON = (
    "仅保证 Hub 审批、固定身份与固定设置核验、本次入口进程控制；工具审批只能拒绝。"
    "原生子 Agent 与不经审批的工具操作（可能包括启动其他 Agent 的 CLI）不在 Hub 配额内，"
    "停止只关闭本次入口进程，后代进程与远端停止不保证。"
)

Emit = Callable[[str, dict], None]

#: How often a pending card's status is re-read while the turn waits.
CARD_POLL_SECONDS = 0.1
#: How long to wait for the server to acknowledge ``turn/interrupt``. The
#: acknowledgement is not the stop; the terminal event is.
INTERRUPT_REQUEST_TIMEOUT = 5.0
#: Thread item types that mean a provider-native child agent (local schema).
NATIVE_AGENT_ITEM_TYPES = frozenset({"collabAgentToolCall", "subAgentActivity"})
#: Thread item types that are tool activity (observed, never controlled).
TOOL_ITEM_TYPES = frozenset({"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"})


def _tool_notification_kind(method: str) -> Optional[str]:
    """``commandExecution`` for ``item/commandExecution/outputDelta`` and the like.

    Item-scoped tool notifications (output deltas, patch updates, progress)
    are activity evidence of their own, even when no item/started was seen.
    """
    parts = method.split("/")
    if len(parts) == 3 and parts[0] == "item" and parts[1] in TOOL_ITEM_TYPES:
        return parts[1]
    return None


def _ignore(_etype: str, _payload: dict) -> None:
    return None


def _utc_naive() -> dt.datetime:
    """Naive UTC, matching how the kernel stores ``expires_at``."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _fit_decision(decision: str, offered: Any) -> str:
    """What to write for a decision. Never ``cancel``; never upgraded to accept.

    A decline is sent as ``decline`` even when the provider's list omits it:
    alpha.11's default list is accept / (amendment) / cancel, and S3 showed the
    server honours ``decline`` regardless, while ``cancel`` would abort the
    whole turn (D1). An accept the provider did not offer becomes a decline.
    """
    if decision == "accept" and (not isinstance(offered, list) or "accept" in offered):
        return "accept"
    return "decline"


def settings_problems(result: dict, cwd: str) -> list[str]:
    """Read-back of a thread/start or thread/resume answer (S3-S5 rule)."""
    problems = []
    if result.get("approvalsReviewer") != "user":
        problems.append("approvalsReviewer")
    if result.get("approvalPolicy") != "on-request":
        problems.append("approvalPolicy")
    sandbox = result.get("sandbox")
    if not (isinstance(sandbox, dict) and sandbox.get("type") == "readOnly"
            and sandbox.get("networkAccess", False) is False
            and set(sandbox) <= {"type", "networkAccess"}):
        problems.append("sandbox")
    got = result.get("cwd")
    if not (isinstance(got, str) and os.path.isabs(got)
            and os.path.normcase(os.path.abspath(got)) == os.path.normcase(os.path.abspath(cwd))):
        problems.append("cwd")
    return problems


#: Characters a shell quoter leaves unquoted (Python's shlex.quote safe set, ASCII).
_UNQUOTED_SAFE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@%+=:,./_-")


def split_quoted_argv(text: str) -> Optional[list[str]]:
    """Exact POSIX word splitting of a ``shlex_join`` string, or None if not reliable.

    Only what a shell quoter produces is accepted: bare words of safe
    characters, '...' and "..." (where only \\ \" \\$ \\` escape). Anything
    else - an unquoted backslash (a POSIX escape, but a Windows path
    separator), unquoted shell syntax or non-ASCII, an unbalanced quote - has
    two plausible readings, so no split is offered and the original is shown.
    """
    words: list[str] = []
    word: list[str] = []
    in_word = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t":
            if in_word:
                words.append("".join(word))
                word, in_word = [], False
            i += 1
            continue
        in_word = True
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                return None
            word.append(text[i + 1:end])
            i = end + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    return None
                c = text[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and text[i + 1] in '\\"$`':
                    word.append(text[i + 1])
                    i += 2
                    continue
                if c in "$`":
                    return None  # expansion inside double quotes: not a literal
                word.append(c)
                i += 1
        elif c in _UNQUOTED_SAFE:
            word.append(c)
            i += 1
        else:
            return None
    if in_word:
        words.append("".join(word))
    return words


def command_view(text: Any) -> dict:
    """The reported ``command`` split back into argv, for display only (D3).

    ``command`` is ``shlex_join`` of the shell's argv (S3); a PowerShell or cmd
    wrapper is shown as the shell plus the inner command. When the string
    cannot be split without guessing (see :func:`split_quoted_argv`), nothing
    rewritten is offered: the page shows the original only. This is a reading
    aid, never a safety decision (the S3 scope check is separate and strict).
    """
    if not isinstance(text, str) or not text or "\0" in text or "\n" in text:
        return {"parsed": False}
    argv = split_quoted_argv(text)
    if not argv:
        return {"parsed": False}
    name = ntpath.basename(argv[0]).lower()
    if (name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh") and len(argv) >= 3
            and argv[-2] == "-Command" and set(argv[1:-2]) <= {"-NoProfile"}):
        return {"parsed": True, "shell": argv[0], "shell_flags": argv[1:-1], "inner_command": argv[-1]}
    if name in ("cmd.exe", "cmd") and len(argv) == 3 and argv[1].lower() == "/c":
        return {"parsed": True, "shell": argv[0], "shell_flags": [argv[1]], "inner_command": argv[2]}
    return {"parsed": True, "argv": argv}


def resume_structure(result: dict, previous_turn_id: str) -> dict:
    """Structural facts of a thread/resume answer about the previous turn; no text."""
    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
    turns = [t for t in thread.get("turns") or [] if isinstance(t, dict)]
    previous = next((t for t in turns if t.get("id") == previous_turn_id), None)
    items = [i for i in (previous or {}).get("items") or [] if isinstance(i, dict)]
    return {"turns_returned": len(turns), "previous_turn_present": previous is not None,
            "previous_turn_status": str(previous.get("status") or "") if previous else "",
            "previous_turn_user_message": any(i.get("type") == "userMessage" for i in items)}


def _thread_id(result: dict) -> str:
    thread = result.get("thread")
    if isinstance(thread, dict):
        return str(thread.get("id") or "")
    return str(result.get("threadId") or "")


class LaunchRefused(OSError):
    """The factory will not start the process; nothing was launched."""


class CodexAppServerFactory:
    """Real ``codex app-server --stdio`` launcher (supervised mode only).

    Build it with :meth:`pinned`; admission compares every field with
    :data:`PINNED_APPSERVER`, so a factory pointed anywhere else is refused.
    """

    #: How long a descendant may take to exit after its parent was closed (S5).
    close_grace_seconds = 3.0

    def __init__(self, *, codex_path: str, codex_home: str, expected_identity: Optional[dict],
                 version: str = "unknown", proxy: Optional[str] = None,
                 ca_bundle: Optional[str] = None, env_overrides: tuple[str, ...] = ()) -> None:
        self.codex_path = codex_path
        self.codex_home = codex_home
        self.expected_identity = expected_identity
        self.version = version
        self.proxy = proxy
        self.ca_bundle = ca_bundle
        #: Identity environment variables that were set (they are never used
        #: here; their presence blocks supervised admission).
        self.env_overrides = tuple(env_overrides)
        self.argv = [codex_path, *APPSERVER_ARGV_TAIL]

    @classmethod
    def pinned(cls, *, codex_home: str, proxy: Optional[str] = None, ca_bundle: Optional[str] = None,
               env_overrides: tuple[str, ...] = ()) -> "CodexAppServerFactory":
        return cls(codex_path=PINNED_APPSERVER["path"], codex_home=codex_home,
                   expected_identity={"sha256": PINNED_APPSERVER["sha256"],
                                      "size": PINNED_APPSERVER["size"]},
                   version=PINNED_APPSERVER["version"], proxy=proxy, ca_bundle=ca_bundle,
                   env_overrides=env_overrides)

    def binding_identity(self) -> str:
        ident = self.expected_identity or {}
        return (f"codex-app-server@{self.version};sha256={ident.get('sha256', '')};"
                f"size={ident.get('size', '')}")

    def identity_matches(self) -> tuple[bool, str]:
        from agenthub_adapters.codex import file_identity

        if not self.codex_path or not os.path.isfile(self.codex_path):
            return False, "executable missing"
        if not self.expected_identity:
            return False, "no expected identity recorded (not pinned)"
        actual = file_identity(self.codex_path)
        for key in ("size", "sha256"):
            if self.expected_identity.get(key) != actual.get(key):
                return False, f"{key} mismatch"
        return True, "identity matches"

    def supervised_problems(self) -> list[str]:
        """Cheap checks for admission (it runs under the kernel's writer lock).

        The full SHA-256 of the ~340 MB file is computed at launch instead.
        """
        problems = []
        if self.env_overrides:
            problems.append("env_override:" + ",".join(self.env_overrides))
        pinned = PINNED_APPSERVER
        if (os.path.normcase(self.codex_path or "") != os.path.normcase(pinned["path"])
                or (self.expected_identity or {}) != {"sha256": pinned["sha256"], "size": pinned["size"]}
                or self.version != pinned["version"]):
            problems.append("identity_not_pinned_constant")
        if not self.codex_path or not os.path.isfile(self.codex_path):
            problems.append("executable_missing")
        elif os.path.getsize(self.codex_path) != (self.expected_identity or {}).get("size"):
            problems.append("size_mismatch")
        if self.argv != [self.codex_path, *APPSERVER_ARGV_TAIL]:
            problems.append("argv_changed")
        if not self.proxy:
            problems.append("proxy_missing")
        if not (self.ca_bundle and os.path.isfile(self.ca_bundle) and os.access(self.ca_bundle, os.R_OK)):
            problems.append("ca_unreadable")
        return problems

    def __call__(self, invocation_id: str, workdir: str) -> Any:
        from agenthub_adapters.appserver import StdioTransport
        from agenthub_adapters.codex import build_env

        problems = self.supervised_problems()
        if problems:
            raise LaunchRefused("launch refused: " + ", ".join(problems))
        ok, why = self.identity_matches()  # full hash, right before the launch
        if not ok:
            raise LaunchRefused(f"launch refused: {why}")
        env = build_env(codex_home=self.codex_home, proxy=self.proxy, ca_bundle=self.ca_bundle)
        return StdioTransport(list(self.argv), env=env, cwd=workdir,
                              stderr_path=log_path(workdir, invocation_id, "appserver-stderr"))


def log_path(workdir: str, invocation_id: str, kind: str) -> str:
    """Diagnostics live beside the work directories, never inside the cwd (D6)."""
    logs = os.path.join(os.path.dirname(os.path.abspath(workdir)), "_logs")
    os.makedirs(logs, exist_ok=True)
    return os.path.join(logs, f"{invocation_id}-{kind}.log")


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

    def __init__(self, thread_id: str, turn_id: str, previous_turn_id: str = "") -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        #: Resume only: the thread's previous turn, owner of the one usage replay.
        self.previous_turn_id = previous_turn_id
        self.deltas: dict[str, list[str]] = {}
        self.final_message = ""
        self.usage: dict = {}
        self.replayed_usage: Optional[dict] = None
        self.usage_anomalies = 0
        self.turn_started_seen = False
        self.turn_status = ""
        self.terminal_seen = False
        self.foreign_events = 0
        self.late_events = 0
        self.declined: list[str] = []
        # request id -> (ServerRequest, card id) while the user decides.
        self.cards: dict[Any, tuple[Any, str]] = {}
        self.card_ids: list[str] = []
        self.last_card_poll = 0.0
        # Requests the gate refused to answer (decisions closed): they stay
        # unanswered on purpose and must never be routed again - re-routing
        # would fall back to an automatic decline after the effective point.
        self.closed_requests: set[Any] = set()
        # Methods of requests deliberately left unanswered by the gate.
        self.unanswered: list[str] = []
        # Cards whose (forged or unexpected) approval was not sent as accept.
        self.accept_suppressed: list[str] = []
        self.interrupt_requested = False
        self.interrupt_accepted = False
        # Provider-native child agents seen in this turn (ids/enums only).
        self.native_agents: list[dict] = []
        # child thread id -> its latest own state ("unknown" until reported).
        self.child_state: dict[str, str] = {}
        # Tool items of this turn (type, id, process id seen, completed).
        self.tools: dict[str, dict] = {}

    def unfinished_children(self) -> list[str]:
        return [t for t, s in self.child_state.items() if s not in NATIVE_CHILD_END_STATES]

    def tool_process_state(self) -> str:
        """``none`` / ``items_completed`` / ``unknown`` - never "stopped".

        A completed item is the provider's report about the item; it is not a
        separate confirmation that a process it started has exited.
        """
        if not self.tools:
            return "none"
        # Output without a completed item (or without any item) is no end evidence.
        return "items_completed" if all(t["completed"] for t in self.tools.values()) else "unknown"

    def tool_entry(self, item_id: str, kind: str) -> dict:
        return self.tools.setdefault(item_id, {
            "item_type": kind, "item_id": item_id, "process_id_reported": False,
            "completed": False, "status": "", "lifecycle_seen": False, "output_events": 0})

    def count_late(self, notes: list) -> None:
        """Bound events already received after this turn's terminal (§4.8)."""
        self.late_events += sum(1 for n in notes if (n.params or {}).get("threadId") == self.thread_id)

    def ours(self, params: dict, *, turn_from_payload: bool = False) -> bool:
        """Exact binding: both this thread's and this turn's id must be present (D8)."""
        if params.get("threadId") != self.thread_id:
            return False
        got = turn_id_of_params(params) if turn_from_payload else params.get("turnId")
        return got == self.turn_id


def turn_id_of_params(params: dict) -> str:
    turn = params.get("turn") or {}
    return str(turn.get("id") or "") if isinstance(turn, dict) else ""


class AppServerCodexAdapter(AdapterContract):
    """Codex via app-server. Gated like the exec adapter, plus D1-D11."""

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
        # Requested on every thread request and read back before any turn.
        self.sandbox = "read-only"
        self.disable_subagents = True  # requested via --disable; not proof, not control
        #: Only the trusted offline stubs may answer accept. A real app-server
        #: offers decline only until the accept chain is verified (S7).
        self.accept_enabled = _trusted_offline(transport_factory)
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

    def binding_identity(self) -> str:
        """What a call's snapshot binds to (D11); changes with the pinned identity."""
        bind = getattr(self.transport_factory, "binding_identity", None)
        if not callable(bind):
            return "unavailable"
        try:
            value = bind()
        except Exception:  # noqa: BLE001
            return "unavailable"
        if not isinstance(value, str) or not value or value == "unknown":
            return "unavailable"  # never matches, on either side
        return value

    def capabilities(self) -> dict[str, Any]:
        return {
            "post_message": True,
            "stream": True,
            "resume_session": True,
            "cancel": False,  # no stop without a live turn (e.g. after a restart)
            "interrupt_live_turn": True,  # turn/interrupt; settles only on the terminal event
            "observe_native_agents": True,  # collab/sub-agent items; observation, not control
            "observe_tool_items": True,  # tool items; observation, not control
            "stop_local_process": True,
            "limit_dir": "unknown",
            "intercept_tool": False,
            "control_subagent": False,
            "real_usage": "unknown",
            "tool_accept": self.accept_enabled,  # offline stubs only
            # Nothing has been verified through the Hub. Standalone probes,
            # each this run only (docs/appserver-s6-plan.md §2):
            "verified": {"post_message": False, "stream": False, "resume_session": False,
                         "cancel": False, "intercept_tool": False, "control_subagent": False},
            "probe_evidence": {
                "decline_one_command_approval": "evidence/s3-real-run-3-pass",
                "interrupt_before_first_output": "evidence/s4-real-run-1-pass",
                "cold_resume_history": "evidence/s5-real-run-2-pass",
            },
            "transport": TRANSPORT,
        }

    def execution_admission(self) -> ExecutionAdmission:
        if _trusted_offline(self.transport_factory):
            return ExecutionAdmission(True, "simulation", "offline_fixture",
                                      "离线 app-server 桩验证，不调用真实 Codex。")
        return ExecutionAdmission(False, "blocked", "appserver_real_unverified",
                                  REAL_APPSERVER_BLOCK_REASON)

    def supervised_execution_admission(self) -> ExecutionAdmission:
        factory = self.transport_factory
        if type(factory) is not CodexAppServerFactory:
            return ExecutionAdmission(False, "blocked", "appserver_real_unverified",
                                      REAL_APPSERVER_BLOCK_REASON)
        try:
            problems = factory.supervised_problems()
        except Exception:  # noqa: BLE001
            problems = ["factory_check_failed"]
        if not (isinstance(self.timeout_seconds, (int, float)) and math.isfinite(self.timeout_seconds)
                and 0 < self.timeout_seconds <= 600):
            problems.append("timeout")
        if self.accept_enabled:
            problems.append("tool_accept_enabled")  # decline-only until S7

        if problems:
            return ExecutionAdmission(
                False, "blocked", "appserver_supervised_conditions",
                "用户监督模式要求代码常量中的固定身份（无环境变量覆盖）、验证过的启动参数、"
                "代理和可读的 CA、最多 600 秒的本地时限。未满足：" + ", ".join(problems))
        return ExecutionAdmission(True, "supervised", "user_supervised_boundary", SUPERVISED_REASON)

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
        binding = self.binding_identity()
        if binding == "unavailable" or snapshot.adapter_version != binding:
            # The kernel refuses this before consuming the approval; reaching
            # here means it was bypassed. Nothing is launched either way.
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: approved for another adapter identity; nothing started")
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

    def _thread_home(self, thread_id: str) -> Optional[tuple[str, str]]:
        """(cwd, previous turn id) of the latest call that started a turn on a thread (D6)."""
        for record in self.store.records_for_adapter_id(thread_id):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            workdir, turn_id = payload.get("workdir"), payload.get("turn_id")
            if isinstance(workdir, str) and workdir and isinstance(turn_id, str) and turn_id:
                return workdir, turn_id
        return None

    def _run(self, invocation_id: str, snapshot: InvocationSnapshot,
             resume_id: Optional[str], emit: Emit, gate: Any = None) -> AdapterStartResult:
        previous_turn_id = ""
        if resume_id:
            home = self._thread_home(resume_id)
            if home is None or not os.path.isdir(home[0]):
                return AdapterStartResult(
                    "", ok=False, terminal=True,
                    detail="refused: no recorded work directory for this provider thread; nothing started")
            workdir, previous_turn_id = home
        else:
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
        descendants: Optional[dict] = None
        # What actually went over the wire, for audits (no text, ids and hashes only).
        pid = getattr(transport, "pid", None)
        facts: dict[str, Any] = {
            "process_id": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
            "opening_method": "", "input_sha256": "", "resume_check": None, "turn_id_reused": False,
        }
        try:
            client.start()
            session.initialize(client_name="agent-hub", version=self.version)
            if resume_id:
                facts["opening_method"] = THREAD_RESUME
                result = client.request(THREAD_RESUME, {"threadId": resume_id, "cwd": workdir,
                                                        **THREAD_SETTINGS})
                thread_id = _thread_id(result)
                if thread_id != resume_id:
                    raise _Refused("thread/resume returned a different thread id")
            else:
                facts["opening_method"] = THREAD_START
                result = client.request(THREAD_START, {"cwd": workdir, **THREAD_SETTINGS})
                thread_id = _thread_id(result)
            if not thread_id:
                raise _Refused("server returned no thread id")
            self.store.set_adapter_id(invocation_id, thread_id)
            problems = settings_problems(result, workdir)
            if problems:
                raise _Refused("appserver_settings_mismatch: " + ",".join(problems))
            if resume_id:
                facts["resume_check"] = resume_structure(result, previous_turn_id)
                if not facts["resume_check"]["previous_turn_present"]:
                    raise _Refused("thread/resume did not return the previous turn; history not confirmed")
            turn_requested = True
            facts["input_sha256"] = hashlib.sha256(snapshot.task_input.encode("utf-8")).hexdigest()
            result = client.request(TURN_START, {
                "threadId": thread_id, "input": [{"type": "text", "text": snapshot.task_input}],
                **TURN_POLICY,
            })
            turn_id = str((result.get("turn") or {}).get("id") or result.get("turnId") or "")
            if not turn_id:
                raise AppServerError("turn/start returned no turn id")
            # A resumed turn must be a NEW turn. If the server reuses the previous
            # id, this turn's events and the previous turn's usage replay cannot
            # be told apart: recorded, and the usage is not trusted.
            facts["turn_id_reused"] = bool(resume_id) and turn_id == previous_turn_id
            turn = _Turn(thread_id, turn_id, previous_turn_id)
            with self._lock:
                self._live[invocation_id] = (client, turn)
            emit(STREAM_TURN_STARTED, {"thread_id": thread_id, "turn_id": turn_id,
                                       "resumed": bool(resume_id)})
            timed_out = not self._pump(client, turn, emit, gate)
            if turn.terminal_seen:
                # Bound events after this turn's terminal are anomalies (§4.8).
                turn.count_late(client.notifications())
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
            local_exit_confirmed, descendants = self._close(invocation_id, transport)
            if gate is not None and turn is not None and turn.cards:
                # Unanswered requests died with the turn/transport: void them
                # so a late click cannot "approve" something no one will run.
                # A void card is never answered (plan §4.4).
                try:
                    gate.void_pending()
                except Exception:  # noqa: BLE001
                    pass

        with self._lock:
            stop_requested = invocation_id in self._stop_requests
            self._stop_requests.discard(invocation_id)
        return self._settle(invocation_id, thread_id, turn, turn_requested, failure,
                            timed_out, stop_requested, local_exit_confirmed, resume_id, emit,
                            workdir=workdir, descendants=descendants, facts=facts)

    def _pump(self, client: AppServerClient, turn: _Turn, emit: Emit, gate: Any = None) -> bool:
        """Follow this turn until its terminal event. False on timeout."""
        deadline = time.monotonic() + self.timeout_seconds
        wall_deadline = _utc_naive() + dt.timedelta(seconds=self.timeout_seconds)
        while time.monotonic() < deadline:
            if gate is not None and self._has_unrouted(client, turn):
                # Requests and notifications are queued separately by the
                # reader; apply what arrived before the request so its output
                # precedes the card in the event stream.
                if self._observe_batch(client.notifications(), turn, emit):
                    return True
            self._route_requests(client, turn, emit, gate, wall_deadline)
            self._answer_decided(client, turn, gate)
            note = client.wait_for(lambda _n: True, timeout=0.05)
            if note is None:
                if client.disconnected:
                    # Drain anything that arrived before EOF, then stop.
                    self._observe_batch(client.notifications(), turn, emit)
                    return True
                continue
            if self._observe(note, turn, emit):
                return True
        return False

    def _observe_batch(self, batch: list, turn: _Turn, emit: Emit) -> bool:
        """Observe a drained batch in order. True once this turn's terminal is seen.

        A drained batch has left the client's queue, so whatever follows the
        terminal in it is counted as late here instead of being lost.
        """
        for index, note in enumerate(batch):
            if self._observe(note, turn, emit):
                turn.count_late(batch[index + 1:])
                return True
        return False

    def _observe(self, note: Any, turn: _Turn, emit: Emit) -> bool:
        params = note.params or {}
        if note.method == TURN_STARTED:
            if turn.ours(params, turn_from_payload=True):
                turn.turn_started_seen = True
            else:
                turn.foreign_events += 1
            return False
        tool_kind = _tool_notification_kind(note.method)
        if tool_kind is not None:
            # Output deltas and the like: counted as activity, text never kept.
            if turn.ours(params):
                turn.tool_entry(str(params.get("itemId") or ""), tool_kind)["output_events"] += 1
            else:
                turn.foreign_events += 1
            return False
        if note.method in (ITEM_STARTED, ITEM_COMPLETED):
            item = params.get("item") or {}
            kind = item.get("type") if isinstance(item, dict) else None
            if kind in NATIVE_AGENT_ITEM_TYPES or kind in TOOL_ITEM_TYPES:
                if not turn.ours(params):
                    turn.foreign_events += 1
                elif kind in NATIVE_AGENT_ITEM_TYPES:
                    self._observe_native(item, turn, emit)
                else:
                    self._observe_tool(item, note.method, turn)
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
        if note.method == TOKEN_USAGE_UPDATED:
            last = (params.get("tokenUsage") or {}).get("last")
            if turn.ours(params):
                if isinstance(last, dict):
                    turn.usage = dict(last)
            elif (turn.previous_turn_id and params.get("threadId") == turn.thread_id
                  and params.get("turnId") == turn.previous_turn_id
                  and not turn.turn_started_seen and turn.replayed_usage is None):
                # The one restored usage a resume sends for the previous turn
                # (S5): recorded apart, never this turn's usage (D9).
                turn.replayed_usage = dict(last) if isinstance(last, dict) else {}
            else:
                turn.usage_anomalies += 1
                turn.foreign_events += 1
        return False

    @staticmethod
    def _observe_tool(item: dict, method: str, turn: _Turn) -> None:
        """Record a tool item: type, id, whether a process id was reported, completion.

        Command text and output are not kept. Observing is not intercepting.
        """
        entry = turn.tool_entry(str(item.get("id") or ""), str(item.get("type") or ""))
        entry["lifecycle_seen"] = True
        if item.get("processId"):
            entry["process_id_reported"] = True
        if method == ITEM_COMPLETED:
            entry["completed"] = True
            entry["status"] = str(item.get("status") or "")

    @staticmethod
    def _observe_native(item: dict, turn: _Turn, emit: Emit) -> None:
        """Record a provider-native child agent. Seeing it is not controlling it.

        Field names follow the local schema capture (ThreadItem variants
        ``collabAgentToolCall`` and ``subAgentActivity``). The collab call's
        ``prompt`` is deliberately not copied.
        """
        kind = item.get("type")
        ended: list[str] = []
        if kind == "collabAgentToolCall":
            children = [t for t in (item.get("receiverThreadIds") or []) if isinstance(t, str)]
            states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
            for child in children:
                turn.child_state.setdefault(child, "unknown")
            # Only a child's OWN state is end evidence; the collab call's own
            # status (e.g. a completed spawnAgent) is not. ``message`` is not copied.
            for child, state in states.items():
                status = state.get("status") if isinstance(state, dict) else None
                if isinstance(child, str) and isinstance(status, str):
                    turn.child_state[child] = status
                    if child not in children:
                        children.append(child)
                    if status in NATIVE_CHILD_END_STATES:
                        ended.append(child)
            entry = {"item_type": kind, "item_id": str(item.get("id") or ""),
                     "tool": str(item.get("tool") or ""), "status": str(item.get("status") or ""),
                     "child_thread_ids": children, "ended_child_thread_ids": ended}
        else:
            child = item.get("agentThreadId")
            activity = str(item.get("kind") or "")
            if isinstance(child, str):
                # started/interacted say it runs; completed/interrupted end it.
                turn.child_state[child] = activity or "unknown"
                if activity in NATIVE_CHILD_END_STATES:
                    ended.append(child)
            entry = {"item_type": kind, "item_id": str(item.get("id") or ""), "tool": "",
                     "status": activity,
                     "child_thread_ids": [child] if isinstance(child, str) else [],
                     "ended_child_thread_ids": ended}
        turn.native_agents.append(entry)
        emit(STREAM_NATIVE_AGENT, entry)

    def interrupt_invocation(self, invocation_id: str) -> dict[str, Any]:
        """Send ``turn/interrupt`` for a turn this process is driving.

        An accepted request is not a stop. The running ``start_streaming``
        keeps following the turn; only its ``turn/completed`` with a terminal
        status settles the call (``interrupted`` -> ``outcome="canceled"``).
        The tested build answers only once the turn is actually aborted (S4),
        so no answer within the wait means "not yet acknowledged" (D5).
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
        except AppServerTimeout:
            return {"interrupt_requested": True, "interrupt_accepted": False,
                    "remote_stop_confirmed": False, "reason": "not_yet_acknowledged"}
        except AppServerError as exc:
            return {"interrupt_requested": True, "interrupt_accepted": False,
                    "remote_stop_confirmed": False, "reason": type(exc).__name__}
        turn.interrupt_accepted = True
        return {"interrupt_requested": True, "interrupt_accepted": True,
                "remote_stop_confirmed": False, "reason": "awaiting_terminal_event"}

    @staticmethod
    def _gate_allows_fallback(gate: Any) -> bool:
        may_answer = getattr(gate, "may_answer", None)
        if not callable(may_answer):
            return False  # a gate that cannot say "open" allows nothing
        try:
            return bool(may_answer())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _has_unrouted(client: AppServerClient, turn: _Turn) -> bool:
        return any(not r.answered and r.id not in turn.cards and r.id not in turn.closed_requests
                   for r in client.server_requests())

    def _card_details(self, params: dict) -> dict:
        details = {k: v for k, v in describe_approval(params).items()
                   if k not in ("itemId", "threadId", "turnId", "availableDecisions")}
        if "command" in details:
            details["command_view"] = command_view(details["command"])
        if "reason" in details:
            details["reason_source"] = "model"  # the model's own explanation, not a fact
        if not self.accept_enabled:
            details["provider_available_decisions"] = params.get("availableDecisions")
        return details

    def _route_requests(self, client: AppServerClient, turn: _Turn, emit: Emit,
                        gate: Any, wall_deadline: dt.datetime) -> None:
        """New tool requests of THIS turn become cards; everything else is declined."""
        for request in client.server_requests():
            if request.answered or request.id in turn.cards or request.id in turn.closed_requests:
                continue
            params = request.params or {}
            if (gate is not None and requires_explicit_decision(request.method)
                    and turn.ours(params)):
                try:
                    card_id = gate.open(
                        method=request.method, request_key=json.dumps(request.id),
                        thread_id=turn.thread_id, turn_id=turn.turn_id,
                        item_id=str(params.get("itemId") or ""),
                        details=self._card_details(params),
                        available_decisions=(params.get("availableDecisions") if self.accept_enabled
                                             else list(DECLINE_ONLY)),
                        deadline=wall_deadline,
                    )
                except Exception:  # noqa: BLE001
                    card_id = None
                if card_id:
                    turn.cards[request.id] = (request, card_id)
                    turn.card_ids.append(card_id)
                    continue
            if gate is not None and not self._gate_allows_fallback(gate):
                # Card creation failed or the request is not ours, and the
                # gate does not confirm the call is open (closed, or state
                # unreadable): send nothing - not even an automatic decline.
                turn.closed_requests.add(request.id)
                turn.unanswered.append(request.method)
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
            claim = getattr(gate, "claim", None)
            if callable(claim):
                # Last check before writing: once the call's tool decisions are
                # closed (interrupt/stop effective point) this returns None and
                # the decision is recorded as blocked, never sent. A void card
                # is never claimable, so it is never answered.
                try:
                    claimed = claim(card_id)
                except Exception:  # noqa: BLE001
                    claimed = None
                if claimed is None:
                    del turn.cards[request_id]
                    turn.closed_requests.add(request_id)
                    continue
                answer = claimed
            else:
                # Only an approved card becomes accept; everything else declines.
                answer = "accept" if status == "approved" else "decline"
            if answer == "accept" and not self.accept_enabled:
                # Decline-only: no path may write accept, not even a forged row.
                answer = "decline"
                turn.accept_suppressed.append(card_id)
            decision = _fit_decision(answer, (request.params or {}).get("availableDecisions"))
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

    def _close(self, invocation_id: str, transport: Any) -> tuple[bool, Optional[dict]]:
        """Close our own transport; record (never end) its descendants (D10)."""
        with self._lock:
            self._active.pop(invocation_id, None)
        pid = getattr(transport, "pid", None)
        before = None
        if isinstance(pid, int) and not isinstance(pid, bool):
            try:
                before = proc_tree.snapshot_descendants(pid)
            except Exception:  # noqa: BLE001 - a record, never a reason to fail
                before = None
        try:
            transport.close()
            closed = True
        except Exception:  # noqa: BLE001 - AppServerTimeout: child exit unconfirmed
            closed = False
        alive = getattr(transport, "alive", None)
        exited = closed and (not alive if isinstance(alive, bool) else True)
        report = None
        if before is not None:
            grace = getattr(self.transport_factory, "close_grace_seconds", 0.0)
            try:
                left = proc_tree.survivors(before, float(grace)) if before else []
            except Exception:  # noqa: BLE001
                left = None
            report = {"before_close": [{"pid": e[0], "image": e[1]} for e in before],
                      "after_grace": None if left is None else [{"pid": e[0], "image": e[1]} for e in left],
                      "grace_seconds": float(grace)}
        return exited, report

    def _settle(self, invocation_id: str, thread_id: str, turn: Optional[_Turn],
                turn_requested: bool, failure: Optional[str], timed_out: bool,
                stop_requested: bool, local_exit_confirmed: bool,
                resume_id: Optional[str], emit: Emit, *, workdir: str = "",
                descendants: Optional[dict] = None,
                facts: Optional[dict] = None) -> AdapterStartResult:
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
        # The parent turn's own outcome, kept apart from the call's settlement.
        parent_turn = ({"turn_status": turn.turn_status, "verdict": verdict, "terminal": terminal}
                       if turn is not None else None)
        unfinished = turn.unfinished_children() if turn is not None else []
        if unfinished and terminal:
            # Native children without end evidence may still run (and spend)
            # outside Hub quota: the call cannot be confirmed finished.
            verdict, terminal = RUNNING, False
            detail = (f"parent turn ended {turn.turn_status or 'without status'}; native child "
                      f"agents without end evidence: {', '.join(unfinished)}; "
                      "not counted in Hub quota, quota slot kept")
        if (facts or {}).get("turn_id_reused"):
            detail += "; resumed turn reused the previous turn id: its usage is not attributable"
        tool_state = turn.tool_process_state() if turn is not None else "none"
        if tool_state == "unknown":
            detail += "; tool items without completion: tool process state unknown, not claimed stopped"
        survivors = (descendants or {}).get("after_grace")
        if survivors:
            detail += (f"; {len(survivors)} descendant process(es) still present after close "
                       "(recorded only; Hub does not end them)")
        if verdict == FINISHED and not local_exit_confirmed:
            detail += "; local app-server exit unconfirmed"

        payload = {
            "transport": TRANSPORT,
            "thread_id": thread_id,
            "turn_id": turn.turn_id if turn else "",
            "workdir": workdir,
            "resumed": bool(resume_id),
            "previous_turn_id": turn.previous_turn_id if turn else "",
            "turn_requested": turn_requested,
            "turn_status": turn.turn_status if turn else "",
            "final_message": turn.final_message if turn else "",
            "usage": turn.usage if turn else {},
            "replayed_usage": turn.replayed_usage if turn else None,
            "usage_anomalies": turn.usage_anomalies if turn else 0,
            "declined_requests": list(turn.declined) if turn else [],
            "unanswered_requests": list(turn.unanswered) if turn else [],
            "tool_approvals": list(turn.card_ids) if turn else [],
            "accept_suppressed": list(turn.accept_suppressed) if turn else [],
            "tool_activity": list(turn.tools.values()) if turn else [],
            "tool_process_state": tool_state,
            "interrupt_requested": bool(turn and turn.interrupt_requested),
            "interrupt_accepted": bool(turn and turn.interrupt_accepted),
            "native_agents": list(turn.native_agents) if turn else [],
            "native_children": dict(turn.child_state) if turn else {},
            "native_unfinished": unfinished,
            "parent_turn": parent_turn,
            "foreign_events": turn.foreign_events if turn else 0,
            "late_events": turn.late_events if turn else 0,
            "local_process_exit_confirmed": local_exit_confirmed,
            "descendants": descendants,
            "verdict": verdict,
            "terminal": terminal,
            "detail": detail,
            **(facts or {}),
        }
        if thread_id and terminal and turn_requested:
            payload["native_session"] = {"transport": TRANSPORT, "thread_id": thread_id}
        self.store.set_payload(invocation_id, payload)
        self.store.set_status(invocation_id, verdict, detail[:400])
        if turn is not None:
            emit(STREAM_TURN_ENDED, {"turn_status": turn.turn_status, "verdict": verdict,
                                     "terminal": terminal, "native_unfinished": unfinished,
                                     "tool_process_state": tool_state})
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
                "detail": "no stop without a live turn; turn/interrupt settles only on the terminal event"}

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self.cancel(invocation_id)

    def stop_local_invocation(self, invocation_id: str) -> dict:
        """Close only our own transport. Never a remote, descendant or native-child stop."""
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
