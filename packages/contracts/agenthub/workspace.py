"""Workspace identity and access are part of the approved snapshot.permissions."""
import os


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
