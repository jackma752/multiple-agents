"""Repository hygiene checks required by AGENT_HUB_PLAN §6 bullet 10.

"日志和仓库不含真实密钥" — no real credentials in the tracked tree. The kernel
reads the user/agent/worker tokens from the environment; only clearly-marked
development defaults may appear in source.

Deliberately avoids spawning subprocesses with piped stdio (blocked in this
environment); it walks the working tree and skips generated directories.
"""

from __future__ import annotations

import os
import re

import pytest

# tests/test_hygiene.py -> tests -> repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Patterns that would indicate a real committed credential.
SUSPICIOUS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),          # OpenAI-style key
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),          # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),              # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".ini", ".json",
    ".yaml", ".yml", ".txt", ".sh", ".ps1",
}

# Generated / vendored directories that are not part of the source tree.
SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", "wheelhouse",
    "var", ".uv-cache", ".uv-python", ".worktrees", "node_modules",
    "dist", "runtime", "logs",
}


def _iter_source_files():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in TEXT_SUFFIXES:
                yield os.path.join(dirpath, name)


def test_no_obvious_credentials_in_source_tree():
    offenders = []
    for path in _iter_source_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        for pattern in SUSPICIOUS:
            if pattern.search(content):
                offenders.append((os.path.relpath(path, REPO_ROOT), pattern.pattern))
    assert not offenders, f"possible committed credentials: {offenders}"


def test_user_token_is_read_from_environment_not_hardcoded():
    """The auth module must consult the environment for the user token."""
    auth_path = os.path.join(REPO_ROOT, "apps", "api", "agenthub_rt", "auth.py")
    assert os.path.exists(auth_path), auth_path
    with open(auth_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "AGENT_HUB_USER_TOKEN" in src
    assert "os.environ" in src


# --------------------------------------------------------------------------- #
# credentials must not live under the repo, and tokens must not be logged
# --------------------------------------------------------------------------- #


def _load_config():
    import importlib

    import agenthub_kernel.config as cfg

    return importlib.reload(cfg)


def test_codex_home_defaults_outside_the_repository(monkeypatch):
    """CODEX_HOME can hold a login credential, so it must not sit in the repo.

    Even inside a gitignored directory it would be one ``git add -f``, archive or
    backup away from leaking.
    """
    monkeypatch.delenv("AGENT_HUB_CODEX_HOME", raising=False)
    cfg = _load_config()
    home = os.path.abspath(cfg.codex_home())
    repo = os.path.abspath(REPO_ROOT)
    assert not home.lower().startswith(repo.lower() + os.sep), (
        f"CODEX_HOME is inside the repository: {home}"
    )


def test_credential_location_is_outside_the_repository(monkeypatch):
    monkeypatch.delenv("AGENT_HUB_CODEX_HOME", raising=False)
    cfg = _load_config()
    cred = os.path.abspath(cfg.credential_location())
    repo = os.path.abspath(REPO_ROOT)
    assert os.path.basename(cred) == "auth.json"
    assert not cred.lower().startswith(repo.lower() + os.sep), (
        f"credential path is inside the repository: {cred}"
    )


def test_codex_home_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", str(tmp_path / "custom-home"))
    cfg = _load_config()
    assert os.path.abspath(cfg.codex_home()) == os.path.abspath(str(tmp_path / "custom-home"))


def test_resolving_codex_home_has_no_side_effects(monkeypatch, tmp_path):
    """Reading settings must not create directories.

    Resolving used to call os.makedirs, so merely building the settings (or
    describing the adapter) could fail - or write - outside the workspace. Path
    resolution is now pure; only an actual launch creates the directory.
    """
    target = tmp_path / "resolved-but-not-created"
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", str(target))
    cfg = _load_config()
    assert os.path.abspath(cfg.codex_home()) == os.path.abspath(str(target))
    assert not target.exists(), "resolving CODEX_HOME must not create it"
    assert os.path.abspath(cfg.credential_location()) == os.path.abspath(
        str(target / "auth.json")
    )
    assert not target.exists()


def test_ensure_codex_home_creates_the_directory(monkeypatch, tmp_path):
    target = tmp_path / "to-be-created"
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", str(target))
    cfg = _load_config()
    assert os.path.abspath(cfg.ensure_codex_home()) == os.path.abspath(str(target))
    assert target.is_dir()


def test_codex_home_status_is_safe_to_report(monkeypatch, tmp_path):
    """The status dict answers "is a login configured?" without reading anything.

    It must expose paths and booleans only - no field may carry credential
    content, and asking must not create the directory (the durable home is made by
    the operator's own login, not as a side effect of a status check).
    """
    target = tmp_path / "long-term-home"
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", str(target))
    cfg = _load_config()

    status = cfg.codex_home_status()

    assert set(status) == {
        "codex_home",
        "outside_repository",
        "exists",
        "writable",
        "credential",
        "credential_present",
        "override_env",
    }
    assert status["outside_repository"] is True
    assert status["exists"] is False and status["credential_present"] is False
    assert status["credential"].endswith("auth.json")
    assert not target.exists(), "a status check must not create CODEX_HOME"

    # An in-repo home is reported as such, so tooling can refuse it.
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", os.path.join(REPO_ROOT, "var", "in-repo-home"))
    cfg = _load_config()
    assert cfg.codex_home_status()["outside_repository"] is False


def test_login_tool_never_names_the_credential_file():
    """The login helper must ask config for the path, never hardcode or open it.

    Prose may mention the file (the docstring explains why it belongs outside the
    repo); CODE must not name it, because that is how a tool ends up opening a
    credential.
    """
    import ast

    path = os.path.join(REPO_ROOT, "tools", "demo", "codex_login.py")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()

    doc_spans = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node):
                first = node.body[0]
                doc_spans.append((first.lineno, first.end_lineno or first.lineno))

    code_lines = [
        line
        for number, line in enumerate(src.splitlines(), 1)
        if not any(lo <= number <= hi for lo, hi in doc_spans)
    ]
    code = "\n".join(code_lines)

    assert "auth.json" not in code, "the tool's code must not name the credential file"
    assert "codex_home_status" in code, "the tool must ask config where the home is"
    assert "login status" in code, "the tool must tell the user how to verify the login"


def test_serve_banner_never_prints_the_token():
    """The startup banner lands in scrollback, logs and screenshots.

    Guard at source level: serve.py must not interpolate a token value into any
    print/echo. It may only report WHERE the token came from.
    """
    import re

    path = os.path.join(REPO_ROOT, "tools", "demo", "serve.py")
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"print\(", line) and re.search(r"token", line, re.IGNORECASE)
        and "not shown" not in line
        and "value not shown" not in line
    ]
    assert not offenders, f"serve.py prints a token value: {offenders}"
    assert "value not shown" in src or "not shown" in src


def test_no_tool_prints_the_user_token():
    """Same guard across every script under tools/."""
    import re

    bad = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, "tools")):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            p = os.path.join(dirpath, name)
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if re.search(r"print\(", line) and re.search(r"token", line, re.IGNORECASE):
                        if "not shown" in line or "token=" in line:
                            continue
                        bad.append(f"{os.path.relpath(p, REPO_ROOT)}:{i}: {line.strip()}")
    assert not bad, f"scripts print a token: {bad}"


def test_gitignore_excludes_runtime_artifacts():
    """The SQLite file, wheel cache and venv must be ignored, not committed."""
    gitignore = os.path.join(REPO_ROOT, ".gitignore")
    with open(gitignore, "r", encoding="utf-8") as fh:
        lines = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    for expected in ("/wheelhouse/", "/var/", ".venv/", "*.db", "__pycache__/"):
        assert expected in lines, f"{expected} missing from .gitignore"
