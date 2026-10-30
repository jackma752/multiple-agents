"""The local write preflight must return promptly even under Windows sandbox denial."""
import os
from pathlib import Path
import runpy
import tempfile
from unittest.mock import Mock


# Configuration is a stdlib-only module. This preflight must be diagnosable
# independently of the Worker's adapter/network dependency imports.
CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                          "apps/worker/agenthub_kernel/config.py"))["codex_home_write_check"]


def test_denial_returns_after_one_attempt_when_access_reports_writable(tmp_path, monkeypatch):
    denied = Mock(side_effect=PermissionError("write denied by execution policy"))
    with monkeypatch.context() as patch:
        # Keep a regression to the old Windows tempfile behavior bounded.
        patch.setattr(tempfile, "TMP_MAX", 4)
        patch.setattr(os, "access", lambda *_: True)
        patch.setattr(os, "open", denied)
        ok, detail = CHECK(str(tmp_path))
    assert not ok and "write denied by execution policy" in detail
    assert denied.call_count == 1
    assert list(tmp_path.iterdir()) == []


def test_write_failure_cleans_up_only_the_file_it_created(tmp_path, monkeypatch):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep")
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", Mock(side_effect=PermissionError("write failed")))
        ok, detail = CHECK(str(tmp_path))
    assert not ok and "write failed" in detail
    assert list(tmp_path.iterdir()) == [sentinel]
    assert sentinel.read_text() == "keep"


def test_success_preserves_existing_files_and_checks_the_selected_directory(tmp_path, monkeypatch):
    selected, other = tmp_path / "selected", tmp_path / "other"
    selected.mkdir()
    other.mkdir()
    sentinel = selected / ".agenthub-write-probe"
    sentinel.write_text("keep")
    monkeypatch.setenv("AGENT_HUB_CODEX_HOME", str(other))
    assert CHECK(str(selected))[0]
    assert list(selected.iterdir()) == [sentinel]
    assert list(other.iterdir()) == []
    assert sentinel.read_text() == "keep"
