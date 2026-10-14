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


# --------------------------------------------------------------------------- #
# Real adapter settings (Codex)
# --------------------------------------------------------------------------- #
#
# Everything here is env-overridable so a deployment pins its own values. The
# defaults match what was actually verified on this machine
# (docs/agent-codex-capabilities.md, docs/codex-proxy-setup.md).

ENV_ADAPTER = "AGENT_HUB_ADAPTER"                 # "mock" (default) or "codex"
ENV_CODEX_PATH = "AGENT_HUB_CODEX_PATH"
ENV_CODEX_SHA256 = "AGENT_HUB_CODEX_SHA256"
ENV_CODEX_SIZE = "AGENT_HUB_CODEX_SIZE"
ENV_CODEX_VERSION = "AGENT_HUB_CODEX_VERSION"
ENV_PROXY = "AGENT_HUB_PROXY"
ENV_CA_BUNDLE = "AGENT_HUB_CA_BUNDLE"
ENV_CODEX_TIMEOUT = "AGENT_HUB_CODEX_TIMEOUT"
#: DEV/TEST ONLY: JSON array prepended to the codex argv, so an offline
#: integration can drive the whole stack with a STUB process instead of Codex.
ENV_CODEX_PREFIX = "AGENT_HUB_CODEX_PREFIX"

#: The executable that passed the verified probe, with its recorded identity.
#: Pinning both means a silently-updated binary is detected before it runs.
PINNED_CODEX_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "OpenAI", "Codex", "bin", "2e5e00daee91c61d", "codex.exe",
)
PINNED_CODEX_SHA256 = "d13914ced6c7af174d8d638db938284231227312104b06a25e9608c632a7daec"
PINNED_CODEX_SIZE = 334121776
PINNED_CODEX_VERSION = "codex-cli 0.162.0-alpha.17.2"


def adapter_kind() -> str:
    """Which adapter the API/Worker should use. Defaults to the inert Mock."""
    return (os.environ.get(ENV_ADAPTER) or "mock").strip().lower()


def codex_home() -> str:
    path = os.path.join(DATA_DIR, "codex-home")
    os.makedirs(path, exist_ok=True)
    return path


def codex_workdir_root() -> str:
    path = os.path.join(DATA_DIR, "codex-runs")
    os.makedirs(path, exist_ok=True)
    return path


def codex_execution_store_path() -> str:
    return os.path.join(DATA_DIR, "codex-executions.db")


def codex_settings() -> dict:
    """Resolved Codex adapter settings (env override wins)."""
    return {
        "codex_path": os.environ.get(ENV_CODEX_PATH) or PINNED_CODEX_PATH,
        "expected_identity": {
            "sha256": os.environ.get(ENV_CODEX_SHA256) or PINNED_CODEX_SHA256,
            "size": int(os.environ.get(ENV_CODEX_SIZE) or PINNED_CODEX_SIZE),
        },
        "version": os.environ.get(ENV_CODEX_VERSION) or PINNED_CODEX_VERSION,
        "codex_home": codex_home(),
        "workdir_root": codex_workdir_root(),
        "store_path": codex_execution_store_path(),
        # The proxy and CA that made the verified probe work. Empty means "no
        # proxy", which is correct only where the endpoints are directly reachable.
        "proxy": os.environ.get(ENV_PROXY) or "http://127.0.0.1:8090",
        "ca_bundle": os.environ.get(ENV_CA_BUNDLE) or _default_ca_bundle(),
        "timeout_seconds": float(os.environ.get(ENV_CODEX_TIMEOUT) or 300),
        "command_prefix": _command_prefix(),
    }


def _command_prefix() -> list:
    """DEV/TEST ONLY argv prefix (JSON array). Empty unless explicitly set."""
    raw = os.environ.get(ENV_CODEX_PREFIX)
    if not raw:
        return []
    import json

    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _default_ca_bundle() -> str:
    """A readable PEM bundle, needed when the proxy intercepts TLS."""
    try:
        import certifi

        return certifi.where()
    except Exception:  # noqa: BLE001
        return ""
