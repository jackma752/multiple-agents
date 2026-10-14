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
ENV_CODEX_HOME = "AGENT_HUB_CODEX_HOME"
ENV_CODEX_SHA256 = "AGENT_HUB_CODEX_SHA256"
ENV_CODEX_SIZE = "AGENT_HUB_CODEX_SIZE"
ENV_CODEX_VERSION = "AGENT_HUB_CODEX_VERSION"
ENV_PROXY = "AGENT_HUB_PROXY"
ENV_CA_BUNDLE = "AGENT_HUB_CA_BUNDLE"
ENV_CODEX_TIMEOUT = "AGENT_HUB_CODEX_TIMEOUT"
#: Sandbox policy for real Codex runs. Defaults to "read-only"; the only other
#: values are Codex's own (`workspace-write`, `danger-full-access`). It exists so a
#: PAIRED CONTROL can run the same prompt against a writable sandbox to show that a
#: read-only refusal is caused by the policy - never to make a probe pass.
ENV_CODEX_SANDBOX = "AGENT_HUB_CODEX_SANDBOX"
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_CODEX_SANDBOX = "read-only"
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
    """Where Codex keeps its own config and, if copied, its login credential.

    Pure path resolution: this function NEVER touches the filesystem, so merely
    reading the settings cannot fail - or create a directory - as a side effect.
    Use :func:`ensure_codex_home` at the point of an actual launch.

    Defaults to a location OUTSIDE the repository (``%LOCALAPPDATA%\\AgentHub``),
    because it can contain a login credential. Nothing under the repo should hold
    one, even in a gitignored directory: a stray ``git add -f``, an archive or a
    backup would leak it. Override with ``AGENT_HUB_CODEX_HOME``.
    """
    override = os.environ.get(ENV_CODEX_HOME)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AgentHub", "codex-home")


def ensure_codex_home() -> str:
    """Create CODEX_HOME and return it; raises RuntimeError if it is unusable.

    Called only where a real Codex process is about to be launched, because
    Codex needs a writable home (it failed with ``os error 5`` when it could not
    write ``$CODEX_HOME/tmp``).
    """
    path = codex_home()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"CODEX_HOME {path!r} is not usable ({exc}). Codex needs a writable "
            "home directory; point AGENT_HUB_CODEX_HOME at one."
        ) from exc
    return path


def credential_location() -> str:
    """Where a copied login credential would live (reported, never read)."""
    return os.path.join(codex_home(), "auth.json")


def _inside(path: str, root: str) -> bool:
    """True when ``path`` is root itself or sits under it."""
    p = os.path.abspath(path).lower().rstrip("\\/")
    r = os.path.abspath(root).lower().rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def codex_home_status() -> dict:
    """Facts about the long-term CODEX_HOME, for the console/tooling.

    Reports presence and permissions ONLY. It never opens ``auth.json``: the
    contents of a credential are not needed to answer "is a login configured?".
    """
    path = codex_home()
    credential = credential_location()
    exists = os.path.isdir(path)
    return {
        "codex_home": path,
        "outside_repository": not _inside(path, REPO_ROOT),
        "exists": exists,
        "writable": os.access(path, os.W_OK) if exists else False,
        "credential": credential,
        "credential_present": os.path.isfile(credential),
        "override_env": ENV_CODEX_HOME if os.environ.get(ENV_CODEX_HOME) else "",
    }



def codex_workdir_root() -> str:
    path = os.path.join(DATA_DIR, "codex-runs")
    os.makedirs(path, exist_ok=True)
    return path


def codex_execution_store_path() -> str:
    return os.path.join(DATA_DIR, "codex-executions.db")


def codex_sandbox() -> str:
    """The sandbox policy for real runs. Unknown values fall back to read-only.

    Falling back (rather than accepting anything) matters: a typo must never widen
    the sandbox.
    """
    value = (os.environ.get(ENV_CODEX_SANDBOX) or "").strip().lower()
    return value if value in CODEX_SANDBOX_MODES else DEFAULT_CODEX_SANDBOX


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
        "sandbox": codex_sandbox(),
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
