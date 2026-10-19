"""Adapter selection.

One place decides which adapter the API/Worker use, so the console, tests and the
CLI all agree:

* ``mock``  - the inert default. No process is launched. Safe everywhere.
* ``codex`` - strict admission remains blocked. Explicit per-call supervised
  mode allows bounded operation without attesting native-child control.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from agenthub.contracts import execution_admission
from agenthub_kernel import config

MOCK = "mock"
CODEX = "codex"
KNOWN = (MOCK, CODEX)


def adapter_kind() -> str:
    kind = config.adapter_kind()
    return kind if kind in KNOWN else MOCK


def build_adapter(kind: Optional[str] = None, *, force_mock: bool = False) -> Any:
    """Construct the adapter. Mock unless codex is explicitly requested.

    ``force_mock`` lets tests guarantee no real process can be launched even if
    the environment happens to set ``AGENT_HUB_ADAPTER=codex``.
    """
    kind = MOCK if force_mock else (kind or adapter_kind())

    if kind == CODEX:
        from agenthub_adapters.codex import CodexAdapter, StubCodexRunner, is_bundled_stub_command
        from agenthub_adapters.store import ExecutionStore, ensure_parent

        s = config.codex_settings()
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


def describe(adapter: Any) -> dict:
    """Small, safe summary for the console header."""
    # Which agent_id values this adapter will serve. The console uses this to
    # pre-fill its agent field: a call created for an agent the adapter does not
    # serve can never launch, so the UI must not silently hardcode one. An empty
    # list means "no declared restriction" (the inert Mock adapter).
    info = {
        "name": getattr(adapter, "name", type(adapter).__name__),
        "kind": CODEX if getattr(adapter, "name", "") == "codex" else MOCK,
        "real_process": getattr(adapter, "name", "") == "codex",
        "agent_ids": sorted(getattr(adapter, "AGENT_IDS", ()) or ()),
    }
    admission = execution_admission(adapter)
    info["execution_admission"] = admission.to_dict()
    info["supervised_admission"] = execution_admission(adapter, "supervised").to_dict()
    info["real_process"] = info["real_process"] and admission.mode != "simulation"
    if info["kind"] == CODEX:
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
    return info
