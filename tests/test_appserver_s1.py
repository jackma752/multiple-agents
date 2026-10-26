"""S1 must reject forbidden traffic BEFORE it reaches any transport."""
import importlib.util
import json
import os
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from agenthub_kernel import config

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("s1_probe", ROOT / "tools/probes/appserver_s1.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


@pytest.fixture
def s1():
    server, streams = P.S.make_pair("happy")
    transport = P._QueueTransport(streams["reader"], streams["writer"])
    client = P.S1Client(transport, workdir="test-empty-dir", request_timeout=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client.start()
    yield client, server
    server.stop()
    client.close()
    thread.join(timeout=2)


def initialize(client):
    return P.A.AppServerSession(client).initialize(client_name="agent-hub-s1", version="0.1.0")


@pytest.mark.parametrize("message", [
    {"id": 1, "method": "turn/start", "params": {}},
    {"id": 1, "method": "thread/resume", "params": {}},
    {"id": 1, "method": "thread/fork", "params": {}},
    {"id": 1, "method": "command/exec", "params": {}},
    {"id": 1, "result": {"decision": "accept"}},
    {"method": "initialized", "params": {}},
    {"id": 1, "method": "future/unsafe-method", "params": {}},
])
def test_forbidden_message_is_never_written(message):
    transport = Mock()
    client = P.S1Client(transport, workdir="empty")
    with pytest.raises(P.CheckFailed):
        client._send(message)
    transport.write.assert_not_called()
    assert client.sent == []


def test_only_one_readonly_empty_thread_is_allowed(s1):
    client, server = s1
    initialize(client)
    with pytest.raises(P.CheckFailed):
        client.request("thread/start", {"cwd": client.workdir, "sandbox": "danger-full-access"})
    params = {"cwd": client.workdir, "sandbox": "read-only", "approvalPolicy": "never"}
    assert client.request("thread/start", params)["thread"]["id"]
    with pytest.raises(P.CheckFailed):
        client.request("thread/start", params)
    assert server.received_methods() == ["initialize", "initialized", "thread/start"]


@pytest.mark.parametrize("message", [
    {"id": 44, "method": "item/commandExecution/requestApproval", "params": {"secret": "private"}},
    {"method": "turn/started", "params": {}},
    {"method": "item/agentMessage/delta", "params": {"delta": "private"}},
])
def test_unsolicited_activity_fails_without_logging_payload(message):
    client = P.S1Client(Mock(), workdir="empty")
    with pytest.raises(P.A.AppServerProtocolError):
        client._dispatch(message)
    assert client.violation
    assert "private" not in json.dumps(client.received)


def test_exact_home_checked_without_overwriting_existing_file(tmp_path, monkeypatch):
    wrong = tmp_path / "wrong"
    right = tmp_path / "selected"
    wrong.mkdir()
    right.mkdir()
    sentinel = right / ".agenthub-write-probe"
    sentinel.write_text("keep")
    monkeypatch.setenv(config.ENV_CODEX_HOME, str(wrong))
    assert config.codex_home_write_check(str(right))[0]
    assert sentinel.read_text() == "keep"
    assert list(right.iterdir()) == [sentinel]
    assert list(wrong.iterdir()) == []


def test_missing_pin_stops_before_home_or_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "S1_CODEX_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(P, "RUN_ROOT", str(tmp_path / "runs"))
    monkeypatch.delenv(config.ENV_CODEX_PATH, raising=False)
    launch, home = Mock(), Mock()
    monkeypatch.setattr(P.A, "StdioTransport", launch)
    monkeypatch.setattr(config, "codex_home_write_check", home)
    assert P.run_real(str(tmp_path / "home"), clean=True) == 1
    launch.assert_not_called()
    home.assert_not_called()
    record = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text(encoding="utf-8"))
    assert record["process_started"] is False
    assert record["passed"] is False
    assert record["sent"] == []


def test_pin_environment_cannot_silently_select_new_binary(monkeypatch):
    monkeypatch.setenv(config.ENV_CODEX_PATH, "some-new-codex.exe")
    with pytest.raises(P.CheckFailed):
        P.preflight("home")


def test_cleanup_is_scoped_and_missing_env_is_not_cwd(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    run = root / "s1-owned"
    run.mkdir(parents=True)
    (run / "proof").write_text("test")
    monkeypatch.setattr(P, "RUN_ROOT", str(root))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    assert not P._clean_own_run_dir(str(root))
    assert not P._clean_own_run_dir(str(tmp_path))
    assert not P._clean_own_run_dir(str(run), protected_home=str(run / "home"))
    assert P._clean_own_run_dir(str(run))
    assert root.exists() and not run.exists()


def test_dead_child_cannot_pass(s1):
    client, _ = s1
    # Fixture already started the pump; exercise only owns a single start.
    client.start = lambda: None
    child = Mock(alive=False, returncode=1)
    with pytest.raises(P.CheckFailed, match="进程仍存活"):
        P.exercise_s1(client, child)


def test_cleanup_failure_prevents_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "RUN_ROOT", str(tmp_path / "runs"))
    child = Mock(pid=123, alive=True, returncode=None)
    child.close.side_effect = OSError("cannot stop")
    monkeypatch.setattr(P.A, "StdioTransport", Mock(return_value=child))
    monkeypatch.setattr(P, "exercise_s1", lambda client, transport: "stub-thread")
    assert P.run_process_probe() == 1
    record = json.loads(next((tmp_path / "runs").glob("*/result.json")).read_text(encoding="utf-8"))
    assert record["process_stopped"] is False
    assert record["passed"] is False


def test_real_preflight_checks_requested_home(tmp_path, monkeypatch):
    # The rule under test is "the home is outside the repository and its workspace".
    # Pin the probe's REPO to a scratch layout so the assertion does not depend on
    # where pytest's tmp_path happens to live: in a confined environment TMP can fall
    # back to a directory inside the real repository's parent, which would make this
    # test fail for a reason that has nothing to do with the rule.
    fake_repo = tmp_path / "workspace" / "repo"
    fake_repo.mkdir(parents=True)
    monkeypatch.setattr(P, "REPO", str(fake_repo))

    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"not executed")
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").touch()  # presence only; no real credential
    monkeypatch.setattr(P, "S1_CODEX_PATH", str(binary))
    monkeypatch.setattr(P, "S1_CODEX_SIZE", binary.stat().st_size)
    monkeypatch.setattr(P, "S1_CODEX_SHA256", P.sha256(str(binary)))
    for key in (config.ENV_CODEX_PATH, config.ENV_CODEX_SIZE, config.ENV_CODEX_SHA256):
        monkeypatch.delenv(key, raising=False)
    check = Mock(return_value=(True, "ok"))
    monkeypatch.setattr(config, "codex_home_write_check", check)
    monkeypatch.setattr(P, "build_real_environment", lambda path: {"CODEX_HOME": path})
    _, env = P.preflight(str(home))
    check.assert_called_once_with(str(home))
    assert env["CODEX_HOME"] == str(home)


def test_preflight_rejects_a_home_next_to_the_repository(tmp_path, monkeypatch):
    """The workspace clause must bite: the drive root must NOT swallow it.

    Regression for the `REPO/../..` bug: that expression normalises to `E:\\`, which
    contains every path on the drive, so the check rejected legitimate locations.
    """
    fake_repo = tmp_path / "workspace" / "repo"
    fake_repo.mkdir(parents=True)
    monkeypatch.setattr(P, "REPO", str(fake_repo))
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"not executed")
    monkeypatch.setattr(P, "S1_CODEX_PATH", str(binary))
    monkeypatch.setattr(P, "S1_CODEX_SIZE", binary.stat().st_size)
    monkeypatch.setattr(P, "S1_CODEX_SHA256", P.sha256(str(binary)))
    for key in (config.ENV_CODEX_PATH, config.ENV_CODEX_SIZE, config.ENV_CODEX_SHA256):
        monkeypatch.delenv(key, raising=False)
    for label, location in (("inside the repo", fake_repo / "home"),
                            ("beside the repo", tmp_path / "workspace" / "home")):
        location.mkdir(parents=True, exist_ok=True)
        (location / "auth.json").touch()
        with pytest.raises(P.CheckFailed):
            P.preflight(str(location))
