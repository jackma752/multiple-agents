"""Offline checks of the Claude Code real-test driver (tools/probes/claude_code_min.py).

Stub mode only; every real-mode path is shown to refuse before any launch.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_code_min", ROOT / "tools" / "probes" / "claude_code_min.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def report(run_root: Path) -> dict:
    [path] = list(run_root.glob("*/report.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_identity_stub_passes_and_records_no_values(tmp_path):
    assert probe.run_identity(run_root=tmp_path) == 0
    result = report(tmp_path)
    assert result["verdict"] == "PASS" and result["version_line"].startswith("2.1.308")
    assert result["help_missing_flags"] == []
    assert "text" not in result["version"]


@pytest.mark.parametrize("scenario, verdict, code", [
    ("success-usage", "PASS", None),
    ("success-no-usage", "INCONCLUSIVE", "no_usage"),
    ("error-result", "FAIL", "status_failed"),
    ("malformed", "FAIL", "status_unknown"),
])
def test_turn_stub_verdicts(tmp_path, scenario, verdict, code):
    probe.run_turn(scenario=scenario, run_root=tmp_path)
    result = report(tmp_path)
    assert result["verdict"] == verdict
    if code:
        assert code in result["reason_codes"]
    blob = json.dumps(result, ensure_ascii=False)
    assert probe.PROMPT not in blob  # hashes and structure only


@pytest.fixture
def no_launch(monkeypatch):
    calls = []

    def sentinel(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("a process was launched")

    monkeypatch.setattr(subprocess, "Popen", sentinel)
    return calls


def test_real_steps_refuse_without_their_approvals(tmp_path, no_launch):
    assert probe.run_identity(real=True, run_root=tmp_path) == 2
    assert probe.run_turn(real=True, approved=True, run_root=tmp_path) == 2
    assert probe.run_turn(real=True, acknowledged=True, run_root=tmp_path) == 2
    assert no_launch == [] and not list(tmp_path.iterdir())


def test_real_turn_refuses_while_identity_is_unrecorded(tmp_path, no_launch, monkeypatch):
    monkeypatch.setattr("agenthub_adapters.claude_code.ZERO_MODEL_IDENTITY_REPORT", None)
    assert probe.run_turn(real=True, approved=True, acknowledged=True, run_root=tmp_path) == 1
    result = report(tmp_path)
    assert result["refused"] and "identity_report_recorded" in result["refused"]
    assert no_launch == []
