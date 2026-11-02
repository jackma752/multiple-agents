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

from agenthub.context import check_preview


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

INDEPENDENT = "independent"
NATIVE_RESUME = "native_resume"
CONTEXT_MODES = frozenset({INDEPENDENT, NATIVE_RESUME})


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
    execution_mode: str = "strict"
    #: ``independent`` sends only ``task_input``. ``native_resume`` asks the
    #: adapter to continue the provider thread ``resume_thread_id``, which
    #: exposes that thread's earlier context to the call - so the thread id is
    #: part of what the user approves.
    context_mode: str = "independent"
    resume_thread_id: Optional[str] = None
    #: Earlier replies the user chose to hand to this agent. Their text is
    #: already inside ``task_input``; each entry records where it came from
    #: (``message_id``, ``agent_id``, ``sha256``, ``chars``), so the approval
    #: shows and binds exactly which content is passed on.
    handoff: tuple = ()
    #: Pre-send context preview (T-1d, ``agenthub.context``): an exact split
    #: of ``task_input`` into parts with estimates, plus the provider-side
    #: context marked unknown. Checked against ``task_input`` here and bound
    #: by ``payload_hash``. ``None`` for snapshots made before T-1d and for
    #: calls not composed by the conversation path.
    context_preview: Optional[dict] = None
    #: Requested model and effort are approved options, never prompt text.
    #: None keeps provider defaults and preserves hashes of older snapshots.
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    model_provider: Optional[str] = None

    def __post_init__(self) -> None:
        from agenthub.native import native_profile
        native_profile(self.permissions)
        from agenthub.workspace import execution_timeout
        execution_timeout(self, None)
        from agenthub.models import validate_selection
        validate_selection(self.model, self.reasoning_effort)
        if self.execution_mode not in {"strict", "supervised"}:
            raise ValueError("execution_mode must be strict or supervised")
        if self.context_preview is not None:
            check_preview(self.task_input, self.context_preview)
        if isinstance(self.handoff, list):  # as stored by to_dict()
            object.__setattr__(self, "handoff", tuple(self.handoff))
        if not isinstance(self.handoff, tuple) or not all(isinstance(h, dict) for h in self.handoff):
            raise ValueError("handoff must be a sequence of source records")
        if self.context_mode not in CONTEXT_MODES:
            raise ValueError("context_mode must be independent or native_resume")
        if self.context_mode == NATIVE_RESUME:
            if not isinstance(self.resume_thread_id, str) or not self.resume_thread_id.strip():
                raise ValueError("native_resume requires resume_thread_id")
        elif self.resume_thread_id is not None:
            raise ValueError("resume_thread_id is only valid with native_resume")

    def payload_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps(self.to_dict(), sort_keys=True).encode("utf-8"))
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        result = {
            "agent_id": self.agent_id,
            "task_input": self.task_input,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
            "permissions": self.permissions,
            "resources": self.resources,
            "adapter_version": self.adapter_version,
            "policy_version": self.policy_version,
        }
        # Preserve hashes of existing strict-mode / independent snapshots.
        if self.execution_mode != "strict":
            result["execution_mode"] = self.execution_mode
        if self.context_mode != INDEPENDENT:
            result["context_mode"] = self.context_mode
            result["resume_thread_id"] = self.resume_thread_id
        if self.handoff:
            result["handoff"] = [dict(h) for h in self.handoff]
        if self.context_preview is not None:
            result["context_preview"] = self.context_preview
        for key in ("model", "reasoning_effort", "model_provider"):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        return result


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

    #: Cumulative totals and execution seconds: 0 means unlimited. Concurrency
    #: and depth remain actual limits, including zero as no children allowed.
    max_subagents_total: int = 0
    max_subagents_concurrent: int = 2
    max_depth: int = 1
    max_agent_invocations: int = 0
    run_time_limit_seconds: int = 0
    root_task_ttl_seconds: int = 3600
    collaboration_enabled: bool = False
    #: Token budgets (T-1c, ``agenthub.budget``), counted in ledger tokens:
    #: ``input_total`` (cache included) + ``output_total``. ``None`` = no
    #: budget, so stored policies without these keys behave as before. The
    #: per-call budget is what each call reserves against the tree; no adapter
    #: can stop a turn at N tokens, so it is never a hard per-call limit.
    token_budget_total: Optional[int] = None
    token_budget_per_call: Optional[int] = None
    token_budget_warn_percent: int = 80
    #: ``strict``: refuse calls that cannot be reliably metered or reserved, and
    #: any reservation that would cross the total. ``soft``: only a known
    #: exhausted budget refuses; the rest is a warning.
    token_budget_mode: str = "strict"
    #: Canonical JSON [agent, provider-interface, model] -> BudgetPolicy fields.
    #: These budgets share the root's transaction, but not its counters.
    model_budgets: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        from agenthub.models import validate_budgets
        validate_budgets(self.model_budgets)
        if type(self.collaboration_enabled) is not bool:
            raise ValueError("collaboration_enabled must be a boolean")
        for name in ("token_budget_total", "token_budget_per_call"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 1):
                raise ValueError(f"{name} must be a positive integer or null, got {value!r}")
        warn = self.token_budget_warn_percent
        if not isinstance(warn, int) or isinstance(warn, bool) or not 1 <= warn <= 100:
            raise ValueError(f"token_budget_warn_percent must be 1-100, got {warn!r}")
        if self.token_budget_mode not in {"strict", "soft"}:
            raise ValueError("token_budget_mode must be strict or soft")
        if (self.token_budget_total is not None and self.token_budget_per_call is not None
                and self.token_budget_per_call > self.token_budget_total):
            raise ValueError("token_budget_per_call cannot exceed token_budget_total")
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
        if self.max_subagents_total and self.max_subagents_concurrent > self.max_subagents_total:
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
            "collaboration_enabled": self.collaboration_enabled,
            "token_budget_total": self.token_budget_total,
            "token_budget_per_call": self.token_budget_per_call,
            "token_budget_warn_percent": self.token_budget_warn_percent,
            "token_budget_mode": self.token_budget_mode,
            "model_budgets": self.model_budgets,
            "version": self.version,
        }


# --------------------------------------------------------------------------- #
# Adapter contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExecutionAdmission:
    """Host-owned launch eligibility, independent of a user's approval.

    Strict mode fails closed for unknown controls (plan §4.3). Supervised mode
    is an explicit per-invocation boundary waiver, not a capability claim.
    Only reviewed adapter code may
    declare eligibility; requests, registry capability flags and environment
    variables cannot grant it. ``simulation`` never certifies a real Agent.
    """

    allowed: bool = False
    mode: str = "blocked"
    code: str = "adapter_controls_unverified"
    reason: str = "适配器控制能力未验证，已阻止启动。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "code": self.code,
            "reason": self.reason,
        }


def execution_admission(adapter: Any, execution_mode: str = "strict") -> ExecutionAdmission:
    """Safely query the trusted adapter; missing/broken declarations deny."""
    try:
        admission = adapter.execution_admission()
        if execution_mode not in {"strict", "supervised"}:
            return ExecutionAdmission()
        if execution_mode == "supervised":
            supervised = getattr(adapter, "supervised_execution_admission", None)
            if callable(supervised):
                admission = supervised()
    except Exception:
        return ExecutionAdmission()
    if (
        not isinstance(admission, ExecutionAdmission)
        or type(admission.allowed) is not bool
        or not isinstance(admission.mode, str)
        or not isinstance(admission.code, str)
        or not isinstance(admission.reason, str)
        or admission.mode not in {"blocked", "simulation", "strict", "supervised"}
        or (admission.mode == "supervised" and execution_mode != "supervised")
        or admission.allowed != (admission.mode != "blocked")
    ):
        return ExecutionAdmission()
    return admission


@dataclass
class AdapterStartResult:
    """What the worker takes away from an attempt to start a call.

    ``terminal`` distinguishes the two ways a start can be "not ok":

    * ``terminal=True`` (default) - the outcome is definitive and the invocation
      may be settled (e.g. ``turn.failed``, a refused request). The kernel frees
      the quota slot.
    * ``terminal=False`` - the outcome could NOT be confirmed: a timeout, or a
      cancellation the adapter cannot perform. The kernel must NOT free the slot,
      because the remote work may still be running. The invocation becomes
      ``unknown`` and keeps its slot until a human resolves it.
    """

    adapter_invocation_id: str
    ok: bool = True
    detail: str = ""
    terminal: bool = True
    #: Optional precision for a terminal, not-ok result. ``"canceled"`` means
    #: the provider itself reported the turn interrupted (a confirmed stop);
    #: anything else settles as ``failed``. Ignored unless ``terminal``.
    outcome: Optional[str] = None


class AdapterContract:
    """Interface every adapter must implement.

    Methods mirror AGENT_HUB_PLAN §5: ``capabilities / health / start /
    stream / cancel / get_status``. Unsupported operations must return an
    explicit ``unsupported`` result rather than falsely claiming success.
    """

    name: str = "base"
    version: str = "0.0.0"

    def execution_admission(self) -> ExecutionAdmission:
        """No launch until this adapter's controls have been established."""
        return ExecutionAdmission()

    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> bool:
        raise NotImplementedError

    def start(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str] = None,
    ) -> AdapterStartResult:
        """Start a call.

        ``gate_token`` is issued by the kernel's consume gate once an approval has
        been atomically consumed and a quota slot reserved. Inert adapters may
        ignore it; an adapter that launches a real process should require it, so
        that reaching a launch without approval is a loud error instead of a
        silent one. Keyword-only and optional for backwards compatibility.
        """
        raise NotImplementedError

    def cancel(self, adapter_invocation_id: str) -> Any:
        raise NotImplementedError

    def get_status(self, adapter_invocation_id: str) -> Any:
        raise NotImplementedError

    # Optional extensions (looked up with getattr; absent means unsupported):
    #
    # * ``start_streaming(invocation_id, snapshot, *, gate_token, emit)`` - same
    #   contract as ``start``, plus ``emit(type, payload)`` for provisional
    #   output while the call runs. ``type`` must be one of
    #   :data:`STREAM_EVENT_TYPES`. Emitting never settles anything: the kernel
    #   still decides from the returned :class:`AdapterStartResult`.
    # * ``supports_native_resume() -> bool`` - True only for code that sends
    #   ``snapshot.resume_thread_id`` to the provider. The Worker refuses a
    #   ``native_resume`` call before consuming its approval otherwise.
    # * ``supports_tool_approval() -> bool`` - True when ``start_streaming``
    #   accepts ``tool_gate`` and routes each provider tool-approval request
    #   through it instead of declining automatically. The gate (kernel-owned,
    #   see :data:`TOOL_APPROVAL_STATUSES`) offers:
    #     ``open(method, request_key, thread_id, turn_id, item_id, details,
    #     available_decisions, deadline) -> card_id | None`` (None: decline),
    #     ``poll(card_id) -> status``,
    #     ``delivered(card_id, decision, ok)`` and ``void_pending()``.
    #   Only ``approved`` may be answered with ``accept``, once, for that exact
    #   request; every other outcome is answered ``decline`` (or not at all
    #   when the request is gone). Approving a run never approves its tools.
    #   ``void(card_id)`` clears a provider-resolved request without closing the
    #   other cards. Offered decisions are request-scoped accept/decline; the
    #   provider's session grants and rule amendments are never auto-substituted.
    # * ``usage_report(invocation_id) -> dict | None`` - this call's usage for
    #   the kernel ledger (T-1a): ``{"mapping", "usage", "source", "notes",
    #   "model"}`` as defined in :mod:`agenthub.usage` (numbers and machine
    #   codes only). ``None`` means the adapter has no record of the call; an
    #   adapter offering this records every call BEFORE launching anything, so
    #   right after its own definitive refusal ``None`` proves nothing ran.
    #   Missing usage is reported as such, never as zero. Without this method
    #   the ledger records ``adapter_reports_no_usage`` (unknown).
    #   Optional ``model_usage`` holds per-model numeric provider fields, never
    #   an extra charge on top of the call total; ``model_source`` distinguishes
    #   thread configuration read-back from provider usage model identity.
    # * ``supports_model_selection`` / ``supports_effort_selection`` - explicit
    #   adapter declarations checked before approval consumption. Options are
    #   sent to the provider API/argv, never inserted as prompt instructions.
    # * ``model_catalog(refresh=False)`` - passive cached metadata. An explicit
    #   user refresh may run a metadata-only process, never a model turn.
    # * ``context_profile() -> dict`` - what the context preview may say about
    #   this adapter (T-1d, :mod:`agenthub.context`): ``adds_prompt_text``
    #   (False only when ``task_input`` is sent verbatim) and ``observed``, a
    #   list of earlier real runs' ``input_total`` under the same
    #   configuration, each with its ``evidence`` and ``binding``. Historical
    #   observations only, shown apart from the estimate. Without this method
    #   the preview marks the adapter's own text as unknown.
    # * ``interrupt_invocation(invocation_id) -> dict`` - ask the provider to
    #   interrupt a turn this process is driving. Returns at least
    #   ``interrupt_requested`` / ``interrupt_accepted``. It NEVER settles or
    #   claims a stop: only the turn's own terminal event (seen by the running
    #   ``start_streaming``) confirms it, reported as ``outcome="canceled"``.
    #
    # Interrupt effective point: the Worker durably closes the call's tool
    # decisions BEFORE it sends ``turn/interrupt`` (also on a tree stop and a
    # local stop). From that commit on, no tool decision is sent to the
    # provider: pending cards become ``void``; decided cards not yet claimed
    # are ``blocked``; new requests are recorded ``void``/``blocked``; no new
    # claim is granted. The adapter must ``claim`` each decision from the gate
    # right before writing it, and may send nothing - not even an automatic
    # decline - that the gate has not allowed. A decision claimed before the
    # closure (``sending``) may still be written afterwards: it is reported as
    # in flight, never claimed as written, and cannot be recalled; ``sent``
    # only means the adapter's write returned.
    #
    # Native child agents: an observed child without trustworthy end evidence
    # (its own state ``completed``/``interrupted``/``errored``/``shutdown``)
    # when the parent turn ends makes the call unconfirmed - ``terminal=False``
    # (kernel: ``unknown``, slot kept) whatever the parent turn reported. The
    # parent turn's actual terminal status is kept separately (execution
    # record ``parent_turn``, ``stream.turn_ended``). Nothing claims a child
    # stopped or counts it against Hub quota.


#: Provisional, adapter-reported stream events. They are display hints only:
#: the authoritative reply is the assistant ``message.created`` written at
#: settlement, and a delta never proves that a turn finished. A delta's text is
#: stored as chat content (``stream_chunks``), never in the event payload; the
#: authenticated event readers attach it server-side.
STREAM_TURN_STARTED = "stream.turn_started"
STREAM_DELTA = "stream.delta"
STREAM_REQUEST_DECLINED = "stream.request_declined"
STREAM_TURN_ENDED = "stream.turn_ended"
#: A provider-native child agent was seen (collab tool call / sub-agent item).
#: Observation only: such children run outside Hub approval and quota.
STREAM_NATIVE_AGENT = "stream.native_agent_observed"
STREAM_EVENT_TYPES = frozenset({
    STREAM_TURN_STARTED, STREAM_DELTA, STREAM_REQUEST_DECLINED, STREAM_TURN_ENDED,
    STREAM_NATIVE_AGENT,
})

#: Lifecycle of one tool approval card (one provider request). ``pending`` is
#: the only non-terminal state; ``approved`` is the only one answered with
#: ``accept``. ``expired`` (time ran out) and ``void`` (the request, turn or
#: transport went away first) never grant anything.
TOOL_APPROVAL_PENDING = "pending"
TOOL_APPROVAL_DETAIL_CHARS = 8000  # a single review field must fit the card's stored preview
TOOL_APPROVAL_APPROVED = "approved"
TOOL_APPROVAL_DECLINED = "declined"
TOOL_APPROVAL_EXPIRED = "expired"
TOOL_APPROVAL_VOID = "void"
TOOL_APPROVAL_STATUSES = frozenset({
    TOOL_APPROVAL_PENDING, TOOL_APPROVAL_APPROVED, TOOL_APPROVAL_DECLINED,
    TOOL_APPROVAL_EXPIRED, TOOL_APPROVAL_VOID,
})
#: What happened to the answer: ``sending`` (claimed; the adapter may be
#: writing it - not proof it was written, and not recallable), ``sent`` (the
#: write returned), ``failed`` (the write failed), ``not_sent`` (nothing left
#: to answer) or ``blocked`` (decided, but tool decisions were closed by an
#: interrupt/stop before it was claimed - never sent).
TOOL_DELIVERY_PENDING = "pending"
TOOL_DELIVERY_SENDING = "sending"
TOOL_DELIVERY_SENT = "sent"
TOOL_DELIVERY_FAILED = "failed"
TOOL_DELIVERY_NOT_SENT = "not_sent"
TOOL_DELIVERY_BLOCKED = "blocked"

#: A native child's own state that counts as trustworthy end evidence
#: (``CollabAgentStatus`` / ``SubAgentActivityKind`` in the local schema).
#: ``notFound`` and a spawn call's own ``completed`` do not.
NATIVE_CHILD_END_STATES = frozenset({"completed", "interrupted", "errored", "shutdown"})
