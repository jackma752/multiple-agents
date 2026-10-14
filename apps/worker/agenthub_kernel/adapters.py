"""Adapter selection.

One place decides which adapter the API/Worker use, so the console, tests and the
CLI all agree:

* ``mock``  - the inert default. No process is launched. Safe everywhere.
* ``codex`` - the real Codex CLI. Requires an explicit opt-in via
  ``AGENT_HUB_ADAPTER=codex`` (or ``--adapter codex``) because it launches a real
  process and spends real quota.
"""

from __future__ import annotations

from typing import Any, Optional

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
        from agenthub_adapters.codex import CodexAdapter
        from agenthub_adapters.store import ExecutionStore, ensure_parent

        s = config.codex_settings()
        store = ExecutionStore(
            path=ensure_parent(s["store_path"]), table="codex_executions"
        )
        return CodexAdapter(
            codex_path=s["codex_path"],
            codex_home=s["codex_home"],
            workdir_root=s["workdir_root"],
            store=store,
            proxy=s["proxy"] or None,
            ca_bundle=s["ca_bundle"] or None,
            timeout_seconds=s["timeout_seconds"],
            expected_identity=s["expected_identity"],
            version=s["version"],
            command_prefix=s.get("command_prefix") or None,
        )

    from agenthub_adapters.mock import MockAdapter

    return MockAdapter()


def describe(adapter: Any) -> dict:
    """Small, safe summary for the console header."""
    info = {
        "name": getattr(adapter, "name", type(adapter).__name__),
        "kind": CODEX if getattr(adapter, "name", "") == "codex" else MOCK,
        "real_process": getattr(adapter, "name", "") == "codex",
    }
    if info["kind"] == CODEX:
        ok, why = adapter.identity_matches()
        info["identity_ok"] = ok
        info["identity_detail"] = why
        info["version"] = getattr(adapter, "version", "unknown")
        info["sandbox"] = getattr(adapter, "sandbox", "")
        info["subagents_disabled"] = bool(getattr(adapter, "disable_subagents", False))
        info["proxy"] = getattr(adapter, "proxy", None)
        info["timeout_seconds"] = getattr(adapter, "timeout_seconds", None)
    return info
