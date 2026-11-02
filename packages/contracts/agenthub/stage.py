"""Stage authorization for automatic execution (P4.6, ``docs/p46-autopilot.md``).

A signed-in user starts a *stage*: a goal, done criteria and these rules. The
scheduler then approves, within the rules only, the calls the stage needs and
the tool requests the rules name; every such approval records its stage, step
and rule. The kernel's execution gate still checks each call, and refuses a
stage-approved call once the stage is no longer active.

Nothing here decides alone: :func:`tool_decision` and :func:`parse_verdict`
are pure functions the kernel applies to what the user set.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Persisted stage states; quota waits are polled separately from runnable calls.
STAGE_ACTIVE = "active"
STAGE_REPAIRING = "repairing"
STAGE_WAITING_QUOTA = "waiting_quota"
STAGE_PAUSED = "paused"
STAGE_COMPLETED = "completed"
STAGE_BLOCKED = "blocked"
STAGE_STOPPED = "stopped"
#: States in which the scheduler may approve and start calls.
STAGE_RUNNABLE = (STAGE_ACTIVE, STAGE_REPAIRING)
STAGE_SCHEDULED = (*STAGE_RUNNABLE, STAGE_WAITING_QUOTA)
#: States that still hold the stage (another one cannot start in the tree).
STAGE_OPEN = (STAGE_ACTIVE, STAGE_REPAIRING, STAGE_WAITING_QUOTA, STAGE_PAUSED)
STAGE_ENDED = (STAGE_COMPLETED, STAGE_BLOCKED, STAGE_STOPPED)

PLAN_NONE = "none"        # the main agent does the goal in one call
PLAN_CURRENT = "current"  # the conversation's latest saved plan
PLAN_AGENT = "agent"      # the main agent proposes a plan first
PLAN_MODES = (PLAN_NONE, PLAN_CURRENT, PLAN_AGENT)
#: P6 research pipeline (``agenthub.papers``). Started only by the paper
#: endpoints, never by the generic stage form; ``StageRules.paper`` then holds
#: the research request hash, roles and resource rules, all inside ``rules_hash``.
PLAN_PAPER = "paper"

MAX_STAGE_CALLS = 50
MAX_PREFIXES = 20
MAX_PREFIX_CHARS = 200
MAX_GOAL_CHARS = 4_000

REVIEW_INSTRUCTION = (
    "Review the results above against the stage goal and done criteria below. Answer with a first line that is "
    "exactly 'VERDICT: PASS' or 'VERDICT: FAIL', then the reasons. Do not start any work and do not use tools.\n"
    "Stage goal: {goal}\nDone criteria: {criteria}"
)

#: Words that need to be named in the matching prefix itself (deleting,
#: publishing, merging, deploying, fetching from the network, sending).
SENSITIVE_WORDS = frozenset({
    "rm", "rmdir", "del", "erase", "rd", "remove-item", "push", "merge", "rebase", "reset", "deploy", "publish",
    "curl", "wget", "invoke-webrequest", "invoke-restmethod", "iwr", "irm", "scp", "ssh", "send", "mail",
    "format", "shutdown", "kill", "stop-process", "taskkill",
})
_CHAINING = re.compile(r"[;&|<>`]|\$\(")
_WORD = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ToolRules:
    command_prefixes: tuple = ()
    allow_file_changes: bool = False


@dataclass(frozen=True)
class StageRules:
    agents: tuple
    main_agent: str
    plan: str = PLAN_NONE
    review_agent: Optional[str] = None
    collect_agent: Optional[str] = None
    max_calls: int = 0
    tool: ToolRules = field(default_factory=ToolRules)
    retry_limit: int = 3
    auto_resume: bool = True
    quota_retry_seconds: int = 300
    models: dict = field(default_factory=dict)
    #: Only with ``plan == PLAN_PAPER``; absent otherwise, so older hashes hold.
    paper: Optional[dict] = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if not self.models:
            out.pop("models", None)
        if self.paper is None:
            out.pop("paper", None)
        out["agents"] = list(self.agents)
        out["tool"] = {"command_prefixes": list(self.tool.command_prefixes),
                       "allow_file_changes": self.tool.allow_file_changes}
        return out

    def rules_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)
                              .encode("utf-8")).hexdigest()


def clean_prefixes(lines: Any) -> tuple:
    """Prefixes as typed: trimmed, non-empty, de-duplicated, bounded; ValueError otherwise."""
    if lines is None:
        return ()
    if not isinstance(lines, (list, tuple)):
        raise ValueError("command_prefixes must be a list")
    out: list[str] = []
    for line in lines:
        if not isinstance(line, str):
            raise ValueError("a command prefix must be text")
        if "\n" in line or "\r" in line:
            raise ValueError("one command prefix per entry")
        text = " ".join(line.split())
        if not text:
            continue
        if len(text) > MAX_PREFIX_CHARS or "\0" in text or _CHAINING.search(text):
            raise ValueError(f"command prefix not allowed: {text[:40]!r}")
        if text not in out:
            out.append(text)
    if len(out) > MAX_PREFIXES:
        raise ValueError(f"at most {MAX_PREFIXES} command prefixes")
    return tuple(out)


def make_rules(data: dict[str, Any], *, served: tuple = ()) -> StageRules:
    """Validated rules from the start request, or ValueError."""
    agents = tuple(dict.fromkeys(a for a in data.get("agents") or () if isinstance(a, str) and a))
    if not agents:
        raise ValueError("choose at least one agent")
    if served and set(agents) - set(served):
        raise ValueError(f"unknown agent(s): {sorted(set(agents) - set(served))}")
    plan = data.get("plan") or PLAN_NONE
    if plan not in PLAN_MODES:
        raise ValueError(f"plan must be one of {PLAN_MODES}")
    roles = {"main_agent": data.get("main_agent"), "review_agent": data.get("review_agent") or None,
             "collect_agent": data.get("collect_agent") or None}
    if roles["main_agent"] not in agents:
        raise ValueError("the main agent must be one of the stage's agents")
    for role in ("review_agent", "collect_agent"):
        if roles[role] is not None and roles[role] not in agents:
            raise ValueError(f"the {role.replace('_', ' ')} must be one of the stage's agents")
    if plan != PLAN_NONE and roles["collect_agent"] is None:
        roles["collect_agent"] = roles["main_agent"]
    max_calls = data.get("max_calls", 0)
    if type(max_calls) is not int or not 0 <= max_calls <= MAX_STAGE_CALLS:
        raise ValueError(f"max_calls must be 0..{MAX_STAGE_CALLS} (0 means unlimited)")
    retry = data.get("retry_limit", 3)
    if type(retry) is not int or not 0 <= retry <= 3:
        raise ValueError("retry_limit must be 0..3")
    quota_retry = data.get("quota_retry_seconds", 300)
    if type(quota_retry) is not int or not 60 <= quota_retry <= 3600:
        raise ValueError("quota_retry_seconds must be 60..3600")
    from agenthub.models import validate_selection
    models = data.get("models") or {}
    if not isinstance(models, dict) or set(models) - set(agents):
        raise ValueError("models must be selections for the stage agents")
    for selection in models.values():
        if not isinstance(selection, dict) or set(selection) - {"model", "reasoning_effort"}:
            raise ValueError("invalid stage model selection")
        validate_selection(**selection)
    tool = ToolRules(command_prefixes=clean_prefixes(data.get("command_prefixes")),
                     allow_file_changes=data.get("allow_file_changes") is True)
    return StageRules(agents=agents, plan=plan, max_calls=max_calls, tool=tool, retry_limit=retry,
                      auto_resume=data.get("auto_resume", True) is not False,
                      quota_retry_seconds=quota_retry, models=models, **roles)


def rules_of(stored: dict[str, Any]) -> StageRules:
    tool = stored.get("tool") or {}
    return StageRules(models=stored.get("models") or {}, paper=stored.get("paper"),
                      agents=tuple(stored["agents"]), main_agent=stored["main_agent"], plan=stored["plan"],
                      review_agent=stored.get("review_agent"), collect_agent=stored.get("collect_agent"),
                      max_calls=stored["max_calls"], retry_limit=stored.get("retry_limit", 3),
                      auto_resume=stored.get("auto_resume", True),
                      quota_retry_seconds=stored.get("quota_retry_seconds", 300),
                      tool=ToolRules(command_prefixes=tuple(tool.get("command_prefixes") or ()),
                                     allow_file_changes=bool(tool.get("allow_file_changes"))))


def _effective_command(details: dict[str, Any]) -> Optional[str]:
    """The command the agent asked for, without a PowerShell/cmd wrapper; None if unreadable."""
    view = details.get("command_view") if isinstance(details.get("command_view"), dict) else {}
    if not view.get("parsed"):
        return None
    if isinstance(view.get("inner_command"), str):
        return view["inner_command"].strip()
    argv = view.get("argv")
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
        return " ".join(argv)
    return None


def _prefix_matches(command: str, prefix: str) -> bool:
    if not command.lower().startswith(prefix.lower()):
        return False
    return len(command) == len(prefix) or command[len(prefix)] == " "


def tool_decision(rules: ToolRules, method: str, details: dict[str, Any]) -> tuple[str, str]:
    """``("approved" | "declined", why)`` for one provider tool request.

    Approved only when the rules name it: a command whose effective text
    starts with a listed prefix at a word boundary, has no chaining or
    redirection, and whose sensitive words (if any) appear in that prefix;
    or a file change when ``allow_file_changes``. A request to widen the
    writable root is always declined.
    """
    details = details if isinstance(details, dict) else {}
    if method == "claude/PreToolUse":
        hook = details.get("native_hook") or {}
        if hook.get("tool_name") in ("Write", "Edit") and rules.allow_file_changes:
            return "approved", "file changes allowed; native workspace gate rechecks before delivery"
        return "declined", "this native tool or subagent launch is not in the stage rules"
    if details.get("additionalPermissions") or details.get("networkApprovalContext"):
        return "declined", "additional access is not covered by command prefixes or file changes"
    if details.get("grantRoot"):
        return "declined", "asks to widen the writable root"
    if "fileChange" in method:
        if rules.allow_file_changes:
            return "approved", "file changes allowed"
        return "declined", "file changes are not in the stage rules"
    if "commandExecution" not in method:
        return "declined", f"{method} is not in the stage rules"
    command = _effective_command(details)
    if not command:
        return "declined", "the command cannot be read without guessing"
    if _CHAINING.search(command) or "\n" in command or "\r" in command:
        return "declined", "chaining or redirection is not allowed"
    words = {w.lower() for w in _WORD.findall(command)}
    for prefix in rules.command_prefixes:
        if not _prefix_matches(command, prefix):
            continue
        named = {w.lower() for w in _WORD.findall(prefix)}
        sensitive = (words & SENSITIVE_WORDS) - named
        if sensitive:
            continue  # a later, more specific prefix may explicitly authorize it
        return "approved", f"prefix {prefix!r}"
    return "declined", "no stage rule allows this command"


_VERDICT = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b", re.IGNORECASE)


def parse_verdict(text: str) -> Optional[str]:
    """``PASS`` / ``FAIL`` from a review reply's first non-empty line; None if absent."""
    for line in (text or "").splitlines():
        if line.strip():
            m = _VERDICT.match(line)
            return m.group(1).upper() if m else None
    return None
