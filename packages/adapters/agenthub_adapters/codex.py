"""Codex adapter: the minimal real integration for P3.

Runs the locally installed Codex CLI non-interactively (``codex exec``) and turns
its JSONL event stream into the kernel's adapter result.

What this adapter does NOT do
-----------------------------
It does **not** grant user approval. Execution eligibility is checked here;
the approval gate lives in the
Worker/PolicyService: ``Worker._process_one`` only calls ``start()`` after
``consume()`` has atomically validated the approval and reserved a quota slot.
This adapter therefore refuses to start unless the caller passes an explicit
``gate_token`` produced by the kernel - defence in depth so that a future code
path cannot reach the process by accident. The tests assert that an unapproved
invocation results in **zero** process launches.

Safety posture (per project rules)
----------------------------------
* read-only sandbox (``--sandbox read-only``)
* ``--disable multi_agent`` requested, but a native spawn was observed despite it
* real launches are blocked under plan §4.3; only offline simulators are admitted
* the executable path is pinned and its identity (size + sha256) is checked
* proxy/CA/CODEX_HOME are injected into **this process only**
* ``cancel`` is reported as **unsupported**: cancellation has never been verified
  against Codex, and AGENTS.md forbids claiming a stop we cannot demonstrate.
  That is deliberate - it makes the kernel treat a timed-out call as ``unknown``
  and keep its quota slot instead of pretending it stopped.

Output capture note: stdout is redirected to a FILE, never a pipe. This
environment's sandbox denies piped stdio for native processes (measured), while
a file handle works.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import threading
import uuid
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agenthub.contracts import (
    AdapterContract,
    AdapterStartResult,
    ExecutionAdmission,
    InvocationSnapshot,
    execution_admission,
)

from agenthub.usage import MAPPING_CODEX_EXEC
from agenthub_adapters.store import (
    CANCELLED,
    FAILED,
    FINISHED,
    RUNNING,
    ExecutionStore,
)

#: Event types observed from a successful `codex exec --json` run.
EVENT_THREAD_STARTED = "thread.started"
EVENT_TURN_STARTED = "turn.started"
EVENT_ITEM_COMPLETED = "item.completed"
EVENT_TURN_COMPLETED = "turn.completed"
EVENT_TURN_FAILED = "turn.failed"
EVENT_ERROR = "error"

DEFAULT_SANDBOX = "read-only"
SUBAGENT_FEATURE = "multi_agent"

NATIVE_SUBAGENT_BLOCK_REASON = (
    "当前 Codex 配置曾在传入禁用参数后派生原生子 Agent，无法保证逐次审批和配额；"
    "不满足严格协作要求，已阻止新的真实调用。可使用 Mock 或手动交接。"
)


def blocked_codex_admission() -> ExecutionAdmission:
    return ExecutionAdmission(
        False, "blocked", "native_subagents_uncontrolled", NATIVE_SUBAGENT_BLOCK_REASON
    )


def bundled_stub_path() -> str:
    return os.path.realpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "tools", "demo", "stub_codex.py"
    ))


def is_bundled_stub_command(path: str, prefix: list[str]) -> bool:
    """Only the checked-in inert stub and this Python interpreter, no flags.

    An arbitrary command prefix or a claimed capability is never an opt-out.
    The stub is trusted source code, like the rest of the installed application.
    """
    return (
        bool(path)
        and os.path.normcase(os.path.realpath(path)) == os.path.normcase(bundled_stub_path())
        and len(prefix) == 1
        and os.path.normcase(os.path.realpath(prefix[0])) == os.path.normcase(os.path.realpath(sys.executable))
    )


class CodexNotConfigured(RuntimeError):
    """Misconfiguration of the Codex adapter.

    ``start()`` used to RAISE this when no executable was pinned. It now returns
    a terminal refusal instead: an exception thrown out of a start happens after
    the kernel has already consumed the approval and reserved the quota slot, so
    raising leaks the slot. Kept for import compatibility.
    """


class CodexLaunchFailed(RuntimeError):
    """``subprocess.Popen`` itself failed: provably nothing was launched.

    This is the one start-time error whose consequences are knowable. The
    operating system refused to create the process, so no Codex work exists and
    the quota slot may safely be freed. Every OTHER exception is ambiguous - the
    process may or may not be running - and must not be treated as terminal.
    """


@dataclass
class CodexRun:
    """Raw outcome of one `codex exec` process."""

    exit_code: Optional[int]
    timed_out: bool
    elapsed: float
    stdout_text: str
    argv: list[str]
    local_stop_requested: bool = False

    @property
    def ok(self) -> bool:
        return (not self.timed_out) and self.exit_code == 0


@dataclass
class ParsedRun:
    """What we extracted from the JSONL stream."""

    events: list[dict] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)
    thread_id: str = ""
    final_message: str = ""
    usage: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    event_types: dict = field(default_factory=dict)

    @property
    def turn_failed(self) -> bool:
        return any(e.get("type") == EVENT_TURN_FAILED for e in self.events)

    @property
    def turn_completed(self) -> bool:
        return any(e.get("type") == EVENT_TURN_COMPLETED for e in self.events)

    @property
    def turn_started(self) -> bool:
        return any(e.get("type") == EVENT_TURN_STARTED for e in self.events)

    @property
    def has_turn_events(self) -> bool:
        return any(
            e.get("type") in (EVENT_TURN_STARTED, EVENT_TURN_COMPLETED, EVENT_TURN_FAILED)
            for e in self.events
        )


# --------------------------------------------------------------------------- #
# argv / env construction
# --------------------------------------------------------------------------- #


def build_argv(
    codex_path: str,
    prompt: str,
    workdir: str,
    *,
    sandbox: str = DEFAULT_SANDBOX,
    disable_subagents: bool = True,
    model: Optional[str] = None,
    extra: Optional[list[str]] = None,
) -> list[str]:
    """Build the `codex exec` argv. Fixed order for reproducibility."""
    argv = [codex_path, "exec", "--json"]
    if disable_subagents:
        argv += ["--disable", SUBAGENT_FEATURE]
    argv += ["--sandbox", sandbox, "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if extra:
        argv += list(extra)
    argv += ["-C", workdir, prompt]
    return argv


def build_env(
    *,
    codex_home: str,
    proxy: Optional[str] = None,
    ca_bundle: Optional[str] = None,
    base_env: Optional[dict] = None,
) -> dict:
    """Environment for the child: this process only, no system changes.

    Codex reads the proxy from the environment; it does NOT read the WinINET
    system setting and it ignores ``-c network.proxy_url`` (verified with
    ``codex doctor``: "session-flags: network is ignored"). Without a readable
    CA bundle an intercepting proxy yields "invalid peer certificate:
    UnknownIssuer".
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["CODEX_HOME"] = codex_home
    if proxy:
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            env[k] = proxy
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
        env.setdefault("no_proxy", "localhost,127.0.0.1")
    if ca_bundle:
        env["CODEX_CA_CERTIFICATE"] = ca_bundle
    return env


def file_identity(path: str) -> dict:
    """Size + sha256, used to pin the executable across runs."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"path": os.path.abspath(path), "size": os.path.getsize(path), "sha256": h.hexdigest()}


# --------------------------------------------------------------------------- #
# event parsing
# --------------------------------------------------------------------------- #


def classify_outcome(run: CodexRun, parsed: ParsedRun) -> tuple[str, bool, str]:
    """Decide the outcome of a finished (or killed) Codex run.

    Returns ``(verdict, terminal, detail)`` where ``verdict`` is one of
    ``FINISHED`` / ``FAILED`` / ``RUNNING`` (the last meaning "unconfirmed").

    ``terminal=False`` is a safety statement, not a detail: it tells the kernel
    NOT to free the quota slot, because we cannot show the work is over. Being
    conservative here is deliberate - freeing early could hand a second call a
    slot the first one still occupies.

    The cases, in order:

    1. the process had to be killed (timeout) -> unconfirmed;
    2. an explicit ``turn.failed`` -> definitive failure;
    3. a turn STARTED but reported neither completion nor failure -> unconfirmed
       (an *abnormal* end: the work began and we never learned its result);
    4. ``turn.completed`` AND a non-zero exit code -> contradictory evidence ->
       unconfirmed (the two reports disagree, so neither can be trusted alone);
    5. ``turn.completed`` AND exit code 0 -> success;
    6. no turn events at all -> failure (nothing ran), reported as abnormal.
    """
    if run.local_stop_requested:
        return RUNNING, False, "local process stop requested; remote/native children not confirmed stopped"
    if run.timed_out:
        return (
            RUNNING,
            False,
            "timed out locally; remote turn not confirmed stopped (cancel unsupported)",
        )

    if parsed.turn_failed:
        reason = "; ".join(parsed.errors) or "turn.failed"
        return FAILED, True, reason

    if parsed.turn_started and not parsed.turn_completed:
        return (
            RUNNING,
            False,
            "abnormal end: turn started but never reported completion or failure; "
            f"exit={run.exit_code}",
        )

    if parsed.turn_completed and run.exit_code != 0:
        return (
            RUNNING,
            False,
            "contradictory evidence: turn.completed reported but the process exited "
            f"with {run.exit_code}",
        )

    if parsed.turn_completed and run.exit_code == 0:
        return FINISHED, True, parsed.final_message

    if not parsed.events:
        return (
            FAILED,
            True,
            f"abnormal end: no events at all; exit={run.exit_code}",
        )

    return FAILED, True, f"no completed turn; exit={run.exit_code}"


def parse_events(text: str) -> ParsedRun:
    """Parse the JSONL stream. Non-JSON lines are kept, not silently dropped."""
    parsed = ParsedRun()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            parsed.unparsed_lines.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.unparsed_lines.append(line)
            continue
        parsed.events.append(event)
        etype = event.get("type", "")
        parsed.event_types[etype] = parsed.event_types.get(etype, 0) + 1
        if etype == EVENT_THREAD_STARTED and event.get("thread_id"):
            parsed.thread_id = event["thread_id"]
        elif etype == EVENT_ITEM_COMPLETED:
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                parsed.final_message = item["text"]
        elif etype == EVENT_TURN_COMPLETED:
            parsed.usage = dict(event.get("usage") or {})
        elif etype in (EVENT_ERROR, EVENT_TURN_FAILED):
            msg = event.get("message") or (event.get("error") or {}).get("message")
            if msg:
                parsed.errors.append(str(msg))
    return parsed


# --------------------------------------------------------------------------- #
# runners
# --------------------------------------------------------------------------- #


class SubprocessCodexRunner:
    """Runs the real CLI. stdout goes to a FILE (pipes are denied here)."""

    def __init__(self, scratch_dir: str) -> None:
        self.scratch_dir = scratch_dir
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._stop_requests: set[str] = set()
        os.makedirs(scratch_dir, exist_ok=True)

    def run(self, argv: list[str], env: dict, timeout: float) -> CodexRun:
        return self._run(uuid.uuid4().hex, argv, env, timeout)

    def run_for_invocation(self, invocation_id: str, argv: list[str], env: dict, timeout: float) -> CodexRun:
        # Keep the bundled stub's command validation when called by the adapter.
        if isinstance(self, StubCodexRunner) and (
            len(argv) < 3 or not is_bundled_stub_command(argv[1], argv[:1]) or argv[2] != "exec"
        ):
            raise CodexLaunchFailed("offline runner refuses any command except the bundled stub")
        return self._run(invocation_id, argv, env, timeout)

    def stop_local(self, invocation_id: str) -> dict:
        """Only this runner's process handle. Never find/kill processes by name or PID input."""
        with self._lock:
            self._stop_requests.add(invocation_id)
            proc = self._processes.get(invocation_id)
            if proc is None:
                return {"local_stop_requested": True, "local_process_exited": False,
                        "remote_stop_confirmed": False, "reason": "no_owned_process_or_launch_pending"}
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return {"local_stop_requested": True, "local_process_exited": proc.poll() is not None,
                "remote_stop_confirmed": False, "reason": "local_entry_process_only"}

    def _run(self, invocation_id: str, argv: list[str], env: dict, timeout: float) -> CodexRun:
        out_path = os.path.join(self.scratch_dir, f"codex-{uuid.uuid4().hex}.jsonl")
        start = time.time()
        timed_out = False
        exit_code: Optional[int] = None
        with open(out_path, "w", encoding="utf-8", errors="replace") as fh:
            try:
                with self._lock:
                    if invocation_id in self._stop_requests:
                        self._stop_requests.discard(invocation_id)
                        return CodexRun(None, False, 0, "", argv, local_stop_requested=True)
                    proc = subprocess.Popen(
                        argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        env=env, cwd=self.scratch_dir,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    self._processes[invocation_id] = proc
            except OSError as exc:
                # The OS refused to create the process: NOTHING is running, so
                # this error is definitively terminal. (Seen in practice as
                # "Access is denied" under a restricted sandbox.)
                raise CodexLaunchFailed(f"could not launch {argv[0]!r}: {exc}") from exc
            try:
                exit_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass  # a concurrent user stop may have already ended it
                try:
                    exit_code = proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._lock:
                    local_stop_requested = invocation_id in self._stop_requests
                    self._stop_requests.discard(invocation_id)
                    if proc.poll() is not None:
                        self._processes.pop(invocation_id, None)

        elapsed = time.time() - start
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            text = ""
        return CodexRun(
            exit_code=exit_code,
            timed_out=timed_out,
            elapsed=elapsed,
            stdout_text=text,
            argv=argv,
            local_stop_requested=local_stop_requested,
        )


class StubCodexRunner(SubprocessCodexRunner):
    """Offline process runner restricted to the bundled stub, checked again at launch."""

    def run(self, argv: list[str], env: dict, timeout: float) -> CodexRun:
        if len(argv) < 3 or not is_bundled_stub_command(argv[1], argv[:1]) or argv[2] != "exec":
            raise CodexLaunchFailed("offline runner refuses any command except the bundled stub")
        return super().run(argv, env, timeout)


class FakeCodexRunner:
    """Simulated Codex process for offline integration tests.

    Emits the same JSONL shape as a real run, without launching anything.
    ``script`` lets a test choose the outcome (success, turn.failed, timeout,
    empty output, malformed lines, ...).
    """

    def __init__(self, script: Callable[[list[str], dict], CodexRun]) -> None:
        self.script = script
        self.calls: list[dict] = []

    def run(self, argv: list[str], env: dict, timeout: float) -> CodexRun:
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout})
        return self.script(argv, env)


def success_script(
    thread_id: str = "01a13a34-63e6-7cc2-85d1-ddfbbec0e2ce",
    message: str = "OK",
    usage: Optional[dict] = None,
) -> Callable[[list[str], dict], CodexRun]:
    """A run that looks exactly like the verified successful probe."""

    def _script(argv: list[str], env: dict) -> CodexRun:
        lines = [
            json.dumps({"type": EVENT_THREAD_STARTED, "thread_id": thread_id}),
            json.dumps({"type": EVENT_TURN_STARTED}),
            json.dumps(
                {
                    "type": EVENT_ITEM_COMPLETED,
                    "item": {"id": "item_0", "type": "agent_message", "text": message},
                }
            ),
            json.dumps(
                {
                    "type": EVENT_TURN_COMPLETED,
                    "usage": usage
                    or {
                        "input_tokens": 14481,
                        "cached_input_tokens": 12544,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 0,
                    },
                }
            ),
        ]
        return CodexRun(0, False, 0.05, "\n".join(lines) + "\n", argv)

    return _script


# --------------------------------------------------------------------------- #
# the adapter
# --------------------------------------------------------------------------- #


class CodexAdapter(AdapterContract):
    """Minimal real adapter. Gated: cannot start without a kernel gate token."""

    name = "codex"

    #: Agent ids this adapter is allowed to serve. An invocation created for a
    #: different agent must NEVER be run by it - otherwise a call registered as
    #: e.g. "mock" could quietly launch a real Codex process.
    AGENT_IDS = frozenset({"codex"})

    def __init__(
        self,
        *,
        codex_path: Optional[str] = None,
        codex_home: str,
        workdir_root: str,
        store: Optional[ExecutionStore] = None,
        runner: Optional[Any] = None,
        proxy: Optional[str] = None,
        ca_bundle: Optional[str] = None,
        timeout_seconds: float = 300.0,
        sandbox: str = DEFAULT_SANDBOX,
        disable_subagents: bool = True,
        model: Optional[str] = None,
        expected_identity: Optional[dict] = None,
        version: str = "unknown",
        command_prefix: Optional[list[str]] = None,
    ) -> None:
        self.codex_path = codex_path
        self.command_prefix = list(command_prefix or [])
        self.codex_home = codex_home
        self.workdir_root = workdir_root
        self.proxy = proxy
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds
        self.sandbox = sandbox
        self.disable_subagents = disable_subagents
        self.model = model
        self.version = version
        self.expected_identity = expected_identity
        self.store = store or ExecutionStore(path=":memory:", table="codex_executions")
        os.makedirs(workdir_root, exist_ok=True)
        self.runner = runner or SubprocessCodexRunner(scratch_dir=os.path.join(workdir_root, "_scratch"))
        self._start_count = 0

    # -- introspection ------------------------------------------------------

    @property
    def start_count(self) -> int:
        """How many times start() was actually entered (test assertion surface)."""
        return self._start_count

    def capabilities(self) -> dict[str, Any]:
        """Mirrors docs/agent-codex-capabilities.md.

        Anything not demonstrated stays ``unknown`` rather than True.
        """
        return {
            "post_message": True,          # verified: exit 0 + agent_message "OK"
            "stream": True,                # verified: JSONL event stream
            "resume_session": True,        # interface exists + thread_id captured;
                                           # resuming BY it is not yet verified
            "cancel": False,               # UNVERIFIED - reported as unsupported
            "stop_local_process": isinstance(getattr(self, "runner", None), SubprocessCodexRunner),
            "limit_dir": "unknown",        # read-only run left the workdir empty,
                                           # but the prompt never asked it to write
            "intercept_tool": False,       # UNVERIFIED
            "control_subagent": False,     # native spawn observed despite disable flag
            "real_usage": True,            # verified: turn.completed.usage tokens
            "verified": {
                "post_message": True,
                "stream": True,
                "real_usage": True,
                "cancel": False,
                "intercept_tool": False,
                "control_subagent": False,
            },
        }

    def execution_admission(self) -> ExecutionAdmission:
        # These are trusted code-level fixtures, never a user-provided flag.
        # Replacing a fixture with a real runner immediately revokes admission.
        if type(self.runner) is FakeCodexRunner or (
            type(self.runner) is StubCodexRunner
            and is_bundled_stub_command(self.codex_path, self.command_prefix)
        ):
            return ExecutionAdmission(True, "simulation", "offline_fixture", "离线桩验证，不调用真实 Codex。")
        return blocked_codex_admission()

    def supervised_execution_admission(self) -> ExecutionAdmission:
        """User-selected bounded operation, not a claim that native children are controlled."""
        if (type(self.runner) is not SubprocessCodexRunner or self.command_prefix
                or self.sandbox != "read-only" or not self.disable_subagents
                or not self.expected_identity or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 600):
            return ExecutionAdmission(reason="用户监督模式要求固定身份、只读、自有进程和最多 600 秒的本地时限。")
        return ExecutionAdmission(True, "supervised", "user_supervised_boundary",
            "仅保证 Hub 审批与本地入口进程控制；原生派生不在 Hub 配额内，远端停止不保证。")

    def stop_local_invocation(self, invocation_id: str) -> dict:
        if not isinstance(self.runner, SubprocessCodexRunner):
            return {"local_stop_requested": False, "local_process_exited": False,
                    "remote_stop_confirmed": False, "reason": "unsupported"}
        return self.runner.stop_local(invocation_id)

    def health(self) -> bool:
        return bool(self.codex_path and os.path.isfile(self.codex_path))

    def identity_matches(self) -> tuple[bool, str]:
        """Confirm the pinned executable is still the one that was vetted."""
        if not self.codex_path or not os.path.isfile(self.codex_path):
            return False, "executable missing"
        if not self.expected_identity:
            return True, "no expected identity recorded (not pinned)"
        actual = file_identity(self.codex_path)
        for k in ("size", "sha256"):
            if self.expected_identity.get(k) != actual.get(k):
                return False, f"{k} mismatch: {self.expected_identity.get(k)} != {actual.get(k)}"
        return True, "identity matches"

    # -- gated start --------------------------------------------------------

    def start(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str] = None,
    ) -> AdapterStartResult:
        """Launch Codex for one invocation.

        ``gate_token`` is issued by the kernel's consume gate. It is required so
        that reaching this code without an approved, quota-reserved invocation is
        a loud error rather than a silent launch.
        """
        if not gate_token:
            # Refuse loudly: no approval was demonstrated.
            return AdapterStartResult(
                adapter_invocation_id="",
                ok=False,
                terminal=True,
                detail="refused: no kernel gate token (invocation not approved)",
            )
        admission = execution_admission(self, snapshot.execution_mode)
        if not admission.allowed:
            # Defence in depth for direct callers and a runner changed after
            # the Worker's eligibility check. No home, run directory or process
            # record is created here, and start_count remains zero.
            return AdapterStartResult("", ok=False, terminal=True, detail=admission.reason)
        if snapshot.adapter_version != "unknown":
            # This adapter declares no binding identity, so it serves only
            # snapshots that recorded none; one approved for another adapter
            # (e.g. app-server) is refused. The Worker checks this first (D11).
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: approved for another adapter identity; nothing started")
        # Agent identity must match. The snapshot records which agent the user
        # approved; running a different one would execute something that was
        # never consented to.
        agent_id = (snapshot.agent_id or "").strip()
        if agent_id not in self.AGENT_IDS:
            return AdapterStartResult(
                adapter_invocation_id="",
                ok=False,
                terminal=True,
                detail=(
                    f"refused: agent_id {agent_id!r} is not served by this adapter "
                    f"(expected one of {sorted(self.AGENT_IDS)})"
                ),
            )
        if not self.codex_path:
            # Terminal refusal, NOT an exception: the kernel has already consumed
            # the approval and reserved a slot by the time start() is called, so
            # raising here would leak that slot. Nothing was launched, so the
            # outcome is definitive and the slot may be freed.
            return AdapterStartResult(
                "", ok=False, terminal=True, detail="refused: codex_path is not configured"
            )
        ok, why = self.identity_matches()
        if not ok:
            return AdapterStartResult("", ok=False, detail=f"refused: {why}")

        # Pre-launch preparation. Every failure in this block happens BEFORE a
        # process exists, so it is definitively terminal. CODEX_HOME is created
        # here rather than in config: resolving settings must have no side
        # effects, and a home we cannot create is a real launch blocker.
        try:
            if self.codex_home:
                os.makedirs(self.codex_home, exist_ok=True)
            workdir = os.path.join(self.workdir_root, invocation_id)
            os.makedirs(workdir, exist_ok=True)
        except OSError as exc:
            return AdapterStartResult(
                "", ok=False, terminal=True, detail=f"refused: launch preparation failed ({exc})"
            )

        # Past this line a launch is being attempted, so start_count reflects a
        # real launch attempt rather than a preparation failure.
        self._start_count += 1
        argv = build_argv(
            self.codex_path,
            snapshot.task_input,
            workdir,
            sandbox=self.sandbox,
            disable_subagents=self.disable_subagents,
            model=self.model,
        )
        # `command_prefix` exists so an offline integration can run a STUB process
        # through the exact same machinery (argv, env, file-captured stdout,
        # parsing, store) without invoking Codex. Empty in normal operation.
        if self.command_prefix:
            argv = list(self.command_prefix) + argv
        env = build_env(codex_home=self.codex_home, proxy=self.proxy, ca_bundle=self.ca_bundle)

        # Record BEFORE launching: if we crash mid-run the external-side record
        # still exists, so recovery sees "may be running" instead of "never ran".
        self.store.record_start(invocation_id, adapter_invocation_id="")

        try:
            if isinstance(self.runner, SubprocessCodexRunner):
                run = self.runner.run_for_invocation(invocation_id, argv, env, self.timeout_seconds)
            else:
                run = self.runner.run(argv, env, self.timeout_seconds)
        except CodexLaunchFailed as exc:
            # The OS refused to create the process: nothing is running, so the
            # outcome is definitive and the slot may be freed.
            self.store.set_status(invocation_id, FAILED, str(exc)[:400])
            return AdapterStartResult("", ok=False, terminal=True, detail=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            # Anything else is AMBIGUOUS: the runner may have started a process
            # before failing. terminal=False keeps the quota slot and turns the
            # call into `unknown` for a human, instead of possibly handing a
            # second call a slot a live run still occupies.
            detail = f"adapter start raised {type(exc).__name__}: {exc}"
            self.store.set_status(invocation_id, RUNNING, detail[:400])
            return AdapterStartResult("", ok=False, terminal=False, detail=detail[:200])

        parsed = parse_events(run.stdout_text)

        local_id = parsed.thread_id or f"codex-{invocation_id}"
        # Record the external execution id NOW that it exists: the kernel keeps it
        # in ``invocations.adapter_invocation_id``, and the adapter's own record
        # must agree, otherwise a stop check or a recovery that reads only the
        # adapter's store cannot say which external execution this is.
        self.store.set_adapter_id(invocation_id, local_id)
        payload = {
            "thread_id": parsed.thread_id,
            "final_message": parsed.final_message,
            "usage": parsed.usage,
            "event_types": parsed.event_types,
            "events": parsed.events,
            "errors": parsed.errors,
            "unparsed_lines": parsed.unparsed_lines,
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "elapsed": round(run.elapsed, 2),
            "argv": run.argv,
            "workdir_entries_after": sorted(os.listdir(workdir)) if os.path.isdir(workdir) else [],
        }
        self.store.set_payload(invocation_id, payload)
        verdict, terminal, detail = classify_outcome(run, parsed)
        payload["verdict"] = verdict
        payload["local_stop_requested"] = run.local_stop_requested
        payload["terminal"] = terminal
        payload["detail"] = detail
        self.store.set_payload(invocation_id, payload)

        if verdict == FINISHED:
            self.store.set_status(
                invocation_id,
                FINISHED,
                f"turn.completed; usage={json.dumps(parsed.usage, ensure_ascii=False)}",
            )
            # `codex exec` is synchronous: the process has already exited, so the
            # run is COMPLETE, not merely started. terminal=True tells the Worker
            # to settle the invocation and free its slot now - otherwise a
            # finished sub-agent would hold a concurrent slot forever.
            return AdapterStartResult(
                local_id, ok=True, terminal=True, detail=parsed.final_message[:200]
            )

        if verdict == FAILED:
            reason = "; ".join(parsed.errors) or detail
            self.store.set_status(invocation_id, FAILED, reason[:400])
            return AdapterStartResult(local_id, ok=False, terminal=True, detail=reason[:200])

        # UNCONFIRMED: keep the record non-terminal so the kernel cannot free the
        # slot. This covers a local timeout, a turn that started but reported no
        # outcome, and contradictory evidence (turn completed yet a non-zero exit).
        self.store.set_status(invocation_id, RUNNING, detail[:400])
        return AdapterStartResult(local_id, ok=False, terminal=False, detail=detail[:200])

    # -- cancel: honestly unsupported ---------------------------------------

    def cancel(self, adapter_invocation_id: str) -> Any:
        """Cancellation has never been verified against Codex.

        AGENTS.md: "无法取消就必须如实声明". So this reports ``unsupported``
        rather than pretending to stop anything.
        """
        return {
            "ok": False,
            "reason": "unsupported",
            "detail": "Codex cancellation is unverified; not claiming a stop",
        }

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self.cancel(invocation_id)

    def get_status(self, adapter_invocation_id: str) -> Any:
        rec = self.store.get(adapter_invocation_id)
        return {"ok": rec is not None, "record": rec}

    def lookup_by_invocation(self, invocation_id: str) -> Optional[dict[str, Any]]:
        """Recovery / stop-confirmation hook: the external side's own view."""
        return self.store.get(invocation_id)

    # -- convenience for the console ---------------------------------------

    def last_result(self, invocation_id: str) -> Optional[dict[str, Any]]:
        rec = self.store.get(invocation_id)
        if not rec:
            return None
        payload = rec.get("payload") or {}
        return {
            "status": rec.get("status"),
            "thread_id": payload.get("thread_id", ""),
            "final_message": payload.get("final_message", ""),
            "usage": payload.get("usage", {}),
            "event_types": payload.get("event_types", {}),
            "workdir_entries_after": payload.get("workdir_entries_after", []),
        }

    def usage_report(self, invocation_id: str) -> Optional[dict[str, Any]]:
        """This call's usage for the kernel ledger; ``None`` without a record.

        ``turn.completed.usage`` was only ever observed on single-request,
        new-thread turns; whether it is per turn or per thread is unverified
        (docs/t0-usage-baseline.md), so a record is at best ``partial``.
        """
        rec = self.store.get(invocation_id)
        if not rec:
            return None
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        usage = payload.get("usage") or {}
        notes = ["exec_usage_semantics_unverified"] if usage else ["no_usage_reported"]
        return {"mapping": MAPPING_CODEX_EXEC, "usage": usage, "source": "exec_turn_completed",
                "notes": notes}
