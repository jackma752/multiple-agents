"""Workspace identity and access are part of the approved snapshot.permissions."""
import os


def validate_execution_timeout(value):
    """Approved Codex wall-clock seconds: zero disables this deadline."""
    if type(value) is not int or not 0 <= value <= 86400:
        raise ValueError("Codex 执行时限须为 0～86400 秒；0 表示不限时")
    return value


def execution_timeout(snapshot, default):
    if "execution_timeout_seconds" not in snapshot.permissions:
        return default
    return validate_execution_timeout(snapshot.permissions["execution_timeout_seconds"])


def approved_workspace(snapshot):
    value = snapshot.permissions.get("workspace")
    if value is None:
        return None
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isdir(value):
        raise ValueError("approved workspace is unavailable")
    resolved = os.path.realpath(value)
    if os.path.normcase(resolved) != os.path.normcase(os.path.abspath(value)):
        raise ValueError("approved workspace target changed")
    if snapshot.permissions.get("workspace_access") not in ("read-only", "workspace-write"):
        raise ValueError("invalid workspace permission")
    return resolved
