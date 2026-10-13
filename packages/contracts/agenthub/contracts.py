"""Canonical data model & state machines for Agent Hub (P2 Mock kernel).

This module defines the shared types that the api, worker and adapters all
agree on. Design mirrors ``AGENT_HUB_PLAN.md`` §4 (safety valves) and §5
(data design), plus the appendix of safety-valve test cases in §6.

Key invariants implemented here and enforced by the worker:

* Only approved approvals can be consumed: ``pending -> approved -> consumed``.
  Pending or approved approvals can instead expire or be revoked; pending
  approvals can also be rejected. These terminal states cannot be consumed.
* ``consumed`` is terminal and may only happen once.
* Every invocation carries an immutable ``payload_hash``; the approval is bound
  to that hash. If the invocation payload changes, the old approval is void.
* Quotas (``max_subagents_total``, ``max_subagents_concurrent``, ``max_depth``)
  are scoped to the whole *root task tree*, not per parent agent.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Enums / state machines
# --------------------------------------------------------------------------- #


class InvocationStatus(str, enum.Enum):
    """Lifecycle of a single agent invocation.

    ``draft -> awaiting_approval -> queued -> starting -> running
       -> succeeded | failed | canceled | timed_out | unknown``
    plus ``rejected`` (user denied) and ``invalid`` (snapshot changed).
    """

    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    INVALID = "invalid"


#: States that mean "this invocation has consumed an approximation slot and is
#: actively occupying a concurrent slot."
RUNNING_OR_STARTING = frozenset(
    {InvocationStatus.STARTING, InvocationStatus.RUNNING}
)

#: Terminal states. Once an invocation reaches one of these it may not move on.
TERMINAL_STATUSES = frozenset(
    {
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.CANCELED,
        InvocationStatus.TIMED_OUT,
        InvocationStatus.UNKNOWN,
        InvocationStatus.REJECTED,
        InvocationStatus.INVALID,
    }
)


class ApprovalStatus(str, enum.Enum):
    """Lifecycle of a user approval.

    Only ``pending -> approved -> consumed`` grants execution. Rejected,
    expired, revoked and consumed approvals cannot grant execution.
    ``consumed`` is terminal; a consumed approval can never be reused.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


#: Approvals that still allow the worker to start the adapter.
CONSUMABLE_APPROVAL_STATUSES = frozenset(
    {ApprovalStatus.APPROVED}
)

#: Terminal approval statuses (no further action possible).
TERMINAL_APPROVAL_STATUSES = frozenset(
    {
        ApprovalStatus.REJECTED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.REVOKED,
        ApprovalStatus.CONSUMED,
    }
)


class Capability(str, enum.Enum):
    """Adapter capability flags (mirrors docs/adapter-capabilities.md)."""

    POST_MESSAGE = "post_message"
    STREAM = "stream"
    RESUME_SESSION = "resume_session"
    CANCEL = "cancel"
    LIMIT_DIR = "limit_dir"
    INTERCEPT_TOOL = "intercept_tool"
    CONTROL_SUBAGENT = "control_subagent"
    REAL_USAGE = "real_usage"


class DelegationKind(str, enum.Enum):
    """How a task was created."""

    USER = "user"
    AUTO = "auto"


# --------------------------------------------------------------------------- #
# Invocation snapshot & payload hash
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InvocationSnapshot:
    """Immutable snapshot of everything the user approved.

    The approval is bound to ``payload_hash``; changing any of these fields
    produces a different hash and voids the approval (see AGENT_HUB_PLAN §4.1).
    """

    agent_id: str
    task_input: str
    parent_task_id: Optional[str]
    depth: int
    permissions: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    adapter_version: str = "unknown"
    policy_version: int = 0

    def payload_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8"))
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_input": self.task_input,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
            "permissions": self.permissions,
            "resources": self.resources,
            "adapter_version": self.adapter_version,
            "policy_version": self.policy_version,
        }


# --------------------------------------------------------------------------- #
# Policy (quota settings)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Policy:
    """User-settable limits. See AGENT_HUB_PLAN §4.2.

    ``max_subagents_total`` and ``max_subagents_concurrent`` are counted across
    the whole root task tree (not per parent). ``max_depth`` is 0-based: the
    main agent is depth 0, a sub-agent is depth 1, a grandchild is depth 2.
    """

    max_subagents_total: int = 3
    max_subagents_concurrent: int = 2
    max_depth: int = 1
    max_agent_invocations: int = 10
    run_time_limit_seconds: int = 600
    root_task_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        # Validate the whole policy up front so an illegal update is rejected
        # before anything is persisted (the caller rolls back on ValueError).
        for name in (
            "max_subagents_total",
            "max_subagents_concurrent",
            "max_depth",
            "max_agent_invocations",
            "run_time_limit_seconds",
            "root_task_ttl_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer, got {value!r}")
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")
        if self.max_agent_invocations < 1:
            raise ValueError("max_agent_invocations must be at least 1")
        if self.run_time_limit_seconds < 1:
            raise ValueError("run_time_limit_seconds must be at least 1")
        if self.max_subagents_concurrent > self.max_subagents_total:
            raise ValueError(
                "max_subagents_concurrent cannot exceed max_subagents_total"
            )

    @property
    def version(self) -> int:
        # Policy objects are immutable settings; the authoritative version lives
        # on the root task row (``TaskRow.policy_version``) so it can increment
        # per update. This property exists only for the stored payload shape.
        return 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_subagents_total": self.max_subagents_total,
            "max_subagents_concurrent": self.max_subagents_concurrent,
            "max_depth": self.max_depth,
            "max_agent_invocations": self.max_agent_invocations,
            "run_time_limit_seconds": self.run_time_limit_seconds,
            "root_task_ttl_seconds": self.root_task_ttl_seconds,
            "version": self.version,
        }


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #


@dataclass
class AdapterStartResult:
    """What the worker takes away from a successful adapter start."""

    adapter_invocation_id: str
    ok: bool = True
    detail: str = ""


class AdapterContract:
    """Interface every adapter must implement.

    Methods mirror AGENT_HUB_PLAN §5: ``capabilities / health / start /
    stream / cancel / get_status``. Unsupported operations must return an
    explicit ``unsupported`` result rather than falsely claiming success.
    """

    name: str = "base"
    version: str = "0.0.0"

    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> bool:
        raise NotImplementedError

    def start(self, invocation_id: str, snapshot: InvocationSnapshot) -> AdapterStartResult:
        raise NotImplementedError

    def cancel(self, adapter_invocation_id: str) -> Any:
        raise NotImplementedError

    def get_status(self, adapter_invocation_id: str) -> Any:
        raise NotImplementedError
