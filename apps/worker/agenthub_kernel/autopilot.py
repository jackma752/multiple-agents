"""Automatic execution of a user-authorized stage (P4.6, ``docs/p46-autopilot.md``).

A signed-in user starts a stage (:func:`start_stage`); the :class:`Autopilot`
then moves it on, one :meth:`Autopilot.tick` at a time:

* each step is an ordinary conversation turn (``submit_turn``), created once
  under a fixed request key, with its own snapshot and approval;
* the scheduler approves it only within the stage's rules and records the
  authorization source (:class:`StageGrantRow`); the Worker then claims it
  through the usual execution gate, which refuses it once the stage is no
  longer active;
* tool requests of a stage call are allowed or declined by the stage's tool
  rules (:func:`auto_decide_tool`), never left waiting for a click.

The stage ends with a local summary. Repair counters, quota waits and probe
leases are persisted; unknown executions are never blindly relaunched.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any, Callable, Optional

from sqlalchemy import func, literal_column, select, text
from sqlalchemy.orm import Session

from agenthub.chat import ConversationTurn
from agenthub.collab import COLLECT_INSTRUCTION, MAX_PLAN_ITEMS, PLAN_INSTRUCTION, parse_plan_text, validate_items
from agenthub.contracts import ApprovalStatus, Policy
from agenthub.stage import (
    MAX_GOAL_CHARS, PLAN_AGENT, PLAN_CURRENT, PLAN_NONE, PLAN_PAPER, REVIEW_INSTRUCTION, STAGE_ACTIVE, STAGE_BLOCKED,
    STAGE_COMPLETED, STAGE_OPEN, STAGE_PAUSED, STAGE_RUNNABLE, STAGE_SCHEDULED, STAGE_STOPPED,
    STAGE_REPAIRING, STAGE_WAITING_QUOTA, make_rules, parse_verdict,
    rules_of, tool_decision,
)
from agenthub_kernel.adapters import adapter_binding, adapter_context_profile, adapter_for
from agenthub_kernel.collab import collaboration_enabled, plan_in, plan_view, plans_of, save_plan
from agenthub_kernel.db import (
    ApprovalRow, ChatMessageRow, CollabItemRow, InvocationRow, StageEventRow, StageGrantRow, StageRunRow, TaskRow,
    ToolApprovalRow, UsageLedgerRow, ProviderQuotaRow,
)
from agenthub_kernel.service import PolicyService, QuotaError, new_id, now
from agenthub_kernel.collab import unstarted_approval_closed
from agenthub_kernel.summaries import tree_owner

WAITING = ("draft", "awaiting_approval", "queued", "starting", "running")
#: Refusals at the gate that clear by themselves (try again next tick).
TRANSIENT = ("max_subagents_concurrent", "tree_paused", "not_ready", "worker_stopped", "native_session_busy")

#: What the user needs to do for each way a stage can block (shown in the summary).
NEEDS = {
    "step_failed": "修复重试额度已用完；查看失败条件和重试记录后调整目标或规则",
    "no_result": "该步骤结束但没有可交接的回复：检查 Agent 输出后重新开启阶段",
    "status_unknown": "该步骤的调用状态未知：先经恢复或人工裁决确认它已结束，才能继续",
    "review_failed": "审阅未通过：按审阅意见调整后重新开启阶段",
    "plan_unreadable": "主 Agent 的回复里没有可用的计划行：改用手写计划，或调整拆分要求",
    "plan_outside_rules": "计划用到了规则以外的 Agent：调整计划或把该 Agent 加入规则",
    "max_calls_reached": "已达到本阶段的调用上限：提高上限后开启新阶段",
    "start_refused": "Hub 的限额、预算或策略拒绝了启动：按提示调整设置后开启新阶段（不会擅自提高上限）",
    "scheduler_error": "调度器遇到内部错误：查看错误类型并修复，已有调用记录和进度保留",
}


def served_agents(adapter: Any) -> tuple:
    agents = getattr(adapter, "agents", None)
    if callable(getattr(adapter, "for_agent", None)) and callable(agents):
        return tuple(sorted(agents()))
    return tuple(sorted(getattr(adapter, "AGENT_IDS", ()) or ()))


def _event(session: Session, stage: StageRunRow, kind: str, *, invocation_id: Optional[str] = None,
           tool_approval_id: Optional[str] = None, **detail: Any) -> None:
    session.add(StageEventRow(stage_id=stage.id, kind=kind, invocation_id=invocation_id,
                              tool_approval_id=tool_approval_id, detail=detail))


# --------------------------------------------------------------------------- #
# "proven first", start, views
# --------------------------------------------------------------------------- #


def proven_modes(session: Session, adapter: Any, user_id: Optional[str] = None) -> dict[str, list[str]]:
    """Per agent, the execution modes with a user-approved success on its current binding."""
    out: dict[str, set] = {}
    granted = select(StageGrantRow.approval_id)
    rows = session.execute(
        select(InvocationRow.snapshot).join(ApprovalRow, ApprovalRow.invocation_id == InvocationRow.id)
        .where(InvocationRow.status == "succeeded", ApprovalRow.status == ApprovalStatus.CONSUMED.value,
               ApprovalRow.id.not_in(granted),
               *((ApprovalRow.user_id == user_id,) if user_id else ()))).all()
    for (snap,) in rows:
        agent = (snap or {}).get("agent_id")
        if agent and snap.get("adapter_version") == adapter_binding(adapter, agent):
            out.setdefault(agent, set()).add(snap.get("execution_mode", "strict"))
    return {agent: sorted(modes) for agent, modes in out.items()}


def open_stage(session: Session, root_task_id: str) -> Optional[StageRunRow]:
    return session.scalar(select(StageRunRow).where(StageRunRow.root_task_id == root_task_id,
                                                    StageRunRow.status.in_(STAGE_OPEN)))


def latest_stage(session: Session, cid: str) -> Optional[StageRunRow]:
    return session.scalar(select(StageRunRow).where(StageRunRow.conversation_id == cid)
                          .order_by(StageRunRow.created_at.desc(), literal_column("stage_runs.rowid").desc()).limit(1))


def start_stage(session: Session, cid: str, user_id: str, data: dict[str, Any], *, adapter: Any) -> StageRunRow:
    """The user's one authorization for a stage. Raises ValueError (bad input) or QuotaError."""
    from agenthub_kernel.conversations import owned_conversation

    owned_conversation(session, cid, user_id)
    rules = make_rules(data, served=served_agents(adapter))
    goal = (data.get("goal") or "").strip()
    criteria = (data.get("criteria") or "").strip()
    if not goal or len(goal) > MAX_GOAL_CHARS or len(criteria) > MAX_GOAL_CHARS:
        raise ValueError(f"goal is required; goal and criteria at most {MAX_GOAL_CHARS} characters")
    mode = data.get("execution_mode")
    if mode not in ("strict", "supervised"):
        raise ValueError("execution_mode must be strict or supervised")
    consent = data.get("supervised_consent") is True
    if mode == "supervised" and not consent:
        raise QuotaError("boundary_consent_required", "请明确同意用户监督边界：原生派生与远端停止不保证受控。")
    proven = proven_modes(session, adapter, user_id)
    missing = [a for a in rules.agents if mode not in proven.get(a, [])]
    if missing:
        raise QuotaError("stage_not_proven",
                         f"先在{('用户监督' if mode == 'supervised' else '严格')}模式下手动跑通这些 Agent 各一次：{missing}")

    svc = PolicyService(session)
    owner = tree_owner(session, cid)
    if owner.root_task_id is None:
        owner.root_task_id = svc.create_task(agent_id=rules.main_agent, goal=goal).id
    root = session.get(TaskRow, owner.root_task_id)
    if svc._tree_stopped(root.id):
        raise QuotaError("tree_stopped", "conversation task tree is stopped")
    if open_stage(session, root.id) is not None:
        raise QuotaError("stage_in_progress", "这个任务树已有进行中的阶段：先暂停/停止它，或等它结束")

    plan_id = None
    if rules.plan == PLAN_CURRENT:
        plans = plans_of(session, cid)
        if not plans:
            raise QuotaError("plan_missing", "会话里还没有协作计划：先写计划，或选择不拆分 / 请主 Agent 拆分")
        plan_id = plans[0].id
        outside = sorted({i["agent_id"] for i in plan_view(session, plans[0])["items"]} - set(rules.agents))
        if outside:
            raise QuotaError("plan_outside_rules", f"计划用到了规则以外的 Agent：{outside}")
    if rules.plan != PLAN_NONE and not collaboration_enabled(session, cid):
        # Dispatching sub-tasks is part of what the user authorizes here.
        current = {k: v for k, v in Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})
                   .as_dict().items() if k != "version"}
        svc.update_policy(root.id, Policy(**{**current, "collaboration_enabled": True}), updated_by=user_id)

    stage = StageRunRow(
        id=new_id("stage"), conversation_id=cid, root_task_id=root.id, status=STAGE_ACTIVE,
        execution_mode=mode, goal=goal, criteria=criteria, rules=rules.as_dict(), rules_hash=rules.rules_hash(),
        supervised_consent=consent, granted_by=user_id,
        checkpoint={"phase": "plan" if rules.plan == PLAN_AGENT else "work", "plan_id": plan_id})
    stage.checkpoint = {**stage.checkpoint,
                        "bindings": {a: adapter_binding(adapter, a) for a in rules.agents}}
    session.add(stage)
    session.flush()
    _event(session, stage, "started", rules_hash=stage.rules_hash, granted_by=user_id)
    return stage


def stage_view(session: Session, stage: StageRunRow) -> dict[str, Any]:
    grants = session.scalars(select(StageGrantRow).where(StageGrantRow.stage_id == stage.id)
                             .order_by(StageGrantRow.created_at)).all()
    events = session.scalars(select(StageEventRow).where(StageEventRow.stage_id == stage.id)
                             .order_by(StageEventRow.id.desc()).limit(30)).all()
    summary = stage.summary
    if summary and stage.status in (STAGE_STOPPED, STAGE_BLOCKED):
        # A stop acknowledgement may arrive after the local stage summary was written.
        summary = _summary(session, stage, kind=summary["kind"],
                           **{key: summary.get(key, "") for key in ("step", "reason", "message", "invocation_id")})
    return {
        "stage_id": stage.id, "conversation_id": stage.conversation_id, "root_task_id": stage.root_task_id,
        "status": stage.status, "execution_mode": stage.execution_mode, "goal": stage.goal,
        "criteria": stage.criteria, "rules": stage.rules, "rules_hash": stage.rules_hash,
        "granted_by": stage.granted_by, "checkpoint": stage.checkpoint, "summary": summary,
        "calls_used": len(grants), "created_at": stage.created_at.isoformat() + "Z",
        "steps": [{"step": g.step, "invocation_id": g.invocation_id, "rule": g.rule} for g in grants],
        "events": [{"kind": e.kind, "invocation_id": e.invocation_id, "tool_approval_id": e.tool_approval_id,
                    "detail": e.detail, "at": e.created_at.isoformat() + "Z"} for e in events],
    }


def grant_view(session: Session, approval_id: Optional[str]) -> Optional[dict[str, Any]]:
    """For a call card: the stage rule that approved it, if any."""
    grant = session.get(StageGrantRow, approval_id) if approval_id else None
    if grant is None:
        return None
    return {"kind": "stage", "stage_id": grant.stage_id, "step": grant.step, "rule": grant.rule}


def set_status(session: Session, stage_id: str, user_id: str, action: str) -> StageRunRow:
    """The user pauses, resumes or stops a stage; stopping revokes its unstarted approvals."""
    stage = session.get(StageRunRow, stage_id)
    if stage is None or stage.granted_by != user_id:
        raise QuotaError("stage_not_found", "stage not found")
    if stage.status not in STAGE_OPEN:
        raise QuotaError("stage_ended", f"stage is {stage.status}")
    if action == "pause":
        stage.status = STAGE_PAUSED
    elif action == "resume":
        if stage.status not in (STAGE_PAUSED, STAGE_WAITING_QUOTA):
            raise QuotaError("not_paused", f"stage is {stage.status}")
        stage.status = STAGE_ACTIVE
        stage.checkpoint = {**(stage.checkpoint or {}), "manual_resume": True}
    elif action == "stop":
        _revoke_unstarted(session, stage)
        stage.status = STAGE_STOPPED
        stage.summary = _summary(session, stage, kind="stopped", reason="stopped_by_user")
    else:
        raise ValueError("action must be pause, resume or stop")
    _event(session, stage, action, by=user_id)
    session.flush()
    return stage


def _revoke_unstarted(session: Session, stage: StageRunRow) -> int:
    svc, count = PolicyService(session), 0
    for grant in session.scalars(select(StageGrantRow).where(StageGrantRow.stage_id == stage.id)).all():
        appr = session.get(ApprovalRow, grant.approval_id)
        if appr is not None and appr.status in (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value):
            svc.revoke(appr.id)
            count += 1
    return count


# --------------------------------------------------------------------------- #
# tool requests
# --------------------------------------------------------------------------- #


def auto_decide_tool(db: Any, card_id: str) -> Optional[str]:
    """Allow or decline a stage call's tool request by the stage's tool rules.

    Does not decide cards outside an active stage. Paused stage cards remain
    undeliverable until the stage resumes. Returns the decision, or None.
    """
    from agenthub_kernel.tool_approvals import card_hash, decide

    with db.session() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(ToolApprovalRow, card_id)
        if row is None or row.status != "pending":
            return None
        grant = session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == row.invocation_id))
        stage = session.get(StageRunRow, grant.stage_id) if grant is not None else None
        if stage is None or stage.status not in STAGE_RUNNABLE:
            return None
        decision, why = tool_decision(rules_of(stage.rules).tool, row.method, row.details or {})
        presented, stage_id, invocation_id, method = card_hash(row), stage.id, row.invocation_id, row.method
        try:
            decide(session, card_id, decision=decision, actor=f"stage:{stage_id}", presented_hash=presented,
                   already_locked=True)
        except QuotaError:
            return None  # expired/closed while paused: provider handling settles it
        stage = session.get(StageRunRow, stage_id)
        _event(session, stage, f"tool_{decision}", invocation_id=invocation_id, tool_approval_id=card_id,
               method=method[:80], rule=why[:200])
    return decision


def tool_rule_of(session: Session, card_id: str) -> Optional[dict[str, Any]]:
    """For a tool card: the stage rule that decided it, if any."""
    event = session.scalar(select(StageEventRow).where(StageEventRow.tool_approval_id == card_id)
                           .order_by(StageEventRow.id.desc()).limit(1))
    if event is None:
        return None
    return {"stage_id": event.stage_id, "decision": event.kind.removeprefix("tool_"),
            "rule": (event.detail or {}).get("rule")}


# --------------------------------------------------------------------------- #
# the scheduler
# --------------------------------------------------------------------------- #


class _Blocked(Exception):
    def __init__(self, step: str, reason: str, message: str = "", invocation_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.step, self.reason, self.message, self.invocation_id = step, reason, message, invocation_id


class Autopilot:
    """Moves every running stage on. One :meth:`tick` is one pass; nothing blocks on a call."""

    def __init__(self, db: Any, worker: Any, *, files_root: Callable[[], Optional[str]] = lambda: None,
                 clock: Callable[[], dt.datetime] = now) -> None:
        self.db = db
        self.worker = worker
        self.files_root = files_root
        self.clock = clock
        #: P6 tool dependencies (``papers.pipeline.PaperRuntime``); None: paper tool steps wait.
        self.papers: Any = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._kick = threading.Event()

    # -- background loop ---------------------------------------------------

    def start_background(self, interval: float) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:  # don't put provider content in logs
                    logging.getLogger(__name__).error("autopilot tick failed: %s", type(exc).__name__)
                self._kick.wait(interval)
                self._kick.clear()
        self._thread = threading.Thread(target=loop, name="agenthub-autopilot", daemon=True)
        self._thread.start()

    def kick(self) -> None:
        self._kick.set()

    def stop_background(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._kick.set()
        if self._thread is not None:
            self._thread.join(timeout)

    # -- one pass ------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        with self._lock:  # one pass at a time in this process; the DB lock covers the rest
            with self.db.session() as session:
                ids = session.scalars(select(StageRunRow.id).where(StageRunRow.status.in_(STAGE_SCHEDULED))).all()
                cards = session.scalars(select(ToolApprovalRow.id).join(
                    StageGrantRow, StageGrantRow.invocation_id == ToolApprovalRow.invocation_id).where(
                        ToolApprovalRow.status == "pending")).all()
            for card_id in cards:
                auto_decide_tool(self.db, card_id)
            started, ended = [], []
            for stage_id in ids:
                try:
                    to_start = self._advance_one(stage_id)
                except Exception as exc:
                    # The failed transaction has rolled back. Isolate this stage
                    # so a scheduler bug cannot replay it forever or starve others.
                    with self.db.session() as session:
                        session.execute(text("BEGIN IMMEDIATE"))
                        stage = session.get(StageRunRow, stage_id)
                        if stage is not None and stage.status in STAGE_SCHEDULED:
                            self._end(session, stage, STAGE_BLOCKED, reason="scheduler_error",
                                      step=(stage.checkpoint or {}).get("phase", ""),
                                      message=type(exc).__name__)
                    to_start = []
                for inv_id in to_start:
                    outcome = self._start(stage_id, inv_id)
                    if outcome:
                        started.append(inv_id)
                if self._is_paper(stage_id):
                    # P6 tool steps run outside the stage transaction; a second
                    # pass consumes their results in the same tick.
                    from agenthub_kernel.papers.pipeline import run_tools

                    if run_tools(self, stage_id):
                        try:
                            again = self._advance_one(stage_id)
                        except Exception:  # noqa: BLE001 - the next tick isolates it as above
                            again = []
                        for inv_id in again:
                            if self._start(stage_id, inv_id):
                                started.append(inv_id)
                if self._status(stage_id) not in STAGE_RUNNABLE:
                    ended.append(stage_id)
                if self._status(stage_id) == STAGE_BLOCKED:
                    self.interrupt_stage(stage_id)
            return {"stages": list(ids), "started": started, "ended": ended}

    def interrupt_stage(self, stage_id: str) -> list[dict]:
        """Best-effort stop of this stage only; unsupported stays explicitly unconfirmed."""
        with self.db.session() as session:
            active = list(session.scalars(select(InvocationRow.id).join(
                StageGrantRow, StageGrantRow.invocation_id == InvocationRow.id).where(
                    StageGrantRow.stage_id == stage_id, InvocationRow.status.in_(("starting", "running")))))
        outcomes = []
        for invocation_id in active:
            try:
                outcomes.append(self.worker.interrupt(invocation_id, requested_by=f"stage:{stage_id}", wait_seconds=0))
            except QuotaError as exc:
                outcomes.append({"invocation_id": invocation_id, "reason": exc.code})
        if outcomes:
            with self.db.session() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                stage = session.get(StageRunRow, stage_id)
                self._checkpoint(stage, stop_requests=outcomes)
        return outcomes

    def _status(self, stage_id: str) -> Optional[str]:
        with self.db.session() as session:
            stage = session.get(StageRunRow, stage_id)
            return stage.status if stage is not None else None

    def _is_paper(self, stage_id: str) -> bool:
        with self.db.session() as session:
            stage = session.get(StageRunRow, stage_id)
            return stage is not None and (stage.rules or {}).get("plan") == PLAN_PAPER

    def _advance_one(self, stage_id: str) -> list[str]:
        with self.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            stage = session.get(StageRunRow, stage_id)
            if stage is None or stage.status not in STAGE_SCHEDULED:
                return []
            try:
                result = self._advance(session, stage)
                if stage.status in STAGE_SCHEDULED:
                    waits = (stage.checkpoint or {}).get("waits", {})
                    busy = session.scalar(select(InvocationRow.id).join(
                        StageGrantRow, StageGrantRow.invocation_id == InvocationRow.id).where(
                            StageGrantRow.stage_id == stage.id, InvocationRow.status.in_(WAITING)))
                    stage.status = (STAGE_WAITING_QUOTA if waits and not busy and not result else
                                    STAGE_REPAIRING if (stage.checkpoint or {}).get("repairing") else STAGE_ACTIVE)
                return result
            except _Blocked as blocked:
                self._end(session, stage, STAGE_BLOCKED, step=blocked.step, reason=blocked.reason,
                          message=blocked.message, invocation_id=blocked.invocation_id)
                return []
            except (ValueError, QuotaError) as exc:
                self._end(session, stage, STAGE_BLOCKED, step=(stage.checkpoint or {}).get("phase", ""),
                          reason="start_refused", message=getattr(exc, "code", type(exc).__name__))
                return []

    def _start(self, stage_id: str, invocation_id: str) -> bool:
        result = self.worker.submit(invocation_id)
        if result.get("accepted"):
            return True
        code = result.get("code") or result.get("error") or "not_started"
        if any(code.startswith(t) for t in TRANSIENT):
            return False  # stays approved; the next pass tries again
        with self.db.session() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            stage = session.get(StageRunRow, stage_id)
            if stage is not None and stage.status in STAGE_RUNNABLE:
                grant = session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == invocation_id))
                self._end(session, stage, STAGE_BLOCKED, step=grant.step if grant else "?", reason="start_refused",
                          message=f"{code}: {str(result.get('error') or '')[:300]}", invocation_id=invocation_id)
        return False

    # -- the stage's state machine -------------------------------------------

    @staticmethod
    def _checkpoint(stage: StageRunRow, **changes: Any) -> None:
        stage.checkpoint = {**(stage.checkpoint or {}), **changes}

    def _step(self, stage: StageRunRow, step: str, **changes: Any) -> dict:
        steps = dict((stage.checkpoint or {}).get("steps", {}))
        state = {**steps.get(step, {}), **changes}
        steps[step] = state
        self._checkpoint(stage, steps=steps)
        return state

    def _advance(self, session: Session, stage: StageRunRow) -> list[str]:
        rules = rules_of(stage.rules)
        cp = stage.checkpoint or {}
        to_start = self._unstarted(session, stage)
        if rules.plan == PLAN_PAPER:
            from agenthub_kernel.papers.pipeline import advance as paper_advance

            return paper_advance(self, session, stage, to_start)
        phase = cp.get("phase")
        if phase == "plan":
            inv, state, reply = self._ensure(session, stage, "plan", rules.main_agent,
                PLAN_INSTRUCTION.format(max_items=MAX_PLAN_ITEMS, agents=", ".join(rules.agents))
                + "\n\nTask:\n" + stage.goal, to_start)
            if state != "done":
                return to_start
            items = parse_plan_text(reply.text, rules.agents)
            if not items:
                self._retry(session, stage, "plan", inv, "plan_unreadable")
                return to_start
            if any(i.get("unknown_agent") for i in items):
                raise _Blocked("plan", "plan_outside_rules", invocation_id=inv.id)
            try:
                plan = save_plan(session, stage.conversation_id, validate_items(items), source=f"reply:{reply.id}")
            except ValueError:
                self._retry(session, stage, "plan", inv, "plan_unreadable")
                return to_start
            self._checkpoint(stage, phase="work", plan_id=plan.id)
            _event(session, stage, "plan_saved", invocation_id=inv.id, plan_id=plan.id, items=len(items))
            phase = "work"

        if phase == "work" and rules.plan == PLAN_NONE:
            text_ = stage.goal + (f"\n\nDone criteria: {stage.criteria}" if stage.criteria else "")
            inv, state, reply = self._ensure(session, stage, "main", rules.main_agent, text_, to_start)
            if state != "done":
                return to_start
            phase = "review" if rules.review_agent else ("collect" if rules.collect_agent else "done")
            self._checkpoint(stage, phase=phase, result_message_id=reply.id)
        elif phase == "work":
            plan = plan_in(session, stage.conversation_id, stage.checkpoint["plan_id"])
            view = plan_view(session, plan)
            running = sum(i["state"] in ("awaiting", "running") for i in view["items"])
            states = {i["index"]: i["state"] for i in view["items"]}
            for item in view["items"]:
                step = f"item:{item['index']}"
                if item["state"] == "unconfirmed":
                    raise _Blocked(step, "status_unknown", invocation_id=item["invocation_id"])
                if item["state"] == "done":
                    continue
                if any(states[d] != "done" for d in item["depends_on"]):
                    continue
                if item["state"] not in ("awaiting", "running") and running >= self._policy(session, stage).max_subagents_concurrent:
                    continue
                before = len(to_start)
                row = session.get(CollabItemRow, item["item_id"])
                self._ensure(session, stage, step, item["agent_id"], row.goal, to_start, plan_item_id=row.id)
                running += len(to_start) - before
            if not view["all_done"]:
                return to_start
            phase = "review" if rules.review_agent else "collect"
            self._checkpoint(stage, phase=phase)

        if phase == "repair":
            cp = stage.checkpoint
            extra = self._results(cp)
            if not cp.get("plan_id"):
                extra["handoff_message_ids"] = (*extra.get("handoff_message_ids", ()), cp["review_message_id"])
            else:
                extra["handoff_message_ids"] = (cp["review_message_id"],)
            inv, state, reply = self._ensure(session, stage, f"repair:{cp['review_retries']}", rules.main_agent,
                "Revise the results to address the review above. Explain the cause, change the approach, "
                "and verify the fix. Do not repeat completed operations unnecessarily.\nGoal: " + stage.goal
                + "\nDone criteria: " + stage.criteria, to_start, **extra)
            if state != "done":
                return to_start
            self._checkpoint(stage, phase="review", result_message_id=reply.id, repaired_result=True)
            phase = "review"

        if phase == "review":
            cp = stage.checkpoint
            number = cp.get("review_retries", 0)
            step = "review" if not number else f"review:{number}"
            inv, state, reply = self._ensure(session, stage, step, rules.review_agent,
                REVIEW_INSTRUCTION.format(goal=stage.goal, criteria=stage.criteria or "(none)"),
                to_start, **self._results(cp))
            if state != "done":
                return to_start
            verdict = parse_verdict(reply.text)
            _event(session, stage, "review", invocation_id=inv.id, verdict=verdict or "none")
            if verdict != "PASS":
                attempts = (stage.checkpoint.get("retry_counts") or {}).get("review_cycle", 0)
                if attempts >= rules.retry_limit:
                    raise _Blocked(step, "review_failed", f"Review rejected after {number} repair retries", inv.id)
                self._checkpoint(stage, phase="repair", review_retries=number + 1,
                                 review_message_id=reply.id, repairing=True, review=verdict or "unreadable",
                                 retry_counts={**stage.checkpoint.get("retry_counts", {}), "review_cycle": attempts + 1})
                _event(session, stage, "repair_scheduled", invocation_id=inv.id, step=step, retry=number + 1)
                return to_start
            phase = "collect" if rules.collect_agent else "done"
            self._checkpoint(stage, phase=phase, review="PASS", repairing=False)

        if phase == "collect":
            inv, state, reply = self._ensure(session, stage, "collect", rules.collect_agent,
                COLLECT_INSTRUCTION, to_start, **self._results(stage.checkpoint))
            if state != "done":
                return to_start
            phase = "done"
            self._checkpoint(stage, phase=phase, result_message_id=reply.id)
        if phase == "done":
            self._end(session, stage, STAGE_COMPLETED)
        session.flush()
        return to_start

    @staticmethod
    def _results(cp: dict) -> dict:
        if cp.get("plan_id") and not cp.get("repaired_result"):
            return {"collect_plan_id": cp["plan_id"]}
        return {"handoff_message_ids": (cp["result_message_id"],)}

    def _policy(self, session: Session, stage: StageRunRow) -> Policy:
        root = session.get(TaskRow, stage.root_task_id)
        return Policy(**{k: v for k, v in (root.policy or {}).items() if k != "version"})

    def _unstarted(self, session: Session, stage: StageRunRow) -> list[str]:
        return list(session.scalars(select(StageGrantRow.invocation_id).join(
            ApprovalRow, ApprovalRow.id == StageGrantRow.approval_id).where(
                StageGrantRow.stage_id == stage.id, ApprovalRow.status == ApprovalStatus.APPROVED.value)))

    def _payload(self, inv: InvocationRow) -> dict:
        adapter = adapter_for(self.worker.adapter, inv.snapshot.get("agent_id"))
        record = adapter.lookup_by_invocation(inv.id)
        return (record or {}).get("payload") or {}

    def _retry(self, session: Session, stage: StageRunRow, step: str, inv: InvocationRow, reason: str) -> None:
        state = self._step(stage, step)
        logical = "review_cycle" if step.startswith(("review", "repair:")) else step
        retries = (stage.checkpoint.get("retry_counts") or {}).get(logical, 0)
        if retries >= rules_of(stage.rules).retry_limit:
            raise _Blocked(step, reason, f"Initial attempt plus {retries} repair retries exhausted", inv.id)
        history = [*state.get("failures", []), {"invocation_id": inv.id, "reason": reason,
                    "agent": inv.snapshot.get("agent_id"), "attempt": state.get("attempt", 0) + 1}]
        self._step(stage, step, retries=retries + 1, attempt=state.get("attempt", 0) + 1,
                   failures=history, previous_invocation_id=inv.id, invocation_id=None, retry_reason=reason,
                   resume_after_quota=False)
        self._checkpoint(stage, repairing=True,
                         retry_counts={**stage.checkpoint.get("retry_counts", {}), logical: retries + 1})
        _event(session, stage, "retry_scheduled", invocation_id=inv.id, step=step, retry=retries + 1, reason=reason)

    def _record_quota(self, session: Session, inv: InvocationRow, quota: dict) -> ProviderQuotaRow:
        bucket = inv.snapshot["agent_id"]  # one credential context per provider in this Hub instance
        row = session.get(ProviderQuotaRow, bucket)
        if row is not None and row.last_failure_id == inv.id:
            return row
        grant = session.scalar(select(StageGrantRow).where(StageGrantRow.invocation_id == inv.id))
        stage = session.get(StageRunRow, grant.stage_id)
        initial = rules_of(stage.rules).quota_retry_seconds
        backoff = min((row.backoff_seconds * 2) if row else initial, 3600)
        reset = dt.datetime.fromisoformat(quota["reset_at"].removesuffix("Z")) if quota.get("reset_at") else None
        next_check = max(self.clock() + dt.timedelta(seconds=1), reset) if reset else self.clock() + dt.timedelta(seconds=backoff)
        if row is None:
            row = ProviderQuotaRow(bucket=bucket)
            session.add(row)
        row.next_check_at, row.reset_at = next_check, reset
        row.backoff_seconds, row.last_failure_id, row.probe_invocation_id = backoff, inv.id, None
        session.flush()
        return row

    def _quota_ready(self, session: Session, stage: StageRunRow, step: str, agent: str) -> bool:
        row = session.get(ProviderQuotaRow, agent)
        if row is not None and row.probe_invocation_id:
            probe = session.get(InvocationRow, row.probe_invocation_id)
            if probe and probe.status == "unknown":
                raise _Blocked(step, "status_unknown", "Quota probe outcome is unknown; no duplicate probe", probe.id)
            if probe and probe.status in ("draft", "awaiting_approval"):
                # A probe that never started cannot pin the bucket forever. When its
                # approval is gone (revoked, declined or expired by a policy change)
                # the call provably never ran, so the lease is released and a fresh
                # probe may be created. ``pending``/``approved`` still may run and
                # keep the lease: an unstarted call is never duplicated.
                approval = session.scalar(
                    select(ApprovalRow).where(ApprovalRow.invocation_id == probe.id))
                if approval is None or approval.status not in ("pending", "approved"):
                    _event(session, stage, "quota_probe_stranded", invocation_id=probe.id, step=step,
                           agent=agent, approval_status=(approval.status if approval else None))
                    row.probe_invocation_id = None
                    session.flush()
                    probe = None
            if probe and probe.status not in WAITING:
                payload = self._payload(probe)
                if probe.status == "failed" and payload.get("provider_quota"):
                    row = self._record_quota(session, probe, payload["provider_quota"])
                elif probe.status in ("revoked", "invalid", "canceled"):
                    row.probe_invocation_id = None
                else:
                    session.delete(row)
                    session.flush()
                    row = None
        waits = dict((stage.checkpoint or {}).get("waits", {}))
        rules = rules_of(stage.rules)
        if row is None:
            if step in waits and not rules.auto_resume and not stage.checkpoint.get("manual_resume"):
                return False  # another stage recovering the bucket does not authorize this stage's resume
            waits.pop(step, None)
            self._checkpoint(stage, waits=waits)
            return True
        ready = (not row.probe_invocation_id and row.next_check_at <= self.clock()
                 and (rules.auto_resume or stage.checkpoint.get("manual_resume")))
        if ready:
            waits.pop(step, None)
        else:
            waits[step] = {"agent": agent, "next_check_at": row.next_check_at.isoformat() + "Z",
                           "reset_at": row.reset_at.isoformat() + "Z" if row.reset_at else None,
                           "probe_invocation_id": row.probe_invocation_id,
                           "auto_resume": rules.auto_resume}
        self._checkpoint(stage, waits=waits)
        return ready

    def _ensure(self, session: Session, stage: StageRunRow, step: str, agent_id: str, text_: str,
                to_start: list[str], **extra: Any) -> tuple[Optional[InvocationRow], str, Optional[ChatMessageRow]]:
        state = self._step(stage, step)
        key = f"{stage.id}:{step}" + (f":{state['attempt']}" if state.get("attempt") else "")
        message = session.scalar(select(ChatMessageRow).where(
            ChatMessageRow.conversation_id == stage.conversation_id, ChatMessageRow.client_request_id == key))
        inv = session.get(InvocationRow, message.invocation_id) if message else None
        if inv:
            if unstarted_approval_closed(session, inv):
                raise _Blocked(step, "start_refused", "Unstarted approval is closed; authorize a new stage", inv.id)
            if inv.status in WAITING:
                return inv, "waiting", None
            if inv.status == "unknown":
                raise _Blocked(step, "status_unknown", invocation_id=inv.id)
            if inv.status in ("canceled", "revoked", "invalid"):
                raise _Blocked(step, "start_refused", f"Invocation {inv.status}; not retried automatically", inv.id)
            reply = session.scalar(select(ChatMessageRow).where(ChatMessageRow.invocation_id == inv.id,
                                                                ChatMessageRow.role == "assistant"))
            if inv.status == "succeeded" and reply is not None:
                self._quota_ready(session, stage, step, agent_id)
                return inv, "done", reply
            payload = self._payload(inv)
            quota = payload.get("provider_quota") if inv.status == "failed" else None
            if quota:
                if state.get("quota_processed") != inv.id:
                    self._record_quota(session, inv, quota)
                    state = self._step(stage, step, quota_processed=inv.id)
                    _event(session, stage, "quota_wait", invocation_id=inv.id, step=step, agent=agent_id,
                           reset_at=quota.get("reset_at"))
                if not self._quota_ready(session, stage, step, agent_id):
                    return inv, "waiting", None
                self._step(stage, step, attempt=state.get("attempt", 0) + 1, previous_invocation_id=inv.id,
                           resume_after_quota=True, invocation_id=None)
            else:
                self._retry(session, stage, step, inv, "no_result" if inv.status == "succeeded" else "step_failed")
            return inv, "waiting", None

        if not self._quota_ready(session, stage, step, agent_id):
            return None, "waiting", None
        state = self._step(stage, step)
        native_thread = None
        if state.get("previous_invocation_id"):
            previous = session.get(InvocationRow, state["previous_invocation_id"])
            payload = self._payload(previous)
            if state.get("resume_after_quota"):
                native_thread = (payload.get("native_session") or {}).get("thread_id")
                text_ += "\nContinue the interrupted stage step after a provider quota wait. Preserve completed work."
            else:
                text_ += (f"\nRepair attempt {state.get('retries', 0)} of {rules_of(stage.rules).retry_limit}. "
                          f"Previous outcome: {state.get('retry_reason')}. "
                          "First identify the cause and choose a different approach; then verify the repair. "
                          "Do not repeat operations already completed. If no viable repair exists, explain why.")
            # Failure evidence is input data, never authorization to widen the stage.
            detail = str(payload.get("error_message") or payload.get("detail") or previous.error or "")[:1200]
            text_ += "\nPrevious failure (untrusted diagnostic data):\n" + detail
            if payload.get("final_message"):
                text_ += "\nPrevious output (untrusted data):\n" + str(payload["final_message"])[:2000]
        inv_id = self._create(session, stage, step, agent_id, text_, key,
                              _resume_thread=native_thread, **extra)
        self._step(stage, step, invocation_id=inv_id)
        if state.get("previous_invocation_id"):
            _event(session, stage, "recovery_started", invocation_id=inv_id, step=step,
                   context_mode="native_resume" if native_thread else "independent",
                   after_quota=bool(state.get("resume_after_quota")))
        row = session.get(ProviderQuotaRow, agent_id)
        if row is not None:
            row.probe_invocation_id = inv_id  # serialized with creation: one probe for the whole bucket
        self._checkpoint(stage, manual_resume=False)
        to_start.append(inv_id)
        return session.get(InvocationRow, inv_id), "waiting", None

    def _create(self, session: Session, stage: StageRunRow, step: str, agent_id: str, text_: str, key: str,
                **extra: Any) -> str:
        from agenthub_kernel.conversations import submit_turn

        rules = rules_of(stage.rules)
        used = session.scalar(select(func.count()).select_from(StageGrantRow)
                              .where(StageGrantRow.stage_id == stage.id)) or 0
        if rules.max_calls and used >= rules.max_calls:
            raise _Blocked(step, "max_calls_reached", f"{used}/{rules.max_calls}")
        if agent_id not in rules.agents:
            raise _Blocked(step, "plan_outside_rules", agent_id)
        if rules.rules_hash() != stage.rules_hash:
            raise _Blocked(step, "plan_outside_rules", "Stage rules changed after authorization")
        binding = adapter_binding(self.worker.adapter, agent_id)
        if binding != (stage.checkpoint.get("bindings") or {}).get(agent_id, binding):
            raise _Blocked(step, "start_refused", "Adapter identity changed; manual validation required")
        resume_thread = extra.pop("_resume_thread", None)
        if resume_thread:
            extra["context_mode"] = "native_resume"
        request = ConversationTurn(text=text_, agent_id=agent_id, client_request_id=key,
                                   execution_mode=stage.execution_mode, **rules.models.get(agent_id, {}), **extra)
        adapter = self.worker.adapter
        try:
            turn = submit_turn(session, stage.conversation_id, stage.granted_by, request,
                               adapter_version=adapter_binding(adapter, agent_id),
                               context_profile=adapter_context_profile(adapter, agent_id),
                               files_root=self.files_root(), stage_dispatch_limit=rules.max_calls,
                               stage_resume_thread_id=resume_thread)
        except QuotaError as exc:
            raise _Blocked(step, "start_refused", f"{exc.code}: {exc.message[:300]}")
        inv = session.get(InvocationRow, turn["invocation_id"])
        appr = session.get(ApprovalRow, turn["approval_id"])
        # Within the rules: the stage's agent, its execution boundary, Hub depth only.
        if (inv.snapshot.get("agent_id") not in rules.agents
                or inv.snapshot.get("execution_mode", "strict") != stage.execution_mode
                or int(inv.snapshot.get("depth") or 0) > 1
                or any(inv.snapshot.get(k) != rules.models.get(agent_id, {}).get(k)
                       for k in ("model", "reasoning_effort"))):
            PolicyService(session).revoke(appr.id)  # never left approvable by anyone
            raise _Blocked(step, "plan_outside_rules", "call outside the stage rules", inv.id)
        appr.expires_at = stage.expires_at  # stage lifetime, not the manual approval TTL
        PolicyService(session).approve(appr.id, supervised_consent=stage.supervised_consent)
        rule = f"stage {stage.rules_hash[:12]} step {step}"
        session.add(StageGrantRow(approval_id=appr.id, invocation_id=inv.id, stage_id=stage.id, step=step,
                                  rule=rule))
        _event(session, stage, "auto_approved", invocation_id=inv.id, step=step, agent=agent_id)
        session.flush()
        return inv.id

    # -- the end ---------------------------------------------------------------

    def _end(self, session: Session, stage: StageRunRow, status: str, *, step: str = "", reason: str = "",
             message: str = "", invocation_id: Optional[str] = None) -> None:
        if status != STAGE_COMPLETED:
            _revoke_unstarted(session, stage)
        stage.status = status
        kind = "completed" if status == STAGE_COMPLETED else "blocked"
        stage.summary = _summary(session, stage, kind=kind, step=step, reason=reason, message=message,
                                 invocation_id=invocation_id)
        _event(session, stage, kind, invocation_id=invocation_id, step=step, reason=reason)
        session.flush()


def _summary(session: Session, stage: StageRunRow, *, kind: str, step: str = "", reason: str = "",
             message: str = "", invocation_id: Optional[str] = None) -> dict[str, Any]:
    """Written locally from the records (no model call)."""
    steps, totals = [], {"input": 0, "output": 0, "unknown": 0}
    for grant in session.scalars(select(StageGrantRow).where(StageGrantRow.stage_id == stage.id)
                                 .order_by(StageGrantRow.created_at)).all():
        inv = session.get(InvocationRow, grant.invocation_id)
        reply = session.scalar(select(ChatMessageRow.id).where(ChatMessageRow.invocation_id == inv.id,
                                                               ChatMessageRow.role == "assistant"))
        ledger = session.get(UsageLedgerRow, inv.id)
        if ledger is not None and ledger.input_total is not None:
            totals["input"] += ledger.input_total
            totals["output"] += ledger.output_total or 0
        elif inv.status not in ("draft", "awaiting_approval", "queued", "revoked", "invalid"):
            totals["unknown"] += 1
        steps.append({"step": grant.step, "agent": inv.snapshot.get("agent_id"), "status": inv.status,
                      "invocation_id": inv.id, "reply_message_id": reply,
                      "execution_mode": inv.snapshot.get("execution_mode", "strict"),
                      "context_mode": inv.snapshot.get("context_mode", "independent")})
    cp = stage.checkpoint or {}
    out = {"kind": kind, "goal": stage.goal, "criteria": stage.criteria, "steps": steps, "calls": len(steps),
           "usage": totals, "review": cp.get("review"), "result_message_id": cp.get("result_message_id"),
           "plan_id": cp.get("plan_id")}
    out["retry_counts"] = cp.get("retry_counts", {})
    out["failures"] = {key: value.get("failures", []) for key, value in cp.get("steps", {}).items()
                       if value.get("failures")}
    out["quota_waits"] = cp.get("waits", {})
    if kind == "completed":
        out["next"] = "本阶段已结束；需要继续时由你开启下一阶段（不会自动开始）"
    else:
        out.update(step=step, reason=reason, message=message, invocation_id=invocation_id,
                   needs=NEEDS.get(reason, "查看记录后调整规则或目标，再开启新阶段" if kind != "stopped"
                                   else "已由你停止；需要时重新开启阶段"),
                   done=[s["step"] for s in steps if s["status"] == "succeeded" and s["step"] != step])
    return out
