"""Model reservations inside the same writer transaction as call/root admission."""
import hashlib
import json
from sqlalchemy import select, update
from agenthub.budget import BudgetPolicy, BudgetDecision, TreeSpend, decide, settle
from agenthub.context import visible_estimate
from agenthub.models import budget_key
from agenthub_kernel.db import (InvocationRow, TaskRow, UsageLedgerRow, ModelBudgetCounterRow,
                                ModelBudgetReservationRow)
from agenthub_kernel.model_usage import components, provider_of

ACTIVE = ("starting", "running")
SETTLED = ("succeeded", "failed", "canceled", "timed_out", "unknown")


def spend(session, root, agent, provider, model):
    from agenthub_kernel.usage_ledger import ledger_view, not_recorded
    pairs = session.execute(select(InvocationRow, UsageLedgerRow).join(
        TaskRow, TaskRow.id == InvocationRow.task_id).outerjoin(
        UsageLedgerRow, UsageLedgerRow.invocation_id == InvocationRow.id).where(TaskRow.root_task_id == root)).all()
    known = unsettled = reserved = unbounded = running = 0
    for inv, ledger in pairs:
        snap = inv.snapshot or {}
        if snap.get("agent_id") != agent or provider_of(agent) != provider:
            continue
        reservation = session.get(ModelBudgetReservationRow, inv.id)
        belongs = bool(reservation and reservation.provider == provider and reservation.model == model)
        if inv.status in ACTIVE or (reservation and reservation.reserved):
            if belongs:
                reserved += reservation.reserved
            elif not snap.get("model") or snap.get("model") == model:
                unbounded += 1
                running += 1
        elif inv.status in SETTLED:
            view = ledger_view(ledger) if ledger else not_recorded(inv.id, agent)
            if view.get("source") == "not_launched":
                continue
            parts = components(session, [view])
            matching = [v for v in parts if v.get("model") == model]
            unknown = [v for v in parts if not v.get("model")]
            if matching:
                # Each model has at most one component per invocation.
                s = settle(reservation.reservation if belongs else 0, matching[0])
            elif unknown and (belongs or not snap.get("model") or snap.get("model") == model):
                s = settle(reservation.reservation if belongs else 0, None)
            else:
                continue
            known += s.known
            unsettled += s.unsettled
            unbounded += s.unbounded
    return TreeSpend(known, unsettled, reserved, unbounded, running)


def _policy(value):
    return BudgetPolicy(**{"total": None, "per_call": None, "warn_percent": 80, "mode": "strict", **value})


def admit(session, inv, root, policy, *, metered):
    snap = inv.snapshot
    agent = snap["agent_id"]
    provider, model = provider_of(agent), snap.get("model")
    applicable = [(key, val) for key, val in policy.model_budgets.items()
                  if json.loads(key)[:2] == [agent, provider]]
    if provider == 'claude-code' and model in ('opus', 'sonnet', 'haiku') and any(_policy(v).active for _, v in applicable):
        return BudgetDecision(False, "model_budget_needs_resolved_model",
                              message="设置模型预算后请选择完整模型 ID，CLI 别名无法在启动前确定预算归属")
    if not model:
        if any(_policy(v).active and _policy(v).mode == "strict" for _, v in applicable):
            return BudgetDecision(False, "model_budget_model_required", message="该 Agent 设置了模型预算，请先明确选择模型")
        return None
    key = budget_key(agent, provider, model)
    b = _policy(policy.model_budgets.get(key, {}))
    if not b.active:
        return None
    state = spend(session, root, agent, provider, model)
    decision = decide(b, state, estimate=visible_estimate(snap), metered=metered)
    if not decision.allowed:
        return BudgetDecision(False, "model_" + decision.code, message=f"{agent} / {model}：{decision.message}")
    counter_id = hashlib.sha256((root + key).encode()).hexdigest()
    counter = session.get(ModelBudgetCounterRow, counter_id)
    if counter is None:
        counter = ModelBudgetCounterRow(id=counter_id)
        session.add(counter)
    # The kernel holds BEGIN IMMEDIATE. Reconcile actual ledger + live reservations
    # before the conditional update, including calls from before a budget existed.
    for name in ("known", "unsettled", "reserved", "unbounded"):
        setattr(counter, name, getattr(state, name))
    session.flush()
    conditions = [ModelBudgetCounterRow.id == counter_id]
    if b.total is not None:
        conditions.append(ModelBudgetCounterRow.known < b.total)
        if b.mode == "strict":
            conditions.append(ModelBudgetCounterRow.known + ModelBudgetCounterRow.unsettled
                              + ModelBudgetCounterRow.reserved + decision.reserve <= b.total)
    if b.mode == "strict":
        conditions.append(ModelBudgetCounterRow.unbounded == 0)
    changed = session.execute(update(ModelBudgetCounterRow).where(*conditions).values(
        reserved=ModelBudgetCounterRow.reserved + decision.reserve).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        return BudgetDecision(False, "model_token_budget_exceeded", message="模型预算余量已被占用")
    session.add(ModelBudgetReservationRow(invocation_id=inv.id, root_task_id=root, agent_id=agent,
        provider=provider, model=model, reservation=decision.reserve, reserved=decision.reserve))
    session.flush()
    return decision


def release(session, invocation_id):
    row = session.get(ModelBudgetReservationRow, invocation_id)
    inv = session.get(InvocationRow, invocation_id)
    if row and inv and inv.status in SETTLED:
        row.reserved = 0
        session.flush()


def views(session, root, policy, inv=None):
    out = []
    for key, value in policy.model_budgets.items():
        agent, provider, model = json.loads(key)
        b = _policy(value)
        s = spend(session, root, agent, provider, model)
        view = {"key": key, "agent_id": agent, "provider": provider, "model": model, **b.to_dict(),
                "active": b.active, "known": s.known, "unsettled": s.unsettled,
                "reserved": s.reserved, "unbounded": s.unbounded,
                "remaining": b.total - s.charged if b.total is not None else None}
        if inv and inv.snapshot.get("agent_id") == agent and inv.snapshot.get("model") == model:
            view["preview"] = decide(b, s, estimate=visible_estimate(inv.snapshot), metered=True).to_dict()
        out.append(view)
    return out
