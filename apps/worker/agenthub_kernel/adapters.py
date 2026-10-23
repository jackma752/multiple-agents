"""Adapter selection.

One place decides which adapter the API/Worker use, so the console, tests and the
CLI all agree:

* ``mock``  - the inert default. No process is launched. Safe everywhere.
* ``codex`` - strict admission remains blocked. Explicit per-call supervised
  mode allows bounded operation without attesting native-child control.
  ``AGENT_HUB_CODEX_TRANSPORT=app-server`` selects the optional app-server
  transport: the bundled offline stub (simulation), or the real app-server
  pinned by code constant, admitted in supervised mode only
  (docs/appserver-s6-plan.md §4.1).
* ``claude-code`` - one ``claude -p`` process per call (docs/claude-code-adapter.md).
  Strict is blocked; supervised admits only the pinned constant after its
  zero-model identity check is recorded. ``AGENT_HUB_CLAUDE_CODE_STUB`` selects
  the bundled offline stub (simulation).

:func:`adapter_binding` is what a new call's snapshot binds to
(``adapter_version``), so an approval given under one pinned identity can never
run under another (D11).
"""

from __future__ import annotations

import os
from typing import Any, Optional

from agenthub.contracts import execution_admission
from agenthub_kernel import config

MOCK = "mock"
CODEX = "codex"
CLAUDE_CODE = "claude-code"
KNOWN = (MOCK, CODEX, CLAUDE_CODE)
#: Adapter ``name`` -> kind, for adapters that launch a real process.
REAL_KINDS = {CODEX: CODEX, CLAUDE_CODE: CLAUDE_CODE}


def adapter_kind() -> str:
    kind = config.adapter_kind()
    return kind if kind in KNOWN else MOCK


def build_adapter(kind: Optional[str] = None, *, force_mock: bool = False) -> Any:
    """Construct the adapter. Mock unless codex is explicitly requested.

    ``force_mock`` lets tests guarantee no real process can be launched even if
    the environment happens to set ``AGENT_HUB_ADAPTER=codex``.
    """
    kind = MOCK if force_mock else (kind or adapter_kind())

    if kind == CLAUDE_CODE:
        return _build_claude_code(config.claude_code_settings())

    if kind == CODEX:
        from agenthub_adapters.codex import CodexAdapter, StubCodexRunner, is_bundled_stub_command
        from agenthub_adapters.store import ExecutionStore, ensure_parent

        s = config.codex_settings()
        if s.get("transport", "exec") == "app-server":
            return _build_appserver(s)
        store = ExecutionStore(
            path=ensure_parent(s["store_path"]), table="codex_executions"
        )
        prefix = s.get("command_prefix") or []
        runner = None
        if is_bundled_stub_command(s["codex_path"], prefix):
            runner = StubCodexRunner(os.path.join(s["workdir_root"], "_scratch"))
        return CodexAdapter(
            codex_path=s["codex_path"],
            codex_home=s["codex_home"],
            workdir_root=s["workdir_root"],
            store=store,
            runner=runner,
            proxy=s["proxy"] or None,
            ca_bundle=s["ca_bundle"] or None,
            timeout_seconds=s["timeout_seconds"],
            # Defaults to read-only (config.codex_sandbox falls back to read-only on
            # any unrecognised value); an explicit env var is the only way to widen it.
            sandbox=s["sandbox"],
            expected_identity=s["expected_identity"],
            version=s["version"],
            command_prefix=s.get("command_prefix") or None,
        )

    from agenthub_adapters.mock import MockAdapter

    return MockAdapter()


def _build_appserver(s: dict) -> Any:
    from agenthub_adapters.appserver_adapter import AppServerCodexAdapter, CodexAppServerFactory
    from agenthub_adapters.appserver_stub import SCENARIOS, StdioStubFactory
    from agenthub_adapters.store import ExecutionStore, ensure_parent

    if s.get("appserver_stub") in SCENARIOS:
        factory: Any = StdioStubFactory(s["appserver_stub"])
        version = "appserver-stub"
    else:
        # Identity from the reviewed constant only; exec's env overrides are
        # recorded so supervised admission can refuse them (D7).
        factory = CodexAppServerFactory.pinned(
            codex_home=s["codex_home"], proxy=s["proxy"] or None, ca_bundle=s["ca_bundle"] or None,
            env_overrides=tuple(s.get("identity_env_overrides") or ()),
        )
        version = factory.version
    store = ExecutionStore(
        path=ensure_parent(s.get("appserver_store_path") or config.appserver_execution_store_path()),
        table="appserver_executions",
    )
    return AppServerCodexAdapter(
        transport_factory=factory, workdir_root=s["workdir_root"], store=store,
        timeout_seconds=s["timeout_seconds"], version=version,
    )


def _build_claude_code(s: dict) -> Any:
    from agenthub_adapters.claude_code import (
        STUB_SCENARIOS, ClaudeCodeAdapter, ClaudeCodeLauncher, StubClaudeLauncher,
    )
    from agenthub_adapters.store import ExecutionStore, ensure_parent

    if s.get("stub") in STUB_SCENARIOS:
        launcher: Any = StubClaudeLauncher(s["stub"])
    else:
        # Identity from the reviewed constant only; supervised admission decides.
        launcher = ClaudeCodeLauncher.pinned(config_dir=s["config_dir"], proxy=s["proxy"] or None,
                                             ca_bundle=s["ca_bundle"] or None)
    store = ExecutionStore(path=ensure_parent(s["store_path"]), table="claude_code_executions")
    return ClaudeCodeAdapter(launcher=launcher, workdir_root=s["workdir_root"], store=store,
                             timeout_seconds=s["timeout_seconds"])


#: Binding of an adapter that declares no identity (exec, Mock): equal only to itself.
UNKNOWN_BINDING = "unknown"
#: A declaration that failed or returned nothing: it never matches anything.
UNAVAILABLE_BINDING = "unavailable"


def adapter_binding(adapter: Any) -> str:
    """The identity a new call's snapshot binds to (``adapter_version``).

    ``unknown`` when the adapter declares none; ``unavailable`` when its
    declaration fails. The Worker compares the recorded and the current value
    on both sides and refuses ``unavailable`` outright.
    """
    bind = getattr(adapter, "binding_identity", None)
    if not callable(bind):
        return UNKNOWN_BINDING
    try:
        value = bind()
    except Exception:  # noqa: BLE001
        return UNAVAILABLE_BINDING
    if not isinstance(value, str) or not value or value == UNKNOWN_BINDING:
        return UNAVAILABLE_BINDING  # a declared identity cannot pose as "none declared"
    return value


def describe(adapter: Any) -> dict:
    """Small, safe summary for the console header."""
    # Which agent_id values this adapter will serve. The console uses this to
    # pre-fill its agent field: a call created for an agent the adapter does not
    # serve can never launch, so the UI must not silently hardcode one. An empty
    # list means "no declared restriction" (the inert Mock adapter).
    name = getattr(adapter, "name", "")
    info = {
        "name": getattr(adapter, "name", type(adapter).__name__),
        "kind": REAL_KINDS.get(name, MOCK),
        "real_process": name in REAL_KINDS,
        "agent_ids": sorted(getattr(adapter, "AGENT_IDS", ()) or ()),
    }
    admission = execution_admission(adapter)
    info["execution_admission"] = admission.to_dict()
    info["supervised_admission"] = execution_admission(adapter, "supervised").to_dict()
    info["real_process"] = info["real_process"] and admission.mode != "simulation"
    if info["kind"] in (CODEX, CLAUDE_CODE):
        ok, why = adapter.identity_matches()
        info["identity_ok"] = ok
        info["identity_detail"] = why
        info["version"] = getattr(adapter, "version", "unknown")
        info["sandbox"] = getattr(adapter, "sandbox", "")
        info["subagent_disable_requested"] = bool(getattr(adapter, "disable_subagents", False))
        # Legacy field now reports evidence, not command-line intent.
        info["subagents_disabled"] = False
        info["proxy"] = getattr(adapter, "proxy", None)
        info["timeout_seconds"] = getattr(adapter, "timeout_seconds", None)
        info["transport"] = getattr(adapter, "transport", "exec")
        info["binding_identity"] = adapter_binding(adapter)
    supports = getattr(adapter, "supports_native_resume", None)
    info["native_resume"] = bool(supports()) if callable(supports) else False
    return info
