"""Claude Code adapter: non-interactive calls and a scoped strict file profile.

One call = one ``claude -p`` process with a fixed argv, the prompt on stdin and
one JSON result object on stdout (``--output-format json``). The file-hooks-v1
profile was checked on 2026-11-03 against the exact 2.1.308 binary using a
synthetic localhost provider, with no real model or credentials. See
docs/native-strict.md for the tested scope and remaining limitations.

Admission (same structure as the app-server adapter):

* the trusted offline fixtures (:class:`FakeClaudeRunner`, and the bundled stub
  run by :class:`StubClaudeLauncher`) are ``simulation``;
* strict admits only a reviewed binary/profile pair: scoped file tools and
  manually approved new foreground general-purpose children, no shell/MCP;
* supervised mode admits only :class:`ClaudeCodeLauncher` built from the
  reviewed :data:`PINNED_CLAUDE_CODE` constant, the fixed argv, a proxy and a
  readable CA bundle, a config directory and work root outside the repository,
  a local deadline of at most 600 s - and only after the zero-model identity
  check has been recorded in :data:`ZERO_MODEL_IDENTITY_REPORT`. Until then the
  real launcher stays refused.

Rules this adapter enforces:

* the kernel gate token and the snapshot's binding identity (D11) are required;
  the launcher re-hashes the pinned file right before the launch;
* the prompt goes to stdin, never to argv, so user text can never be parsed as
  a flag and does not appear in process listings; ``--disable-slash-commands``
  keeps a leading ``/`` from expanding into a command;
* the child sees no inherited ``CLAUDE*`` / ``ANTHROPIC*`` variables (no
  borrowed credential, endpoint or parent-session marker) and never a variable
  that turns off certificate verification;
* only a parsed result object settles a call. A timeout, a local stop or
  output that cannot be read stays unconfirmed (``terminal=False``: the kernel
  keeps the quota slot and marks the call ``unknown``). ``cancel`` is
  ``unsupported``; a local stop ends only our own entry process;
* prompt and reply text never reach events or logs. The reply is kept only in
  this adapter's execution store (``final_message``), as the other adapters do;
  stdout is deleted after parsing, stderr is kept beside the work directories
  and recorded by size and hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agenthub.contracts import (
    INDEPENDENT,
    AdapterContract,
    AdapterStartResult,
    ExecutionAdmission,
    InvocationSnapshot,
    execution_admission,
)
from agenthub.usage import MAPPING_CLAUDE_CODE, numbers_only
from agenthub_adapters.store import FAILED, FINISHED, RUNNING, ExecutionStore

TRANSPORT = "claude-code-cli"
#: A launch that provably never started a process spent nothing.
NOT_LAUNCHED_USAGE = {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
AGENT_ID = "claude-code"

#: The reviewed Claude Code identity. Static inspection only (2026-10-23):
#: path, size and SHA-256 were read from the file, the version from its
#: FileVersion resource (2.1.308.0); the binary has NOT been executed. It is
#: the desktop-bundled CLI under the MSIX package cache, so a desktop update
#: may remove it - the identity check then fails closed. Never read from the
#: environment; a change here is a code review decision and changes every new
#: call's binding identity (D11).
PINNED_CLAUDE_CODE = {
    "path": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_pzs8sxrjxfjjc",
                         "LocalCache", "Roaming", "Claude", "claude-code", "2.1.308",
                         "c17a819afb40", "claude.exe"),
    "sha256": "c9796ba22a9221586b36371b36f5b66198d66eb4badf6a4cf4a5f7b07becea53",
    "size": 297376928,
    "version": "2.1.308",
}
#: SHA-256 of the reviewed zero-model identity report (real-test Step A:
#: ``--version`` / ``--help`` only). ``None`` = not run, and supervised
#: admission refuses the real launcher.
#: Recorded 2026-10-24 by the user's decision (a): the run of 2026-10-23 was
#: INCONCLUSIVE only because ``--max-turns`` is not listed in ``--help``
#: (version 2.1.308, both commands exit 0, hash unchanged; evidence tag
#: evidence/cc-identity-real-run-1-inconclusive). The flag is kept.
ZERO_MODEL_IDENTITY_REPORT: Optional[str] = "b57a1d5f0d2a0bd1b7c35031c3fdafb74e2a763d82a12a1f40844b8b8cbd5089"

#: The fixed argv after the executable. The prompt is sent on stdin. Every flag
#: is documented; none has been observed on this machine.
CLAUDE_ARGV_TAIL = (
    "-p", "--output-format", "json",
    "--safe-mode",               # no CLAUDE.md, skills, plugins, hooks, MCP, auto memory
    "--tools", "",               # no built-in tools (Bash, edits, Agent): a request, not proof
    "--strict-mcp-config",       # no MCP servers (none passed)
    "--disable-slash-commands",  # a leading "/" in the prompt is not a command
    "--permission-mode", "dontAsk",
    "--max-turns", "1",
    # No implicit CLI dollar cap. Hub budgets and approvals remain separate.
    "--no-session-persistence",  # no transcript on disk; no resume
)

STRICT_PROFILE = "file-hooks-v1"
SPEND_PROFILE = "usd-cap-off-v1"
# Populated only by acceptance against the exact binary, never from a profile
# file or model output. Loopback synthetic-model evidence is labelled as such.
STRICT_VERIFIED_SHA256: frozenset[str] = frozenset({
    "c9796ba22a9221586b36371b36f5b66198d66eb4badf6a4cf4a5f7b07becea53",
    # 2.1.317: all 13 real-CLI/localhost-model scenarios rechecked 2026-11-04.
    "26b01ec22df2c17ac7f5703aa547d36706e8d58412136ced0344a8ced4d86038",
})

#: What the context preview may say about this adapter (T-1d): the prompt is
#: ``task_input`` on stdin, verbatim. ``observed`` are real runs under this pin
#: (docs/t0-usage-baseline.md §4.2, §4.3): total input including cache, with
#: that run's own short input; historical, never this call's estimate.
CONTEXT_PROFILE = {
    "adds_prompt_text": False,
    "observed": [
        {"evidence": "evidence/cc-turn-real-run-1-pass", "binding": "2.1.308",
         "input_total": 1752, "cache_read": 0},
        {"evidence": "evidence/two-agent-real-run-2-pass", "binding": "2.1.308",
         "input_total": 1798, "cache_read": 1161},
    ],
}

#: Inherited variables the child never sees (prefix match, case-insensitive).
STRIPPED_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC")
#: Inherited variables the child never sees (exact names, case-insensitive).
STRIPPED_ENV_NAMES = frozenset({
    "NODE_TLS_REJECT_UNAUTHORIZED", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
})

REAL_BLOCK_REASON = (
    "当前 Claude Code 通道对内部子 Agent 和工具操作的严格控制仍未验证，严格模式暂不可用。"
    "请选用严格模式可用的通道，或明确选择用户监督模式。"
)
SUPERVISED_REASON = (
    "仅保证 Hub 审批、固定身份与固定参数核验、本次入口进程控制。"
    "工具关闭、原生子 Agent 与远端停止均未验证，不在 Hub 配额内；停止只结束本次入口进程。"
)

#: How much of an error text the record keeps (CLI messages, not replies).
ERROR_TEXT_LIMIT = 300

_FIELD = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


class ClaudeLaunchFailed(RuntimeError):
    """The process was provably not created (refused before or by the OS)."""


@dataclass
class ClaudeRun:
    """Raw outcome of one ``claude -p`` process. ``stdout_text`` is transient."""

    exit_code: Optional[int]
    timed_out: bool
    elapsed: float
    stdout_text: str
    argv: list[str]
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stderr_sha256: str = ""
    input_written: bool = False
    local_stop_requested: bool = False


@dataclass
class ParsedResult:
    """The result object, or why there is none."""

    result: Optional[dict] = None
    empty: bool = False
    malformed: bool = False
    usage: dict = field(default_factory=dict)
    #: The whole ``usage`` and ``modelUsage`` objects, numbers only (D-T0-5),
    #: so the ledger can be re-normalized later; no text is kept.
    usage_raw: dict = field(default_factory=dict)
    model_usage: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #


def build_env(*, config_dir: str, proxy: Optional[str] = None, ca_bundle: Optional[str] = None,
              base_env: Optional[dict] = None) -> tuple[dict, list[str], list[str]]:
    """``(env, removed, set)``: the child environment plus variable NAMES (never values).

    The config directory is set explicitly, so the child uses only the login
    stored there; nothing inherited can redirect credentials, the endpoint or
    certificate checks.
    """
    source = dict(base_env if base_env is not None else os.environ)
    env, stripped = {}, []
    for name, value in source.items():
        upper = name.upper()
        if upper.startswith((*STRIPPED_ENV_PREFIXES, "AGENT_HUB_")) or upper in STRIPPED_ENV_NAMES:
            stripped.append(name)
            continue
        env[name] = value
    added = {"CLAUDE_CONFIG_DIR": config_dir, "DISABLE_AUTOUPDATER": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if proxy:
        added.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, NO_PROXY="localhost,127.0.0.1")
    if ca_bundle:
        # Adds to the bundled and system stores (documented); never replaces them.
        added["NODE_EXTRA_CA_CERTS"] = ca_bundle
    env.update(added)
    return env, sorted(stripped), sorted(added)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _inside(path: str, root: str) -> bool:
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    r = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


#: agenthub_adapters/claude_code.py -> agenthub_adapters -> adapters -> packages -> repo
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# --------------------------------------------------------------------------- #
# launchers (what is started) and runners (how it is started)
# --------------------------------------------------------------------------- #


class ClaudeCodeLauncher:
    """The real pinned CLI (supervised mode only). Build it with :meth:`pinned`."""

    def __init__(self, *, path: str, expected_identity: Optional[dict], version: str = "unknown",
                 config_dir: str = "", proxy: Optional[str] = None,
                 ca_bundle: Optional[str] = None, adopted_identity: Optional[dict] = None) -> None:
        self.path = path
        self.expected_identity = expected_identity
        self.version = version
        self.config_dir = config_dir
        self.proxy = proxy
        self.ca_bundle = ca_bundle
        self._adopted_identity = dict(adopted_identity) if adopted_identity else None
        self.argv = [path, *CLAUDE_ARGV_TAIL]

    @classmethod
    def pinned(cls, *, config_dir: str, proxy: Optional[str] = None,
               ca_bundle: Optional[str] = None) -> "ClaudeCodeLauncher":
        return cls(path=PINNED_CLAUDE_CODE["path"],
                   expected_identity={"sha256": PINNED_CLAUDE_CODE["sha256"],
                                      "size": PINNED_CLAUDE_CODE["size"]},
                   version=PINNED_CLAUDE_CODE["version"], config_dir=config_dir,
                   proxy=proxy, ca_bundle=ca_bundle)

    def binding_identity(self) -> str:
        ident = self.expected_identity or {}
        return f"claude-code@{self.version};sha256={ident.get('sha256', '')};size={ident.get('size', '')}"

    def identity_matches(self) -> tuple[bool, str]:
        try:
            info = os.stat(self.path) if self.path else None
        except FileNotFoundError:
            return False, "executable missing"
        except OSError:
            return False, "executable unreadable"
        if info is None or not stat.S_ISREG(info.st_mode):
            return False, "executable missing"
        if not self.expected_identity:
            return False, "no expected identity recorded (not pinned)"
        if info.st_size != self.expected_identity.get("size"):
            return False, "size mismatch"
        try:
            actual = _sha256_file(self.path)
        except FileNotFoundError:
            return False, "executable missing"
        except OSError:
            return False, "executable unreadable"
        if actual != self.expected_identity.get("sha256"):
            return False, "sha256 mismatch"
        return True, "identity matches"

    def supervised_problems(self) -> list[str]:
        """Cheap checks (they run under the kernel's writer lock); the hash is at launch."""
        problems = []
        pinned = self._adopted_identity or PINNED_CLAUDE_CODE
        if (os.path.normcase(self.path or "") != os.path.normcase(pinned["path"])
                or (self.expected_identity or {}) != {"sha256": pinned["sha256"], "size": pinned["size"]}
                or self.version != pinned["version"]):
            problems.append("identity_not_pinned_constant")
        if not ZERO_MODEL_IDENTITY_REPORT and not self._adopted_identity:
            problems.append("identity_unconfirmed")  # real-test Step A not recorded
        try:
            info = os.stat(self.path) if self.path else None
            if info is None or not stat.S_ISREG(info.st_mode):
                problems.append("executable_missing")
            elif info.st_size != (self.expected_identity or {}).get("size"):
                problems.append("size_mismatch")
        except FileNotFoundError:
            problems.append("executable_missing")
        except OSError:
            problems.append("executable_unreadable")
        if self.argv != [self.path, *CLAUDE_ARGV_TAIL]:
            problems.append("argv_changed")
        if not self.config_dir or not os.path.isabs(self.config_dir) or _inside(self.config_dir, REPO_ROOT):
            problems.append("config_dir_not_outside_repository")
        if not self.proxy and not self._adopted_identity:
            problems.append("proxy_missing")
        if ((self.ca_bundle or not self._adopted_identity) and not
                (self.ca_bundle and os.path.isfile(self.ca_bundle) and os.access(self.ca_bundle, os.R_OK))):
            problems.append("ca_unreadable")
        return problems

    def launch_problems(self) -> list[str]:
        """Everything again, plus the full hash, right before the process starts."""
        problems = self.supervised_problems()
        if not problems:
            ok, why = self.identity_matches()
            if not ok:
                problems.append(why.replace(" ", "_"))
        return problems

    def environment(self) -> tuple[dict, list[str], list[str]]:
        return build_env(config_dir=self.config_dir, proxy=self.proxy, ca_bundle=self.ca_bundle)


def bundled_stub_path() -> str:
    return os.path.realpath(os.path.join(REPO_ROOT, "tools", "demo", "stub_claude_code.py"))


#: Scenarios of tools/demo/stub_claude_code.py (hand-written from the docs, not captured).
STUB_SCENARIOS = ("success-usage", "success-no-usage", "error-result", "max-turns", "malformed",
                  "hang", "nonzero-exit", "success-nonzero", "autopilot")


class StubClaudeLauncher:
    """The bundled offline stub, run by this Python interpreter (simulation)."""

    def __init__(self, scenario: str, *, config_dir: str = "", marker: str = "") -> None:
        self.scenario = scenario
        self.config_dir = config_dir
        self.marker = marker
        self.version = "claude-code-stub"
        self.argv = [sys.executable, bundled_stub_path(), "--scenario", scenario, *CLAUDE_ARGV_TAIL]

    def binding_identity(self) -> str:
        return "claude-code-stub:stdio"

    def identity_matches(self) -> tuple[bool, str]:
        return (os.path.isfile(bundled_stub_path()), "bundled offline stub")

    def trusted(self) -> bool:
        argv = self.argv
        return (
            len(argv) == 4 + len(CLAUDE_ARGV_TAIL) and argv[2] == "--scenario"
            and tuple(argv[4:]) == CLAUDE_ARGV_TAIL
            and os.path.normcase(os.path.realpath(argv[0])) == os.path.normcase(os.path.realpath(sys.executable))
            and os.path.normcase(os.path.realpath(argv[1])) == os.path.normcase(bundled_stub_path())
        )

    def launch_problems(self) -> list[str]:
        return [] if self.trusted() else ["untrusted_stub_command"]

    def environment(self) -> tuple[dict, list[str], list[str]]:
        env, stripped, added = build_env(
            config_dir=self.config_dir or os.path.join(REPO_ROOT, "var", "stub-claude-config"))
        if self.marker:
            env["STUB_CLAUDE_MARKER"] = self.marker  # where the stub records argv/env NAMES/stdin hash
        return env, stripped, added


class ProcessRunner:
    """Runs one process per call; owns only the handles it created.

    stdin is a pipe written by a helper thread (a child that never reads it
    cannot block the deadline); stdout goes to a file (no pipe a descendant
    could hold open), read once and deleted; stderr is kept for diagnostics.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._stop_requests: set[str] = set()

    def run(self, invocation_id: str, argv: list[str], env: dict, cwd: str, prompt: str,
            timeout: float, stdout_path: str, stderr_path: str) -> ClaudeRun:
        data = prompt.encode("utf-8")
        start = time.monotonic()
        timed_out = False
        exit_code: Optional[int] = None
        written = {"ok": False}
        with self._lock:
            if invocation_id in self._stop_requests:
                self._stop_requests.discard(invocation_id)
                raise ClaudeLaunchFailed("local stop requested before launch; nothing started")
        with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
            with self._lock:
                try:
                    proc = subprocess.Popen(
                        argv, stdin=subprocess.PIPE, stdout=out_fh, stderr=err_fh, env=env, cwd=cwd,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError as exc:
                    out_fh.close()
                    _remove_quietly(stdout_path)
                    raise ClaudeLaunchFailed(f"could not launch: {type(exc).__name__}") from exc
                self._processes[invocation_id] = proc

            def _feed() -> None:
                try:
                    proc.stdin.write(data)
                    written["ok"] = True
                except OSError:
                    pass  # the child exited or closed stdin first
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            feeder = threading.Thread(target=_feed, name="claude-code-stdin", daemon=True)
            feeder.start()
            try:
                exit_code = proc.wait(timeout=timeout or None)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    proc.kill()
                except OSError:
                    pass  # a concurrent local stop may have ended it
                try:
                    exit_code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    exit_code = None
            finally:
                feeder.join(timeout=2)
                with self._lock:
                    local_stop = invocation_id in self._stop_requests
                    self._stop_requests.discard(invocation_id)
                    if proc.poll() is not None:
                        self._processes.pop(invocation_id, None)
        elapsed = time.monotonic() - start
        try:
            with open(stdout_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
        _remove_quietly(stdout_path)  # it holds the reply; the store keeps the parsed copy
        try:
            stderr_bytes = os.path.getsize(stderr_path)
            stderr_sha256 = _sha256_file(stderr_path)
        except OSError:
            stderr_bytes, stderr_sha256 = 0, ""
        return ClaudeRun(exit_code=exit_code, timed_out=timed_out, elapsed=elapsed,
                         stdout_text=raw.decode("utf-8", errors="replace"), argv=list(argv),
                         stdout_bytes=len(raw), stderr_bytes=stderr_bytes, stderr_sha256=stderr_sha256,
                         input_written=written["ok"], local_stop_requested=local_stop)

    def stop_local(self, invocation_id: str) -> dict:
        """Only this runner's own handle; never by name or PID."""
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


class FakeClaudeRunner:
    """In-process simulation: no process. ``script(argv, env, prompt)`` returns a ClaudeRun."""

    def __init__(self, script: Callable[[list[str], dict, str], ClaudeRun]) -> None:
        self.script = script
        self.calls: list[dict] = []

    def run(self, invocation_id: str, argv: list[str], env: dict, cwd: str, prompt: str,
            timeout: float, stdout_path: str, stderr_path: str) -> ClaudeRun:
        self.calls.append({"invocation_id": invocation_id, "argv": list(argv),
                           "env_keys": sorted(env), "cwd": cwd, "timeout": timeout,
                           "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()})
        return self.script(list(argv), dict(env), prompt)


# --------------------------------------------------------------------------- #
# documented result shape (hand-written fixtures; not captured)
# --------------------------------------------------------------------------- #

#: Usage fields kept from the result (numbers only; documented names).
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def result_object(*, reply: Optional[str] = "OK", subtype: str = "success", is_error: bool = False,
                  usage: Optional[dict] = None, with_usage: bool = True,
                  session_id: str = "5f0c4d3e-1a2b-4c5d-8e9f-000000000001") -> dict:
    """The documented ``--output-format json`` result (hand-written, not captured)."""
    obj: dict[str, Any] = {"type": "result", "subtype": subtype, "is_error": is_error,
                           "duration_ms": 1200, "duration_api_ms": 1100, "num_turns": 1,
                           "session_id": session_id, "permission_denials": [],
                           "uuid": "00000000-0000-4000-8000-000000000002"}
    if reply is not None:
        obj["result"] = reply
    if subtype != "success":
        obj["errors"] = [f"{subtype}"]
    if with_usage:
        obj["total_cost_usd"] = 0.0123
        obj["usage"] = usage or {"input_tokens": 12, "cache_creation_input_tokens": 0,
                                 "cache_read_input_tokens": 0, "output_tokens": 3,
                                 "server_tool_use": {"web_search_requests": 0},
                                 "service_tier": "standard"}
        obj["modelUsage"] = {"claude-model": {"inputTokens": 12, "outputTokens": 3}}
    return obj


def scripted(obj: Optional[dict] = None, *, exit_code: Optional[int] = 0, text: Optional[str] = None,
             timed_out: bool = False) -> Callable[[list[str], dict, str], ClaudeRun]:
    """A FakeClaudeRunner script returning one documented-shape outcome."""

    def _script(argv: list[str], env: dict, prompt: str) -> ClaudeRun:
        out = text if text is not None else (json.dumps(obj) if obj is not None else "")
        return ClaudeRun(exit_code=exit_code, timed_out=timed_out, elapsed=0.05, stdout_text=out,
                         argv=argv, stdout_bytes=len(out.encode("utf-8")), input_written=True)

    return _script


def parse_result(text: str) -> ParsedResult:
    """Exactly one JSON result object, or nothing usable. No guessing from partial output."""
    stripped = text.lstrip("﻿").strip()
    if not stripped:
        return ParsedResult(empty=True)
    try:
        obj = json.loads(stripped)
    except ValueError:
        return ParsedResult(malformed=True)
    if (not isinstance(obj, dict) or obj.get("type") != "result"
            or not isinstance(obj.get("subtype"), str) or type(obj.get("is_error")) is not bool):
        return ParsedResult(malformed=True)
    if obj["subtype"] == "success" and not obj["is_error"] and not isinstance(obj.get("result"), str):
        return ParsedResult(malformed=True)  # a success must carry its reply text
    raw_usage = obj.get("usage")
    usage = {}
    if isinstance(raw_usage, dict):
        usage = {k: raw_usage[k] for k in USAGE_FIELDS
                 if isinstance(raw_usage.get(k), (int, float)) and not isinstance(raw_usage.get(k), bool)}
    return ParsedResult(result=obj, usage=usage, usage_raw=numbers_only(raw_usage),
                        model_usage=numbers_only(obj.get("modelUsage")))


def result_meta(obj: Optional[dict]) -> dict:
    """The rest of the result, for diagnosis: numbers, flags, UUIDs and field names.

    No text: every other string, and every list, is dropped. ``usage`` and
    ``modelUsage`` are kept on their own.
    """
    fields = {k: v for k, v in (obj or {}).items() if isinstance(k, str) and _FIELD.match(k)}
    return {
        "numbers": {k: v for k, v in numbers_only(fields).items() if k not in ("usage", "modelUsage")},
        "flags": {k: v for k, v in fields.items() if isinstance(v, bool)},
        "ids": {k: v for k, v in fields.items() if isinstance(v, str) and _UUID.match(v)},
        "fields": sorted(fields),
    }


def classify(run: ClaudeRun, parsed: ParsedResult) -> tuple[str, bool, str]:
    """``(verdict, terminal, detail)``; ``terminal=False`` keeps the quota slot.

    1. local stop or local timeout -> unconfirmed (cancel unsupported);
    2. no output and a non-zero exit -> failed (the process ended, nothing reported);
    3. no output and exit 0, or output that is not one result object -> unconfirmed;
    4. an error result -> failed (the CLI itself reported the end of the run);
    5. a success result with a non-zero exit -> contradictory -> unconfirmed;
    6. a success result and exit 0 -> finished.
    """
    if run.local_stop_requested:
        return RUNNING, False, "local process stop requested; remote request not confirmed stopped"
    if run.timed_out:
        return (RUNNING, False,
                "timed out locally; entry process killed; remote request not confirmed stopped (cancel unsupported)")
    if parsed.result is None:
        if parsed.empty and run.exit_code not in (0, None):
            return FAILED, True, f"process exited {run.exit_code} without a result object"
        if parsed.empty:
            return RUNNING, False, f"no result object (exit={run.exit_code}); outcome unconfirmed"
        return RUNNING, False, f"output is not one result object (exit={run.exit_code}); outcome unconfirmed"
    result = parsed.result
    if result["is_error"] or result["subtype"] != "success":
        return FAILED, True, f"result reported an error: subtype={result['subtype']}; exit={run.exit_code}"
    if run.exit_code != 0:
        return (RUNNING, False,
                f"contradictory evidence: success result but the process exited with {run.exit_code}")
    return FINISHED, True, "result: success"


# --------------------------------------------------------------------------- #
# the adapter
# --------------------------------------------------------------------------- #


class ClaudeCodeAdapter(AdapterContract):
    """Minimal Claude Code adapter. Gated: gate token, binding identity, admission."""
    supports_model_selection = True
    supports_effort_selection = True

    #: What the pinned CLI (2.1.308) validates ``--effort`` against, read statically from
    #: its binary on 2026-10-30. Per-model subsets exist inside the CLI, which silently
    #: lowers a level the chosen model does not support; there is no RPC to ask which.
    CLI_EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def model_catalog(self, *, refresh=False):
        # This CLI exposes a selection flag, not an account model-list RPC.
        # Aliases are labelled as such; users may enter a provider-specific ID.
        return {"models": [{"id": x, "display_name": x, "source": "cli_alias",
                            "reasoning_efforts": list(self.CLI_EFFORTS)} for x in ('opus', 'sonnet', 'haiku')],
                "default_model": None, "source": "cli_alias", "refresh_available": False,
                "message": "CLI 别名候选，不保证账号可用；默认模型待执行结果确认，也可输入完整模型 ID。"
                           "推理强度 xhigh（超高）、max（最高）由 CLI 接受，所选模型不支持时 CLI 会自动降到可用档位"}


    name = "claude-code"
    transport = TRANSPORT
    AGENT_IDS = frozenset({AGENT_ID})
    #: Requested via ``--tools ""`` (no Agent tool); not proof, not control.
    disable_subagents = True
    sandbox = ""

    @property
    def proxy(self) -> Optional[str]:
        return getattr(self.launcher, "proxy", None)

    def __init__(self, *, launcher: Any, workdir_root: str, store: Optional[ExecutionStore] = None,
                 runner: Optional[Any] = None, timeout_seconds: float = 180.0) -> None:
        self.launcher = launcher
        self.workdir_root = workdir_root
        self.store = store or ExecutionStore(path=":memory:", table="claude_code_executions")
        self.runner = runner if runner is not None else ProcessRunner()
        self.timeout_seconds = timeout_seconds
        self.version = getattr(launcher, "version", "unknown")
        self._start_count = 0
        os.makedirs(workdir_root, exist_ok=True)

    # -- introspection ------------------------------------------------------

    @property
    def start_count(self) -> int:
        """Launch attempts that reached the runner (test assertion surface)."""
        return self._start_count

    def binding_identity(self) -> str:
        bind = getattr(self.launcher, "binding_identity", None)
        if not callable(bind):
            return "unavailable"
        try:
            value = bind()
        except Exception:  # noqa: BLE001
            return "unavailable"
        if not isinstance(value, str) or not value or value == "unknown":
            return "unavailable"
        return value if self._simulated() else value + ";strict-profile=" + STRICT_PROFILE + ";spend-profile=" + SPEND_PROFILE

    def capabilities(self) -> dict[str, Any]:
        """Scoped runtime evidence; no claim about untested binaries or tools."""
        strict_verified = not self._strict_problems()
        return {
            "post_message": True,      # one prompt via stdin; not run against Claude Code
            "final_reply": True,       # result.result of the JSON object
            "stream": False,           # not implemented (json, not stream-json)
            "resume_session": False,   # not implemented (--no-session-persistence)
            "cancel": False,           # unsupported
            "stop_local_process": type(self.runner) is ProcessRunner,
            "limit_dir": "unknown",
            "intercept_tool": strict_verified,
            "control_subagent": strict_verified,
            "tools_disabled_requested": not strict_verified,
            "supervised_tools_disabled_requested": True,
            "real_usage": "unknown",   # recorded when the result carries it; unverified
            "verified": {"post_message": False, "final_reply": False, "real_usage": False,
                         "stream": False, "resume_session": False, "cancel": False,
                         "intercept_tool": strict_verified, "control_subagent": strict_verified},
            "transport": TRANSPORT,
            "strict_profile": STRICT_PROFILE,
            "strict_tools": ["Read", "Grep", "Glob", "Write", "Edit", "Agent"],
            "strict_shell": False,
            "strict_profile_verified": strict_verified,
            "strict_native_scope": "new foreground general-purpose children; one native layer; manual launch approval; no token budget",
            "native_subagents_default": "disabled",
        }

    def _simulated(self) -> bool:
        if type(self.runner) is FakeClaudeRunner:
            return True
        return (type(self.runner) is ProcessRunner and type(self.launcher) is StubClaudeLauncher
                and self.launcher.trusted())

    def execution_admission(self) -> ExecutionAdmission:
        if self._simulated():
            return ExecutionAdmission(True, "simulation", "offline_fixture",
                                      "离线 Claude Code 桩验证，不调用真实 Claude Code。")
        if self._strict_problems():
            return ExecutionAdmission(False, "blocked", "claude_code_controls_unverified", REAL_BLOCK_REASON)
        return ExecutionAdmission(True, "strict", "claude_file_hooks_verified",
            "严格文件工具模式：工具操作逐次审批；原生子 Agent 默认关闭，明确开启后逐次审批并计入全树配额。"
            "不开放 Shell、MCP、后台运行与原生续接；Token 预算不是单回合硬上限。")

    def _strict_problems(self) -> list[str]:
        if type(self.launcher) is not ClaudeCodeLauncher or type(self.runner) is not ProcessRunner:
            return ["unsupported_launcher"]
        if (self.launcher.expected_identity or {}).get("sha256") not in STRICT_VERIFIED_SHA256:
            return ["strict_profile_not_verified_for_binary"]
        problems = self.launcher.supervised_problems()
        if _inside(self.workdir_root, REPO_ROOT):
            problems.append("workdir_inside_repository")
        if not (type(self.timeout_seconds) in (int, float) and math.isfinite(self.timeout_seconds)
                and 0 <= self.timeout_seconds <= 86400):
            problems.append("invalid_timeout")
        return problems

    def supports_tool_approval(self) -> bool:
        return True

    def start_streaming(self, invocation_id, snapshot, *, gate_token=None, emit=None, tool_gate=None):
        # Text output remains the final JSON object. This extension supplies
        # the kernel ToolGate while the CLI waits on its live approval cards.
        return self.start(invocation_id, snapshot, gate_token=gate_token, tool_gate=tool_gate)

    def supervised_execution_admission(self) -> ExecutionAdmission:
        if self._simulated():
            return self.execution_admission()
        if type(self.launcher) is not ClaudeCodeLauncher or type(self.runner) is not ProcessRunner:
            return ExecutionAdmission(False, "blocked", "claude_code_controls_unverified", REAL_BLOCK_REASON)
        try:
            problems = self.launcher.supervised_problems()
        except Exception:  # noqa: BLE001
            problems = ["launcher_check_failed"]
        adopted = bool(getattr(self.launcher, "_adopted_identity", None))
        if not (type(self.timeout_seconds) in (int, float) and math.isfinite(self.timeout_seconds)
                and (0 <= self.timeout_seconds <= 86400 if adopted else 0 < self.timeout_seconds <= 600)):
            problems.append("timeout")
        if _inside(self.workdir_root, REPO_ROOT):
            problems.append("workdir_inside_repository")
        if problems:
            reason = (
                "用户监督模式要求已确认的程序身份、固定启动参数、仓库外的配置目录与工作目录、"
                "有效执行时限；显式设置的代理与 CA 也须有效。未满足："
                if adopted else
                "用户监督模式要求代码常量中的固定身份并已记录零模型身份核验、固定启动参数、"
                "仓库外的配置目录与工作目录、代理和可读的 CA、有效执行时限。未满足："
            ) + ", ".join(problems)
            if "executable_missing" in problems:
                reason += "。已采用的 Claude Code 程序不存在，可能已被更新移除；请在本机设置重新扫描或填写现有 exe 路径，确认保存后重新创建并审批调用。"
            if "executable_unreadable" in problems:
                reason += "。Hub 无法读取 Claude Code 程序；请检查当前用户的读取权限。"
            return ExecutionAdmission(
                False, "blocked", "claude_code_supervised_conditions",
                reason)
        return ExecutionAdmission(True, "supervised", "user_supervised_boundary", SUPERVISED_REASON)

    def health(self) -> bool:
        return self.identity_matches()[0]

    def identity_matches(self) -> tuple[bool, str]:
        check = getattr(self.launcher, "identity_matches", None)
        if not callable(check):
            return False, "launcher has no identity check"
        return check()

    # -- gated start --------------------------------------------------------

    def start(self, invocation_id: str, snapshot: InvocationSnapshot, *,
              gate_token: Optional[str] = None, tool_gate: Any = None) -> AdapterStartResult:
        if not gate_token:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: no kernel gate token (invocation not approved)")
        binding = self.binding_identity()
        if binding == "unavailable" or snapshot.adapter_version != binding:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: approved for another adapter identity; nothing started")
        admission = execution_admission(self, snapshot.execution_mode)
        if not admission.allowed:
            return AdapterStartResult("", ok=False, terminal=True, detail=admission.reason)
        agent_id = (snapshot.agent_id or "").strip()
        if agent_id not in self.AGENT_IDS:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail=f"refused: agent_id {agent_id!r} is not served by this adapter")
        if snapshot.context_mode != INDEPENDENT:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail="refused: this adapter cannot resume a provider session")
        if type(self.runner) is not FakeClaudeRunner:
            try:
                problems = self.launcher.launch_problems()
            except Exception as exc:  # noqa: BLE001
                problems = [f"launcher_check_failed:{type(exc).__name__}"]
            if problems:
                return AdapterStartResult("", ok=False, terminal=True,
                                          detail="refused before launch: " + ", ".join(problems))
        try:
            from agenthub.workspace import approved_workspace, execution_timeout
            workspace = approved_workspace(snapshot)
            if execution_timeout(snapshot, self.timeout_seconds) != self.timeout_seconds:
                raise ValueError("approved execution time limit differs from the current adapter")
            native_strict = snapshot.execution_mode == "strict" and not self._simulated()
            if native_strict and tool_gate is None:
                raise ValueError("strict execution requires the kernel native tool gate")
            if native_strict and workspace and (_inside(self.workdir_root, workspace)
                    or _inside(self.launcher.config_dir, workspace)):
                raise ValueError("approved workspace overlaps protected execution configuration")
            # Keep executable project customizations out of the CLI cwd. The
            # kernel scopes file tool requests to the approved workspace.
            workdir = (os.path.join(self.workdir_root, invocation_id) if native_strict
                       else workspace or os.path.join(self.workdir_root, invocation_id))
            os.makedirs(workdir, exist_ok=True)
            if (native_strict or not workspace) and os.listdir(workdir):
                return AdapterStartResult("", ok=False, terminal=True,
                                          detail="refused: work directory is not empty; nothing started")
            logs = os.path.join(self.workdir_root, "_logs")
            scratch = os.path.join(self.workdir_root, "_scratch")
            os.makedirs(logs, exist_ok=True)
            os.makedirs(scratch, exist_ok=True)
            env, stripped, added = self.launcher.environment()
        except (OSError, ValueError) as exc:
            return AdapterStartResult("", ok=False, terminal=True,
                                      detail=f"refused: launch preparation failed ({type(exc).__name__})")

        argv = list(self.launcher.argv)
        if snapshot.model:
            argv += ["--model", snapshot.model]
        if snapshot.reasoning_effort:
            argv += ["--effort", snapshot.reasoning_effort]
        stderr_path = os.path.join(logs, f"{invocation_id}-claude-stderr.log")
        stdout_path = os.path.join(scratch, f"{invocation_id}-{uuid.uuid4().hex}.json")
        # Past this line a launch is attempted. Record first: a crash afterwards
        # reads as "may be running", never as "never ran".
        self._start_count += 1
        self.store.record_start(invocation_id, adapter_invocation_id="")
        facts = {
            "transport": TRANSPORT, "workdir": workdir, "argv_tail": argv[-len(CLAUDE_ARGV_TAIL):],
            "input_sha256": hashlib.sha256(snapshot.task_input.encode("utf-8")).hexdigest(),
            "input_chars": len(snapshot.task_input), "stderr_log": stderr_path,
            "env_stripped": stripped, "env_set": added,  # names only
        }
        try:
            with contextlib.ExitStack() as stack:
                if native_strict:
                    from agenthub_adapters.claude_hooks import ClaudeHookBridge
                    bridge = stack.enter_context(ClaudeHookBridge(tool_gate, cwd=workdir,
                        config_root=os.path.join(self.workdir_root, "_hooks"),
                        permissions=snapshot.permissions, timeout=min(tool_gate.ttl_seconds, 300)))
                    argv = [self.launcher.path, *bridge.argv()]
                    if snapshot.model:
                        argv += ["--model", snapshot.model]
                    if snapshot.reasoning_effort:
                        argv += ["--effort", snapshot.reasoning_effort]
                    env["CLAUDE_CODE_DISABLE_FILE_MENTIONS"] = "1"
                    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
                    env["CLAUDE_CODE_FORK_SUBAGENT"] = "0"
                    env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] = "0"
                    facts["env_set"] = sorted(set(added) | {
                        "CLAUDE_CODE_DISABLE_FILE_MENTIONS", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
                        "CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"})
                    facts["strict_profile"] = STRICT_PROFILE
                    facts["argv_tail"] = []  # do not retain credential-bearing settings paths
                run = self.runner.run(invocation_id, argv, env, workdir, snapshot.task_input,
                                      self.timeout_seconds, stdout_path, stderr_path)
        except ClaudeLaunchFailed as exc:
            # Provably nothing is running: definitive, the slot may be freed.
            self.store.set_payload(invocation_id, {**facts, "verdict": FAILED, "terminal": True,
                                                   "launched": False, "detail": str(exc)[:200]})
            self.store.set_status(invocation_id, FAILED, str(exc)[:400])
            return AdapterStartResult("", ok=False, terminal=True, detail=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - a process may exist
            detail = f"runner raised {type(exc).__name__}; outcome unconfirmed"
            self.store.set_payload(invocation_id, {**facts, "verdict": RUNNING, "terminal": False,
                                                   "detail": detail})
            self.store.set_status(invocation_id, RUNNING, detail)
            return AdapterStartResult("", ok=False, terminal=False, detail=detail)

        parsed = parse_result(run.stdout_text)
        verdict, terminal, detail = classify(run, parsed)
        if native_strict and tool_gate.native_unfinished():
            verdict, terminal, detail = RUNNING, False, "native child end state is unconfirmed; quota retained"
        result = parsed.result or {}
        session_id = result.get("session_id") if isinstance(result.get("session_id"), str) else ""
        local_id = session_id or f"claude-code-{invocation_id}"
        self.store.set_adapter_id(invocation_id, local_id)
        cost = result.get("total_cost_usd")
        model_usage = result.get("modelUsage")
        denials = result.get("permission_denials")
        payload = {
            **facts,
            "thread_id": local_id,
            "session_id": session_id,
            # Only a confirmed success carries a reply; an error text is the CLI's own message.
            "final_message": result.get("result", "") if verdict == FINISHED else "",
            "usage": parsed.usage,
            "usage_raw": parsed.usage_raw,
            "model_usage": parsed.model_usage,
            "result_meta": result_meta(parsed.result),
            "cost_usd_estimate": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            "models": sorted(model_usage) if isinstance(model_usage, dict) else [],
            "num_turns": result.get("num_turns") if isinstance(result.get("num_turns"), int) else None,
            "subtype": result.get("subtype", ""),
            "is_error": result.get("is_error"),
            "error_message": (str(result.get("result", ""))[:ERROR_TEXT_LIMIT]
                              if result.get("is_error") is True else ""),
            "errors": [str(e)[:200] for e in (result.get("errors") or [])[:3]]
            if isinstance(result.get("errors"), list) else [],
            "permission_denials_count": len(denials) if isinstance(denials, list) else None,
            "output_state": "result" if parsed.result is not None else ("empty" if parsed.empty else "malformed"),
            "exit_code": run.exit_code,
            "timed_out": run.timed_out,
            "local_stop_requested": run.local_stop_requested,
            "elapsed": round(run.elapsed, 2),
            "input_written": run.input_written,
            "stdout_bytes": run.stdout_bytes,
            "stderr_bytes": run.stderr_bytes,
            "stderr_sha256": run.stderr_sha256,
            "workdir_entries_after": sorted(os.listdir(workdir)) if os.path.isdir(workdir) else [],
            "verdict": verdict,
            "terminal": terminal,
            "detail": detail,
        }
        if verdict == FAILED:
            from agenthub_adapters.quota import quota_error
            payload["provider_quota"] = quota_error(result)
        self.store.set_payload(invocation_id, payload)
        self.store.set_status(invocation_id, verdict, detail[:400])
        if verdict == FINISHED:
            return AdapterStartResult(local_id, ok=True, terminal=True, detail=detail)
        if verdict == FAILED:
            return AdapterStartResult(local_id, ok=False, terminal=True, detail=detail[:200])
        return AdapterStartResult(local_id, ok=False, terminal=False, detail=detail[:200])

    # -- stop / cancel / status --------------------------------------------

    def cancel(self, adapter_invocation_id: str) -> Any:
        return {"ok": False, "reason": "unsupported",
                "detail": "Claude Code cancellation is not implemented; not claiming a stop"}

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self.cancel(invocation_id)

    def stop_local_invocation(self, invocation_id: str) -> dict:
        if type(self.runner) is not ProcessRunner:
            return {"local_stop_requested": False, "local_process_exited": False,
                    "remote_stop_confirmed": False, "reason": "unsupported"}
        return self.runner.stop_local(invocation_id)

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
        # No native_session: this adapter cannot resume, so nothing may bind one.
        return {"status": record.get("status"), "thread_id": payload.get("thread_id", ""),
                "final_message": payload.get("final_message", ""), "usage": payload.get("usage", {})}

    def context_profile(self) -> dict[str, Any]:
        """What the context preview may say about this adapter (T-1d)."""
        return {**CONTEXT_PROFILE, "observed": [dict(o) for o in CONTEXT_PROFILE["observed"]]}

    def usage_report(self, invocation_id: str) -> Optional[dict[str, Any]]:
        """This call's usage for the kernel ledger; ``None`` without a record.

        Each call is its own process and session (``--no-session-persistence``),
        so the result's ``usage`` is this call's. Whether it also covers the
        CLI's helper requests under several models is unverified.
        """
        record = self.store.get(invocation_id)
        if not record:
            return None
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if payload.get("launched") is False:
            return {"mapping": MAPPING_CLAUDE_CODE, "usage": dict(NOT_LAUNCHED_USAGE),
                    "source": "not_launched", "notes": []}
        # Records from before T-1a hold the four documented fields only.
        usage = payload.get("usage_raw") or payload.get("usage") or {}
        notes: list[str] = []
        models = payload.get("models") if isinstance(payload.get("models"), list) else []
        if len(models) > 1:
            notes.append("multi_model_usage_unverified")
        if not usage:
            notes.append("no_usage_reported")
        return {"mapping": MAPPING_CLAUDE_CODE, "usage": usage, "source": "cli_result",
                "notes": notes, "model": models[0] if len(models) == 1 else None,
                "model_usage": payload.get("model_usage") or {}, "model_source": "provider_usage"}
