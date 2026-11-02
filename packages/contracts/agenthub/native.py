"""Native tool bridge contract. Configuration is bound by snapshot.permissions.

No hook can grant authority: only a kernel ToolGate claim can authorize its
exact tool_use_id once. Uncertain deliveries retain native quota reservations.
The initial strict profile deliberately has no shell, MCP, background or resume.
"""
from __future__ import annotations

NATIVE_DISABLED = "disabled"
NATIVE_APPROVED = "approved"
NATIVE_PROFILES = frozenset({NATIVE_DISABLED, NATIVE_APPROVED})
CLAUDE_STRICT_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit", "Agent")
CLAUDE_NATIVE_TYPES = frozenset({"general-purpose"})
CLAUDE_HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure",
                               "SubagentStart", "SubagentStop"})
CLAUDE_PRE_TOOL = "claude/PreToolUse"
MAX_HOOK_BYTES = 256 * 1024
NATIVE_STATUS_EVENT = "native_agent.status"  # ids/state/depth only; no hook body


def native_profile(permissions: dict) -> str:
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be an object")
    value = permissions.get("native_subagents", NATIVE_DISABLED)
    if not isinstance(value, str) or value not in NATIVE_PROFILES:
        raise ValueError("native_subagents must be disabled or approved")
    return value
