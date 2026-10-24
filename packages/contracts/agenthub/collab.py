"""Controlled collaboration plans (P4, minimal).

A plan splits a conversation's work into at most :data:`MAX_PLAN_ITEMS`
sub-tasks, each for one agent, optionally after earlier items. Either the
user writes it, or the main agent proposes it in an ordinary approved call
(:data:`PLAN_INSTRUCTION`) and its reply is read back (:func:`parse_plan_text`)
for the user to edit and save.

Nothing in a plan runs by itself:

* saving a plan needs the tree's collaboration switch
  (``Policy.collaboration_enabled``); with it off nothing is dispatched;
* each item is dispatched by the user as its own sub-task call (depth 1),
  only when every item it depends on has succeeded; the results it depends
  on are sent with it as visible handoff blocks, so its approval shows them;
* every call is approved one by one, and the existing limits apply when it
  runs (depth, total and concurrent sub-agents, budgets);
* a failed item may be dispatched again at most ``MAX_ATTEMPTS_PER_ITEM - 1``
  times;
* when all items are done, their results go back to an agent in one more
  approved call (:data:`COLLECT_INSTRUCTION`).

Sub-tasks never propose plans of their own: the Hub creates depth-1 tasks only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_PLAN_ITEMS = 5
MAX_GOAL_CHARS = 4_000
MAX_ATTEMPTS_PER_ITEM = 2
#: Results one collecting call may carry.
MAX_COLLECT_CHARS = 40_000

PLAN_INSTRUCTION = (
    "Propose a plan to split the task below into at most {max_items} sub-tasks for these agents: {agents}. "
    "Answer with one line per sub-task, exactly in this form: '1. <agent>: <goal>' and, when a sub-task needs "
    "the results of earlier ones, end its line with '(after 1, 2)'. Each goal must be self-contained, because "
    "the sub-task only sees its goal and the results it depends on. Do not start any work and do not use tools."
)
COLLECT_INSTRUCTION = (
    "The sub-tasks of the plan have finished; their results are above. Combine them into one answer: what was "
    "done, what was verified, and anything still open or contradictory between the results."
)


@dataclass(frozen=True)
class PlanItem:
    index: int
    agent_id: str
    goal: str
    depends_on: tuple = ()


def validate_items(items: list[dict[str, Any]]) -> list[PlanItem]:
    """Items as saved, or ValueError. Indexes are 1..n; dependencies point to earlier items."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_PLAN_ITEMS:
        raise ValueError(f"a plan has 1 to {MAX_PLAN_ITEMS} items")
    out = []
    for position, item in enumerate(items, start=1):
        agent, goal = item.get("agent_id"), item.get("goal")
        deps = item.get("depends_on") or []
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 200:
            raise ValueError(f"item {position}: agent_id is required")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
            raise ValueError(f"item {position}: goal is required (at most {MAX_GOAL_CHARS} characters)")
        if not isinstance(deps, (list, tuple)) or not all(type(d) is int and 1 <= d < position for d in deps):
            raise ValueError(f"item {position}: depends_on may only name earlier items")
        out.append(PlanItem(position, agent.strip(), goal.strip(), tuple(sorted(set(deps)))))
    return out


_LINE = re.compile(r"^\s*(?:[-*]\s*)?(\d+)[.)]\s*\[?([A-Za-z0-9_.-]+)\]?\s*[:：]\s*(.+?)\s*$")
_AFTER = re.compile(r"\s*[(（]\s*after\s+([\d,\s]+)[)）]\s*$", re.IGNORECASE)


def parse_plan_text(text: str, agents: tuple = ()) -> list[dict[str, Any]]:
    """Items read back from a proposed plan; lines that do not fit are skipped.

    ``agents``: when given, items for other agents are kept but marked
    ``unknown_agent`` so the user fixes them before saving. Dependencies on
    items that were skipped or are not earlier are dropped.
    """
    raw = []
    for line in (text or "").splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        goal, deps = match.group(3), []
        after = _AFTER.search(goal)
        if after:
            deps = [int(d) for d in re.findall(r"\d+", after.group(1))]
            goal = goal[:after.start()].strip()
        raw.append((int(match.group(1)), match.group(2), goal, deps))
        if len(raw) == MAX_PLAN_ITEMS:
            break
    renumber = {old: new for new, (old, *_rest) in enumerate(raw, start=1)}
    items = []
    for new, (old, agent, goal, deps) in enumerate(raw, start=1):
        kept = sorted({renumber[d] for d in deps if d in renumber and renumber[d] < new})
        item = {"agent_id": agent, "goal": goal, "depends_on": kept}
        if agents and agent not in agents:
            item["unknown_agent"] = True
        items.append(item)
    return items
