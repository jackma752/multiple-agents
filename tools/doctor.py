r"""Read-only "can this machine run the agents?" report.

This is slice 1 of ``docs/p5-agent-onboarding.md``: **discover + preflight**, no
writes, no adoption, no change to any admission decision. It answers, per
channel (``codex`` / ``claude-code``):

* which executables exist on THIS machine, with size, sha256 and ``--version``;
* whether each one is the reviewed constant, an adopted profile entry, or just a
  candidate that nobody has accepted yet;
* whether the environment around it (proxy, CA bundle, CODEX_HOME, config dir,
  work dir, repository containment) is satisfied;
* what to do about each unmet item.

Design constraints (do not relax):

* **Never reads a credential.** ``auth.json`` / ``.credentials.json`` are not
  opened, hashed or listed by content - only their presence may be reported by
  the caller, and this tool does not even do that.
* **Never writes** unless ``--write-check`` is given, which only uses the existing
  ``config.codex_home_write_check()`` (it writes one temp file and removes it).
* **Never executes** an unknown binary except ``--version`` with a short timeout.
* Failures are reported, never repaired.

Usage::

    .\.venv\Scripts\python.exe tools\doctor.py
    .\.venv\Scripts\python.exe tools\doctor.py --channel codex --json
    .\.venv\Scripts\python.exe tools\doctor.py --write-check

Exit code: 0 = every requested channel has no failing check; 1 = at least one
failing check (so a script can gate on it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for _sub in ("packages/contracts", "packages/adapters", "apps/worker"):
    _p = os.path.join(REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

CHANNELS = ("codex", "claude-code")

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

#: Set to a profile JSON path to override the per-machine default location.
ENV_PROFILE = "AGENT_HUB_PROFILE"

_VERSION_TIMEOUT = 5.0
_HASH_CHUNK = 1 << 20


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""

    def as_dict(self) -> dict:
        out = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class Candidate:
    """One executable found on disk, and what we could learn about it."""

    channel: str
    path: str
    source: str
    exists: bool = False
    size: int | None = None
    sha256: str | None = None
    version: str | None = None
    version_error: str = ""
    read_error: str = ""

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "path": self.path,
            "source": self.source,
            "exists": self.exists,
            "size": self.size,
            "sha256": self.sha256,
            "version": self.version,
            "version_error": self.version_error,
            "read_error": self.read_error,
        }


@dataclass
class ChannelReport:
    channel: str
    status: str = "missing"
    transport: str | None = None
    candidate: Candidate | None = None
    candidates: list = field(default_factory=list)
    checks: list = field(default_factory=list)

    def failing(self) -> list:
        return [c for c in self.checks if c.status == FAIL]

    def as_dict(self) -> dict:
        return {
            "channel": self.channel,
            "status": self.status,
            "transport": self.transport,
            "candidate": self.candidate.as_dict() if self.candidate else None,
            "seen": [c.as_dict() for c in self.candidates],
            "checks": [c.as_dict() for c in self.checks],
        }


# --------------------------------------------------------------------------- #
# Small, testable primitives
# --------------------------------------------------------------------------- #

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_version(path: str, timeout: float = _VERSION_TIMEOUT) -> tuple[str | None, str]:
    """Ask the binary for its version. Returns ``(version, error)``; never raises."""
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not raw:
        raw = (proc.stderr or b"").decode("utf-8", "replace").strip()
    first = raw.splitlines()[0].strip() if raw else ""
    if not first:
        return None, f"exit {proc.returncode}, no output"
    return first, ""


def inspect(channel: str, path: str, source: str, *, read_version_too: bool = True) -> Candidate:
    """Fill in a candidate without ever raising."""
    cand = Candidate(channel=channel, path=path, source=source)
    if not path:
        return cand
    try:
        st = os.stat(path)
    except OSError as exc:
        cand.read_error = f"{type(exc).__name__}: {exc}"
        return cand
    cand.exists = True
    cand.size = st.st_size
    try:
        cand.sha256 = sha256_file(path)
    except OSError as exc:
        cand.read_error = f"{type(exc).__name__}: {exc}"
        return cand
    if read_version_too:
        cand.version, cand.version_error = read_version(path)
    return cand


# --------------------------------------------------------------------------- #
# Profile (read-only here; adopted by a later slice, not by this tool)
# --------------------------------------------------------------------------- #

def profile_path(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    explicit = (env.get(ENV_PROFILE) or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    base = env.get("LOCALAPPDATA") or os.path.expanduser("~")
    host = (socket.gethostname() or "unknown").split(".")[0]
    return os.path.join(base, "AgentHub", "profiles", f"{host}.json")


def _inside(path: str, root: str = REPO_ROOT) -> bool:
    p = os.path.abspath(path).lower().rstrip("\\/")
    r = os.path.abspath(root).lower().rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


def load_profile(path: str, *, repo_root: str = REPO_ROOT) -> tuple[dict, str]:
    """Read a profile. Returns ``(data, error)`` - a profile inside the repository is refused."""
    if _inside(path, repo_root):
        return {}, "profile path is inside the repository (refused)"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return {}, "profile is not a JSON object"
    channels = data.get("channels")
    if channels is not None and not isinstance(channels, dict):
        return {}, "profile 'channels' is not an object"
    return data, ""


def adopted_entry(profile: dict, channel: str) -> dict:
    entry = (profile.get("channels") or {}).get(channel)
    return entry if isinstance(entry, dict) else {}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _glob_candidates(pattern: str) -> list:
    import glob

    try:
        return sorted(glob.glob(pattern))
    except OSError:
        return []


def codex_candidates(env: dict | None = None, profile: dict | None = None) -> list:
    """Ordered (path, source) pairs, most authoritative first, duplicates removed."""
    env = os.environ if env is None else env
    profile = profile or {}
    from agenthub_kernel import config

    pairs: list = []
    adopted = adopted_entry(profile, "codex")
    if adopted.get("path"):
        pairs.append((adopted["path"], "profile"))
    if env.get(config.ENV_CODEX_PATH):
        pairs.append((env[config.ENV_CODEX_PATH], "env"))
    pairs.append((config.PINNED_CODEX_PATH, "pinned"))
    try:
        from agenthub_adapters import appserver_adapter

        pairs.append((appserver_adapter.PINNED_APPSERVER["path"], "appserver-pin"))
    except Exception:  # noqa: BLE001 - a missing constant must not break the report
        pass
    local = env.get("LOCALAPPDATA") or os.path.expanduser("~")
    for found in _glob_candidates(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe")):
        pairs.append((found, "installed"))
    for found in _glob_candidates(os.path.join(local, "AgentHub", "runtimes", "codex", "*", "codex.exe")):
        pairs.append((found, "runtime"))
    import shutil

    which = shutil.which("codex")
    if which:
        pairs.append((which, "path"))
    return _dedupe(pairs)


def claude_candidates(env: dict | None = None, profile: dict | None = None) -> list:
    env = os.environ if env is None else env
    profile = profile or {}
    from agenthub_adapters import claude_code

    pairs: list = []
    adopted = adopted_entry(profile, "claude-code")
    if adopted.get("path"):
        pairs.append((adopted["path"], "profile"))
    pairs.append((claude_code.PINNED_CLAUDE_CODE["path"], "pinned"))
    local = env.get("LOCALAPPDATA") or os.path.expanduser("~")
    msix = os.path.join(local, "Packages", "Claude_pzs8sxrjxfjjc", "LocalCache", "Roaming",
                        "Claude", "claude-code", "*", "*", "claude.exe")
    for found in _glob_candidates(msix):
        pairs.append((found, "msix"))
    import shutil

    which = shutil.which("claude")
    if which:
        pairs.append((which, "path"))
    return _dedupe(pairs)


def _dedupe(pairs: list) -> list:
    seen = set()
    out = []
    for path, source in pairs:
        key = os.path.normcase(os.path.abspath(path)) if path else ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((path, source))
    return out


def classify(channel: str, cand: Candidate | None, profile: dict,
             transport: str | None = None) -> str:
    """``adopted`` | ``pinned`` | ``candidate`` | ``missing``.

    ``adopted``/``pinned`` are claims about **agreement**, not approval: a
    candidate becomes adoptable only when its hash matches the profile entry
    (adopted) or the reviewed constant (pinned).

    Codex has **two** reviewed constants - one per transport - so the transport
    decides which one a candidate is compared against.
    """
    if cand is None or not cand.exists or not cand.sha256:
        return "missing"
    entry = adopted_entry(profile, channel)
    if entry.get("sha256") and entry["sha256"] == cand.sha256:
        if entry.get("size") in (None, cand.size):
            return "adopted"
    constant = _pinned_constant(channel, transport)
    if constant.get("sha256") == cand.sha256 and constant.get("size") in (None, cand.size):
        return "pinned"
    return "candidate"


def _pinned_constant(channel: str, transport: str | None = None) -> dict:
    if channel == "codex":
        if transport == "app-server":
            try:
                from agenthub_adapters import appserver_adapter

                pinned = appserver_adapter.PINNED_APPSERVER
                return {"sha256": pinned["sha256"], "size": pinned["size"],
                        "path": pinned["path"], "version": pinned.get("version", "")}
            except Exception:  # noqa: BLE001 - fall back to the exec constant
                pass
        from agenthub_kernel import config

        return {"sha256": config.PINNED_CODEX_SHA256, "size": config.PINNED_CODEX_SIZE,
                "path": config.PINNED_CODEX_PATH, "version": config.PINNED_CODEX_VERSION}
    if channel == "claude-code":
        from agenthub_adapters import claude_code

        return dict(claude_code.PINNED_CLAUDE_CODE)
    return {}


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except (TypeError, ValueError):
        return False


def pick_candidate(channel: str, seen: list, profile: dict,
                   transport: str | None = None, env: dict | None = None) -> Candidate | None:
    """Which executable a real call would actually use, in adapter resolution order.

    adopted profile entry -> env override -> the transport's reviewed constant ->
    the first candidate that exists at all.
    """
    env = env or {}
    preferred: list = []
    entry = adopted_entry(profile, channel)
    if entry.get("path"):
        preferred.append(entry["path"])
    if channel == "codex":
        from agenthub_kernel import config

        if env.get(config.ENV_CODEX_PATH):
            preferred.append(env[config.ENV_CODEX_PATH])
    pinned_path = (_pinned_constant(channel, transport) or {}).get("path")
    if pinned_path:
        preferred.append(pinned_path)
    for wanted in preferred:
        for cand in seen:
            if cand.exists and _same_path(cand.path, wanted):
                return cand
    # Nothing in the resolution order exists. Report the path the adapter would
    # actually try (usually the pinned one) rather than a neighbour that happens
    # to exist: a real call would refuse on that missing path, and saying
    # "candidate" about a different binary hides why.
    if pinned_path:
        for cand in seen:
            if _same_path(cand.path, pinned_path):
                return cand
    for cand in seen:
        if cand.exists:
            return cand
    return seen[0] if seen else None


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def _env_proxy(env: dict) -> str:
    for name in ("AGENT_HUB_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def common_checks(env: dict, *, write_check: bool = False) -> list:
    from agenthub_kernel import config

    checks: list[Check] = []

    checks.append(Check(
        "python_version",
        OK if sys.version_info >= (3, 10) else FAIL,
        "%d.%d.%d" % sys.version_info[:3],
        "" if sys.version_info >= (3, 10) else "use the project venv (.venv\\Scripts\\python.exe)",
    ))

    proxy = _env_proxy(env)
    checks.append(Check(
        "proxy",
        OK if proxy else WARN,
        proxy or "no proxy variable set",
        "" if proxy else "the verified configuration uses http://127.0.0.1:8090",
    ))

    ca = ""
    try:
        ca = os.environ.get("AGENT_HUB_CA_BUNDLE") or config.codex_settings()["ca_bundle"]
    except Exception:  # noqa: BLE001
        ca = ""
    readable = bool(ca) and os.path.isfile(ca) and os.access(ca, os.R_OK)
    checks.append(Check(
        "ca_bundle",
        OK if readable else FAIL,
        ca or "no CA bundle resolved",
        "" if readable else "pip install certifi, or set AGENT_HUB_CA_BUNDLE to a readable PEM",
    ))

    try:
        status = config.codex_home_status()
    except Exception as exc:  # noqa: BLE001
        status = {"codex_home": "", "exists": False, "outside_repository": True, "error": str(exc)}
    checks.append(Check(
        "codex_home",
        OK if status.get("exists") and status.get("outside_repository") else FAIL,
        "%s (exists=%s, outside_repository=%s)" % (
            status.get("codex_home", ""), status.get("exists"), status.get("outside_repository")),
        "" if status.get("exists") and status.get("outside_repository")
        else "set AGENT_HUB_CODEX_HOME to a writable directory outside the repository",
    ))

    if write_check and status.get("exists"):
        ok, detail = config.codex_home_write_check()
        checks.append(Check(
            "codex_home_writable",
            OK if ok else FAIL,
            detail,
            "" if ok else "app-server needs to create its sqlite state here (see docs/issue-appserver-codex-home-sqlite.md)",
        ))
    else:
        checks.append(Check(
            "codex_home_writable",
            SKIP,
            "not tested (add --write-check)",
            "app-server needs a writable CODEX_HOME; the write probe is opt-in",
        ))
    return checks


def resolve_transport(channel: str, env: dict) -> str | None:
    """The transport that will actually be used, not just the one requested."""
    if channel != "codex":
        return None
    from agenthub_kernel import config

    requested = (env.get(config.ENV_CODEX_TRANSPORT) or config.codex_transport() or "").strip()
    try:
        kind = (env.get(config.ENV_ADAPTER) or config.adapter_kind() or "").strip()
    except Exception:  # noqa: BLE001
        kind = ""
    if kind == "multi":
        # adapters.py builds the multi tree with app-server whatever the env says.
        return "app-server"
    return requested or "exec"


def codex_checks(cand: Candidate | None, profile: dict, env: dict,
                 transport: str | None = None) -> list:
    from agenthub_kernel import config

    transport = transport or resolve_transport("codex", env) or "exec"
    checks: list[Check] = []
    ok = cand is not None and cand.exists
    checks.append(Check(
        "executable_present",
        OK if ok else FAIL,
        cand.path if cand else "not found",
        "" if ok else "install Codex, or adopt an existing copy with tools/doctor.py --adopt (later slice)",
    ))

    pinned = _pinned_constant("codex", transport)
    state = classify("codex", cand, profile, transport)
    entry = adopted_entry(profile, "codex")
    if state == "adopted":
        detail = "matches the adopted profile entry (sha256 %s)" % (cand.sha256 or "")[:12]
        status = OK
        hint = ""
    elif state == "pinned":
        detail = "matches the reviewed %s constant %s" % (transport, pinned.get("version") or "")
        status = OK
        hint = ""
    elif state == "candidate":
        detail = "sha256 %s / size %s matches neither the %s pin nor an adopted entry" % (
            (cand.sha256 or "")[:12], cand.size, transport)
        status = FAIL
        hint = "strict mode refuses it until it is adopted; supervised mode needs an explicit source"
    else:
        detail = "no readable executable"
        status = FAIL
        hint = ""
    checks.append(Check("identity", status, detail, hint))

    if state == "adopted":
        checks.append(Check(
            "adoption_record",
            OK,
            "adopted_at=%s by %s" % (entry.get("adopted_at", "?"), entry.get("adopted_by", "?")),
        ))
    elif state == "pinned":
        checks.append(Check(
            "adoption_record",
            SKIP,
            "pinned constant only; strict mode requires a per-machine adoption record",
            "adoption is slice 3 of docs/p5-agent-onboarding.md",
        ))

    try:
        kind = (env.get(config.ENV_ADAPTER) or config.adapter_kind() or "").strip()
    except Exception:  # noqa: BLE001
        kind = ""
    checks.append(Check(
        "adapter_kind",
        OK if kind in ("codex", "multi") else WARN,
        kind or "unset",
        "" if kind in ("codex", "multi")
        else "this shell would build the mock adapter; start the server with "
             "tools/demo/serve.py --adapter codex|multi",
    ))

    requested = (env.get(config.ENV_CODEX_TRANSPORT) or config.codex_transport() or "").strip()
    note = "" if requested == transport else (
        f"requested {requested or 'default'} is overridden: the multi adapter always uses app-server")
    checks.append(Check(
        "transport",
        OK if transport in config.CODEX_TRANSPORTS else FAIL,
        transport if not note else f"{transport}  ({note})",
        "" if transport in config.CODEX_TRANSPORTS else "use exec or app-server",
    ))

    overrides = config.identity_env_overrides()
    checks.append(Check(
        "identity_env_overrides",
        WARN if overrides else OK,
        ", ".join(overrides) if overrides else "none",
        "app-server ignores these and treats them as a block; exec honours them without an audit record"
        if overrides else "",
    ))
    return checks


def claude_checks(cand: Candidate | None, profile: dict, env: dict) -> list:
    from agenthub_kernel import config
    from agenthub_adapters import claude_code

    checks: list[Check] = []
    ok = cand is not None and cand.exists
    checks.append(Check(
        "executable_present",
        OK if ok else FAIL,
        cand.path if cand else "not found",
        "" if ok else "the pinned path is an MSIX package cache; no override exists today "
                      "(see docs/issue-python-low-integrity.md and docs/p5-agent-onboarding.md)",
    ))

    state = classify("claude-code", cand, profile)
    pinned = claude_code.PINNED_CLAUDE_CODE
    if state == "adopted":
        checks.append(Check("identity", OK, "matches the adopted profile entry", ""))
    elif state == "pinned":
        checks.append(Check(
            "identity", OK,
            "matches the reviewed constant %s" % pinned["version"], ""))
    elif state == "candidate":
        checks.append(Check(
            "identity", FAIL,
            "sha256 %s / size %s is neither the reviewed constant nor adopted" % (
                (cand.sha256 or "")[:12], cand.size),
            "there is no path/hash override for this channel yet; that is slice 2",
        ))
    else:
        checks.append(Check("identity", FAIL, "no readable executable", ""))

    checks.append(Check(
        "zero_model_identity_report",
        OK if claude_code.ZERO_MODEL_IDENTITY_REPORT else FAIL,
        "recorded" if claude_code.ZERO_MODEL_IDENTITY_REPORT else "not recorded",
        "" if claude_code.ZERO_MODEL_IDENTITY_REPORT else "supervised admission requires this hash",
    ))

    config_dir = config.claude_config_dir()
    outside = bool(config_dir) and os.path.isabs(config_dir) and not _inside(config_dir)
    checks.append(Check(
        "config_dir",
        OK if outside else FAIL,
        config_dir,
        "" if outside else "must be an absolute path outside the repository",
    ))
    workdir = config.claude_code_workdir_root()
    outside_wd = bool(workdir) and os.path.isabs(workdir) and not _inside(workdir)
    checks.append(Check(
        "workdir_root",
        OK if outside_wd else FAIL,
        workdir,
        "" if outside_wd else "must be an absolute path outside the repository",
    ))
    return checks


def build_report(channels: list, env: dict | None = None, *, profile: dict | None = None,
                 profile_error: str = "", write_check: bool = False) -> dict:
    env = os.environ if env is None else env
    profile = profile or {}
    common = common_checks(env, write_check=write_check)
    reports = []
    for channel in channels:
        rep = ChannelReport(channel=channel)
        if channel == "codex":
            pairs = codex_candidates(env, profile)
            transport = resolve_transport(channel, env)
            rep.transport = transport
            extra = lambda cand, prof, e: codex_checks(cand, prof, e, transport)  # noqa: E731
        elif channel == "claude-code":
            pairs = claude_candidates(env, profile)
            transport = None
            extra = claude_checks
        else:
            raise ValueError(f"unknown channel: {channel}")
        seen = [inspect(channel, path, source) for path, source in pairs]
        rep.candidates = seen
        rep.candidate = pick_candidate(channel, seen, profile, transport, env)
        rep.status = classify(channel, rep.candidate, profile, transport)
        rep.checks = extra(rep.candidate, profile, env)
        reports.append(rep)
    return {
        "host": socket.gethostname(),
        "repo_root": REPO_ROOT,
        "profile": {
            "path": profile_path(env),
            "loaded": bool(profile),
            "error": profile_error,
        },
        "common": [c.as_dict() for c in common],
        "channels": [r.as_dict() for r in reports],
        "ok": all(c.status != FAIL for c in common)
              and all(not [c for c in r["checks"] if c["status"] == FAIL] for r in
                      (r.as_dict() for r in reports)),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

_MARK = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}


def render_text(report: dict) -> str:
    lines = [
        f"host        : {report['host']}",
        f"repository  : {report['repo_root']}",
        f"profile     : {report['profile']['path']}"
        + (f"  ({report['profile']['error']})" if report["profile"]["error"]
           else ("  (loaded)" if report["profile"]["loaded"] else "  (absent)")),
        "",
        "environment",
    ]
    for item in report["common"]:
        lines.append(_row(item))
    for chan in report["channels"]:
        lines.append("")
        head = f"[{chan['channel']}] status = {chan['status']}"
        if chan.get("transport"):
            head += f"   transport = {chan['transport']}"
        lines.append(head)
        cand = chan["candidate"]
        if cand:
            lines.append(f"  path   : {cand['path']}")
            lines.append(f"  source : {cand['source']}   size: {cand['size']}   "
                         f"sha256: {(cand['sha256'] or '-')[:16]}")
            if cand["version"]:
                lines.append(f"  version: {cand['version']}")
            if cand["version_error"]:
                lines.append(f"  version: unknown ({cand['version_error']})")
            if cand["read_error"]:
                lines.append(f"  read   : {cand['read_error']}")
        others = [c for c in chan["seen"]
                  if not (cand and _same_path(c["path"], cand["path"]))]
        if others:
            lines.append(f"  also seen ({len(others)}):")
            for other in others[:5]:
                mark = "exists" if other["exists"] else "missing"
                lines.append(f"    - {other['path']}  [{other['source']}, {mark}]")
        for item in chan["checks"]:
            lines.append(_row(item, prefix="  "))
    lines.append("")
    lines.append("verdict: " + ("all requested channels pass" if report["ok"]
                                else "at least one channel has a FAIL above"))
    return "\n".join(lines)


def _row(item: dict, prefix: str = "") -> str:
    mark = _MARK.get(item["status"], item["status"])
    line = f"{prefix}{mark}  {item['name']:<26} {item['detail']}"
    if item.get("hint"):
        line += f"\n{prefix}        -> {item['hint']}"
    return line


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(channels: list, env: dict | None = None, *, write_check: bool = False) -> dict:
    path = profile_path(env)
    profile, error = load_profile(path)
    return build_report(channels, env, profile=profile, profile_error=error,
                        write_check=write_check)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only agent channel report.")
    parser.add_argument("--channel", action="append", choices=CHANNELS,
                        help="check one channel (repeatable); default: all")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--write-check", action="store_true",
                        help="also probe that CODEX_HOME is writable (writes and removes one temp file)")
    args = parser.parse_args(argv)
    channels = args.channel or list(CHANNELS)
    report = run(channels, write_check=args.write_check)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
