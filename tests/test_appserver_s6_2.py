"""S6-2 driver, offline only: the bundled stdio stub through the whole Hub path.

Real mode is exercised only up to its refusals; every launch path is replaced
by a sentinel, so no Codex process or model call can happen here.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from agenthub_adapters import appserver as appserver_module
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER
from agenthub_kernel import config

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s6_2_probe_under_test", ROOT / "tools/probes/appserver_s6_2.py")
S62 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S62)


def run(tmp_path, **kwargs):
    code = S62.run_probe(run_root=tmp_path / "evidence", work_root=tmp_path, **kwargs)
    [result_file] = list((tmp_path / "evidence").glob("*/result.json"))
    return code, json.loads(result_file.read_text(encoding="utf-8"))


def test_stub_happy_passes_through_the_whole_hub_path(tmp_path):
    before = {k: os.environ.get(k) for k in ("AGENT_HUB_ADAPTER", "AGENT_HUB_APPSERVER_STUB")}
    data_dir_before = config.DATA_DIR
    code, result = run(tmp_path, scenario="happy")
    assert code == 0 and result["passed"] is True and result["reason_codes"] == []
    assert result["status"] == "succeeded" and result["start_count"] == 1 and result["held_slots"] == 0
    assert result["reply_exact"] is True and result["binding_matches"] is True
    assert result["binding_identity"] == "appserver-stub:stdio"
    order = result["order"]
    assert order["starting"] < order["turn_started"] < order["first_delta"] < order["turn_ended"] < order["succeeded"]
    assert result["workdir_empty"] is True
    assert Path(result["data_dir"]).parent == tmp_path  # outside the repository in real use
    assert result["record"]["tool_process_state"] == "none" and result["record"]["native_agents"] == []
    # The run leaves the process environment and the kernel data dir as they were.
    assert {k: os.environ.get(k) for k in before} == before and config.DATA_DIR == data_dir_before
    assert S62.PROMPT not in json.dumps(result)  # only its hash is recorded


@pytest.mark.parametrize("scenario, code", [
    ("turn-failed", "status_not_succeeded"),
    ("settings-mismatch", "status_not_succeeded"),
    ("tool-unfinished", "tool_activity"),
    ("native-spawn", "native_agent_observed"),
])
def test_stub_counterexamples_fail(tmp_path, scenario, code):
    exit_code, result = run(tmp_path, scenario=scenario)
    assert exit_code == 1 and result["passed"] is False and code in result["reason_codes"]


def test_real_mode_without_both_flags_is_refused_before_anything(tmp_path, capsys):
    for flags in ({}, {"approved": True}, {"acknowledged": True}):
        assert S62.run_probe(real=True, run_root=tmp_path / "evidence", work_root=tmp_path, **flags) == 2
    assert not (tmp_path / "evidence").exists() and list(tmp_path.iterdir()) == []
    assert "REFUSED" in capsys.readouterr().out


def test_real_mode_preflight_failure_launches_nothing(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(appserver_module, "StdioTransport",
                        lambda argv, **kw: launched.append(argv) or (_ for _ in ()).throw(OSError("sentinel")))
    monkeypatch.setitem(PINNED_APPSERVER, "path", str(tmp_path / "missing" / "codex.exe"))
    code, result = run(tmp_path, real=True, approved=True, acknowledged=True,
                       codex_home=str(tmp_path / "home"))
    assert code == 1 and result["refused"].startswith("preflight failed")
    assert "pinned_identity_matches" in result["refused"] and "codex_home_has_login" in result["refused"]
    assert launched == []
    assert not (Path(result["data_dir"]) / "agent-hub.db").exists()  # nothing was created


def test_real_mode_refuses_identity_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(appserver_module, "StdioTransport",
                        lambda argv, **kw: (_ for _ in ()).throw(AssertionError("must not launch")))
    monkeypatch.setenv("AGENT_HUB_CODEX_SHA256", "0" * 64)
    code, result = run(tmp_path, real=True, approved=True, acknowledged=True,
                       codex_home=str(tmp_path / "home"))
    assert code == 1 and "no_identity_override" in result["refused"]
    assert "supervised_admission" in result["refused"]


def test_judge_rejects_wrong_order_and_survivors():
    evidence = {"status": "succeeded", "start_count": 1, "held_slots": 0, "reply_exact": True,
                "binding_matches": True, "workdir_empty": True,
                "order": {"starting": 1, "turn_started": 3, "first_delta": 2, "turn_ended": 4, "succeeded": 5},
                "record": {"tool_process_state": "none", "usage": {"inputTokens": 1},
                           "local_process_exit_confirmed": True,
                           "descendants": {"before_close": [{"pid": 1, "image": "x.exe"}],
                                           "after_grace": [{"pid": 1, "image": "x.exe"}]}}}
    assert S62.judge(evidence, expected_reply="OK") == ["event_order", "descendants_survived"]


@pytest.mark.parametrize("report", [None, {"before_close": [], "after_grace": None},
                                    {"before_close": None, "after_grace": []}])
def test_judge_never_reads_an_unavailable_descendant_check_as_none(report):
    evidence = {"status": "succeeded", "start_count": 1, "held_slots": 0, "reply_exact": True,
                "binding_matches": True, "workdir_empty": True,
                "order": {"starting": 1, "turn_started": 2, "first_delta": 3, "turn_ended": 4, "succeeded": 5},
                "record": {"tool_process_state": "none", "usage": {"inputTokens": 1},
                           "local_process_exit_confirmed": True, "descendants": report}}
    assert S62.judge(evidence, expected_reply="OK") == ["descendants_unverifiable"]
