"""Several agents behind one Hub: each agent_id is served by its own adapter.

The router itself never runs anything. The kernel asks it for the adapter of a
call's ``agent_id`` (``for_agent``) and then checks admission, binding identity,
tool approval and native resume on THAT adapter, exactly as it does for a single
adapter. Anything that still talks to the router directly gets fail-closed
answers: launch admission is blocked and the binding identity is "unavailable",
so a call can never run under a router-level decision.

Calls a router does not route by snapshot (local stop, interrupt, cancel,
records) are dispatched to the adapter that holds the invocation's record, or
to the one the router handed it to in this process.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from agenthub.contracts import ExecutionAdmission

UNAVAILABLE = "unavailable"
NOT_ROUTED = {"ok": False, "reason": "not_routed"}


class RoutingAdapter:
    name = "router"
    version = "router"

    def __init__(self, adapters: dict[str, Any], *, allow_empty: bool = False) -> None:
        if not adapters and not allow_empty:
            raise ValueError("a router needs at least one agent")
        served = {}
        for agent_id, adapter in adapters.items():
            ids = set(getattr(adapter, "AGENT_IDS", ()) or ())
            if ids and agent_id not in ids:
                raise ValueError(f"adapter {type(adapter).__name__} does not serve {agent_id!r}")
            served[agent_id] = adapter
        self._adapters = served
        self._owner: dict[str, str] = {}  # invocation_id -> agent_id, this process only
        self._lock = threading.Lock()

    @property
    def AGENT_IDS(self) -> frozenset:  # noqa: N802 - same name as on every adapter
        return frozenset(self._adapters)

    def agents(self) -> dict[str, Any]:
        return dict(self._adapters)

    def for_agent(self, agent_id: Optional[str]) -> Optional[Any]:
        """The adapter for this agent, or None for an agent no adapter serves."""
        return self._adapters.get(agent_id) if isinstance(agent_id, str) else None

    def note_owner(self, invocation_id: str, agent_id: str) -> None:
        """The kernel records which agent's adapter a call was started on."""
        with self._lock:
            self._owner[invocation_id] = agent_id

    def owner_of(self, invocation_id: str) -> Optional[Any]:
        with self._lock:
            agent_id = self._owner.get(invocation_id)
        if agent_id is not None:
            return self._adapters.get(agent_id)
        for adapter in self._adapters.values():
            lookup = getattr(adapter, "lookup_by_invocation", None)
            try:
                if callable(lookup) and lookup(invocation_id) is not None:
                    return adapter
            except Exception:  # noqa: BLE001 - an unreadable store owns nothing
                continue
        return None

    # -- fail-closed answers for router-level questions ----------------------

    def execution_admission(self) -> ExecutionAdmission:
        return ExecutionAdmission(False, "blocked", "agent_not_resolved",
                                  "调用没有按 Agent 选择适配器，已阻止启动。")

    def supervised_execution_admission(self) -> ExecutionAdmission:
        return self.execution_admission()

    def binding_identity(self) -> str:
        return UNAVAILABLE

    def capabilities(self) -> dict[str, Any]:
        out = {}
        for agent_id, adapter in self._adapters.items():
            caps = getattr(adapter, "capabilities", None)
            out[agent_id] = caps() if callable(caps) else {}
        return {"router": True, "agents": out}

    def health(self) -> bool:
        return all(bool(getattr(a, "health", lambda: False)()) for a in self._adapters.values())

    # -- by invocation id ------------------------------------------------------

    def _dispatch(self, method: str, invocation_id: str, default: Any) -> Any:
        adapter = self.owner_of(invocation_id)
        fn = getattr(adapter, method, None) if adapter is not None else None
        return fn(invocation_id) if callable(fn) else default

    def lookup_by_invocation(self, invocation_id: str) -> Optional[dict[str, Any]]:
        return self._dispatch("lookup_by_invocation", invocation_id, None)

    def last_result(self, invocation_id: str) -> Optional[dict[str, Any]]:
        return self._dispatch("last_result", invocation_id, None)

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self._dispatch("cancel_invocation", invocation_id, dict(NOT_ROUTED))

    def interrupt_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self._dispatch("interrupt_invocation", invocation_id, dict(NOT_ROUTED))

    def stop_local_invocation(self, invocation_id: str) -> dict[str, Any]:
        return self._dispatch("stop_local_invocation", invocation_id, {
            "local_stop_requested": False, "local_process_exited": False,
            "remote_stop_confirmed": False, "reason": "not_routed"})
