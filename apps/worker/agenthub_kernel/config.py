"""Runtime configuration for the Agent Hub kernel.

The API process and the standalone Worker process must point at the **same**
SQLite file so that a call approved in the API is visible to the Worker and
survives a restart. To make that reliable, the default path is anchored to the
repository root (derived from this file's location) rather than to the current
working directory: launching either process from any directory resolves the
same absolute path.

Resolution order for the database URL:

1. ``AGENT_HUB_DB_URL`` environment variable, if set (full SQLAlchemy URL).
2. ``<repo_root>/var/agent-hub.db`` otherwise.

``var/`` is gitignored (it holds runtime state, never committed).
"""

from __future__ import annotations

import os

# agenthub_kernel/config.py -> agenthub_kernel -> worker -> apps -> repo root
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))

#: Directory for runtime state (SQLite file, logs). Gitignored.
DATA_DIR = os.environ.get("AGENT_HUB_DATA_DIR") or os.path.join(REPO_ROOT, "var")

#: Default database file name.
DB_FILENAME = "agent-hub.db"

#: Environment variable that overrides the whole URL.
ENV_DB_URL = "AGENT_HUB_DB_URL"


def default_db_path() -> str:
    """Absolute path to the default SQLite database file."""
    return os.path.join(DATA_DIR, DB_FILENAME)


def default_db_url() -> str:
    """Database URL shared by the API and the Worker.

    Environment override wins; otherwise a repo-anchored file URL is returned so
    that both processes agree regardless of their working directory.
    """
    override = os.environ.get(ENV_DB_URL)
    if override:
        return override
    path = default_db_path().replace("\\", "/")
    return f"sqlite:///{path}"


def ensure_data_dir() -> str:
    """Create the data directory if needed and return it."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
