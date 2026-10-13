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


def test_gitignore_excludes_runtime_artifacts():
    """The SQLite file, wheel cache and venv must be ignored, not committed."""
    gitignore = os.path.join(REPO_ROOT, ".gitignore")
    with open(gitignore, "r", encoding="utf-8") as fh:
        lines = {ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")}
    for expected in ("/wheelhouse/", "/var/", ".venv/", "*.db", "__pycache__/"):
        assert expected in lines, f"{expected} missing from .gitignore"
