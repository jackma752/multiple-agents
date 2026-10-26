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
#: Optional Codex transport: "exec" (default) or "app-server". Choosing a
#: transport grants nothing; admission is decided by reviewed adapter code.
ENV_CODEX_TRANSPORT = "AGENT_HUB_CODEX_TRANSPORT"
CODEX_TRANSPORTS = ("exec", "app-server")
#: DEV/TEST ONLY: run the app-server transport against the bundled stdio stub
#: with this scenario instead of Codex (tools/demo/stub_appserver.py).
ENV_APPSERVER_STUB = "AGENT_HUB_APPSERVER_STUB"

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
    It also never writes, so calling it has no side effects - use
    :func:`codex_home_write_check` when an actual write test is needed.
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


def codex_home_write_check(path: str | None = None) -> tuple[bool, str]:
    """Can THIS process actually write inside CODEX_HOME?

    ``os.access`` is unreliable on Windows and does not see a sandbox policy, and
    the distinction matters: Codex has previously failed with ``os error 5`` when
    it could not write ``$CODEX_HOME/tmp``. A caller about to spend a real call
    should ask first, so a denied directory is reported as "not writable by this
    process" instead of being mistaken for a missing credential or a Codex bug.

    Writes one tiny file and removes it. Returns ``(ok, detail)``.
    """
    import tempfile

    path = os.fspath(path) if path is not None else codex_home()
    if not os.path.isdir(path):
        return False, f"{path} does not exist"
    try:
        # Exclusive random name: never truncate a pre-existing file and never
        # collide with another worker's preflight. Check the selected home,
        # not a different path obtained from an inherited environment override.
        with tempfile.NamedTemporaryFile(prefix=".agenthub-write-probe-", dir=path) as fh:
            fh.write(b"ok")
            fh.flush()
    except OSError as exc:
        return False, f"{path} is not writable by this process ({exc})"
    return True, f"{path} is writable by this process"



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


#: Background threads that drive approved calls after ``POST /run`` returns.
ENV_WORKER_THREADS = "AGENT_HUB_WORKER_THREADS"
#: Seconds a tool approval card stays answerable before it expires (= decline).
ENV_TOOL_APPROVAL_TTL = "AGENT_HUB_TOOL_APPROVAL_TTL"


def _bounded_number(name: str, default: float, low: float, high: float) -> float:
    """An env number clamped to [low, high]; junk falls back to the default."""
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def worker_threads() -> int:
    return int(_bounded_number(ENV_WORKER_THREADS, 4, 1, 32))


def tool_approval_ttl() -> float:
    return _bounded_number(ENV_TOOL_APPROVAL_TTL, 120, 1, 600)


#: The only directory tree whose files a message may reference (T-1e). Unset:
#: file references are refused.
ENV_FILES_ROOT = "AGENT_HUB_FILES_ROOT"


def files_root() -> str | None:
    value = (os.environ.get(ENV_FILES_ROOT) or "").strip()
    return os.path.abspath(value) if value else None


def codex_transport() -> str:
    """Selected Codex transport; anything unrecognised falls back to exec."""
    value = (os.environ.get(ENV_CODEX_TRANSPORT) or "").strip().lower()
    return value if value in CODEX_TRANSPORTS else "exec"


def appserver_execution_store_path() -> str:
    return os.path.join(DATA_DIR, "appserver-executions.db")


def identity_env_overrides() -> tuple[str, ...]:
    """Identity variables that are set. exec honours them; app-server never does.

    The app-server identity is a reviewed code constant
    (``agenthub_adapters.appserver_adapter.PINNED_APPSERVER``); any of these
    being set blocks its supervised admission instead of being silently ignored.
    """
    names = (ENV_CODEX_PATH, ENV_CODEX_SHA256, ENV_CODEX_SIZE, ENV_CODEX_VERSION)
    return tuple(name for name in names if os.environ.get(name))


def codex_settings() -> dict:
    """Resolved Codex adapter settings (env override wins, exec only)."""
    return {
        "identity_env_overrides": identity_env_overrides(),
        "transport": codex_transport(),
        "appserver_stub": (os.environ.get(ENV_APPSERVER_STUB) or "").strip(),
        "appserver_store_path": appserver_execution_store_path(),
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


# --------------------------------------------------------------------------- #
# Claude Code adapter settings
# --------------------------------------------------------------------------- #
#
# The executable identity is a reviewed code constant
# (``agenthub_adapters.claude_code.PINNED_CLAUDE_CODE``); no variable overrides it.

#: DEV/TEST ONLY: run against tools/demo/stub_claude_code.py with this scenario.
ENV_CLAUDE_CODE_STUB = "AGENT_HUB_CLAUDE_CODE_STUB"
ENV_CLAUDE_CODE_TIMEOUT = "AGENT_HUB_CLAUDE_CODE_TIMEOUT"
#: Dedicated CLAUDE_CONFIG_DIR for the child (its own login; never the user's ~/.claude).
ENV_CLAUDE_CONFIG_DIR = "AGENT_HUB_CLAUDE_CONFIG_DIR"
ENV_CLAUDE_CODE_WORKDIR = "AGENT_HUB_CLAUDE_CODE_WORKDIR"


def _local_base() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")


def claude_config_dir() -> str:
    """Pure path resolution (no side effects), outside the repository by default.

    The user logs in there once in their own terminal; the Hub only checks that
    the login file is present and never reads it.
    """
    return os.environ.get(ENV_CLAUDE_CONFIG_DIR) or os.path.join(_local_base(), "AgentHub", "claude-config")


def claude_code_workdir_root() -> str:
    """Per-call empty work directories, outside the repository by default."""
    return os.environ.get(ENV_CLAUDE_CODE_WORKDIR) or os.path.join(_local_base(), "AgentHub", "claude-code-runs")


def claude_code_settings() -> dict:
    return {
        "stub": (os.environ.get(ENV_CLAUDE_CODE_STUB) or "").strip(),
        "config_dir": claude_config_dir(),
        "workdir_root": claude_code_workdir_root(),
        "store_path": os.path.join(DATA_DIR, "claude-code-executions.db"),
        "proxy": os.environ.get(ENV_PROXY) or "http://127.0.0.1:8090",
        "ca_bundle": os.environ.get(ENV_CA_BUNDLE) or _default_ca_bundle(),
        "timeout_seconds": _bounded_number(ENV_CLAUDE_CODE_TIMEOUT, 180, 1, 3600),
    }


# --------------------------------------------------------------------------- #
# DeepSeek chat-completions channel (P5)
# --------------------------------------------------------------------------- #
#: Directory holding ``credentials.json``; outside the repository by default.
ENV_DEEPSEEK_HOME = "AGENT_HUB_DEEPSEEK_HOME"
#: OpenAI-compatible base URL (no trailing ``/v1``; the adapter appends the path).
ENV_DEEPSEEK_BASE_URL = "AGENT_HUB_DEEPSEEK_BASE_URL"
ENV_DEEPSEEK_MODEL = "AGENT_HUB_DEEPSEEK_MODEL"
ENV_DEEPSEEK_CA = "AGENT_HUB_DEEPSEEK_CA"
ENV_DEEPSEEK_TIMEOUT = "AGENT_HUB_DEEPSEEK_TIMEOUT"
#: DEV/TEST ONLY: serve canned replies instead of calling the network.
ENV_DEEPSEEK_STUB = "AGENT_HUB_DEEPSEEK_STUB"

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


def deepseek_home() -> str:
    """Directory of the DeepSeek credential, resolved without touching disk.

    Same reasoning as :func:`codex_home`: a credential must not live under the
    repository, where an archive or a ``git add -f`` could copy it out.
    """
    return os.environ.get(ENV_DEEPSEEK_HOME) or os.path.join(_local_base(), "AgentHub", "deepseek")


def deepseek_credentials_path() -> str:
    return os.path.join(deepseek_home(), "credentials.json")


def deepseek_settings() -> dict:
    return {
        "home": deepseek_home(),
        "credentials_path": deepseek_credentials_path(),
        "base_url": (os.environ.get(ENV_DEEPSEEK_BASE_URL) or DEFAULT_DEEPSEEK_BASE_URL).strip(),
        "model": (os.environ.get(ENV_DEEPSEEK_MODEL) or DEFAULT_DEEPSEEK_MODEL).strip(),
        "stub": (os.environ.get(ENV_DEEPSEEK_STUB) or "").strip(),
        "store_path": os.path.join(DATA_DIR, "deepseek-executions.db"),
        "proxy": os.environ.get(ENV_PROXY) or "http://127.0.0.1:8090",
        "ca_bundle": os.environ.get(ENV_DEEPSEEK_CA) or _default_ca_bundle(),
        "timeout_seconds": _bounded_number(ENV_DEEPSEEK_TIMEOUT, 120, 1, 3600),
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
