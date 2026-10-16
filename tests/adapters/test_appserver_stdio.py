"""Process/stream regressions using Python only: no Codex, auth, or model calls."""

from __future__ import annotations

import io
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from agenthub_adapters import appserver as A


class ProcessDouble:
    def __init__(self, waits):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.pid = 12345
        self.returncode = None
        self.waits = list(waits)
        self.events = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.events.append(("wait", timeout))
        result = self.waits.pop(0)
        if result is None:
            raise subprocess.TimeoutExpired("python-stub", timeout)
        self.returncode = result
        return result

    def terminate(self):
        self.events.append(("terminate",))

    def kill(self):
        self.events.append(("kill",))


def fake_transport(monkeypatch, tmp_path, waits):
    process = ProcessDouble(waits)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    transport = A.StdioTransport(["python-stub"], stderr_path=str(tmp_path / "stderr.log"))
    return transport, process


def assert_handles_closed(transport, process):
    assert process.stdin.closed
    assert process.stdout.closed
    assert transport._stderr_file.closed


def test_popen_failure_closes_stderr(monkeypatch, tmp_path):
    captured = {}

    def fail(*args, **kwargs):
        captured["stderr"] = kwargs["stderr"]
        raise OSError("simulated spawn failure")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(OSError, match="simulated spawn failure"):
        A.StdioTransport(["python-stub"], stderr_path=str(tmp_path / "stderr.log"))
    assert captured["stderr"].closed


@pytest.mark.parametrize("waits,signals,code", [
    ([0], [], 0),
    ([None, 17], ["terminate"], 17),
    ([None, None, 23], ["terminate", "kill"], 23),
])
def test_close_waits_for_eof_then_escalates_and_reaps(monkeypatch, tmp_path, waits, signals, code):
    transport, process = fake_transport(monkeypatch, tmp_path, waits)
    transport.close()
    assert [event[0] for event in process.events if event[0] != "wait"] == signals
    timeouts = [event[1] for event in process.events if event[0] == "wait"]
    assert timeouts == [transport._EOF_TIMEOUT, transport._TERMINATE_TIMEOUT,
                        transport._KILL_TIMEOUT][:len(waits)]
    if signals:
        assert process.events[-1][0] == "wait", "kill must be followed by a reap"
    assert transport.returncode == code
    assert not transport.alive
    assert_handles_closed(transport, process)
    events = list(process.events)
    transport.close()
    assert process.events == events, "a second close must not signal again"
    with pytest.raises(A.AppServerDisconnected):
        transport.write(b"after close\n")


def test_close_does_not_claim_an_unreaped_child_stopped(monkeypatch, tmp_path):
    transport, process = fake_transport(monkeypatch, tmp_path, [None, None, None])
    with pytest.raises(A.AppServerTimeout, match="exit unconfirmed"):
        transport.close()
    assert transport.alive
    assert transport.returncode is None
    assert_handles_closed(transport, process)
    # A cleanup retry may reap the same child; it must not create another one.
    process.waits.append(19)
    transport.close()
    assert transport.returncode == 19


def test_signal_error_does_not_leak_handles_or_claim_exit(monkeypatch, tmp_path):
    transport, process = fake_transport(monkeypatch, tmp_path, [None])

    def denied():
        raise PermissionError("cannot terminate child")

    monkeypatch.setattr(process, "terminate", denied)
    with pytest.raises(PermissionError):
        transport.close()
    assert transport.alive
    assert transport.returncode is None
    assert_handles_closed(transport, process)


def test_exit_racing_with_terminate_is_reaped(monkeypatch, tmp_path):
    transport, process = fake_transport(monkeypatch, tmp_path, [None, 7])

    def exited():
        process.returncode = 7
        raise ProcessLookupError("already exited")

    monkeypatch.setattr(process, "terminate", exited)
    transport.close()
    assert transport.returncode == 7
    assert_handles_closed(transport, process)


def test_unbuffered_write_handles_short_writes(monkeypatch, tmp_path):
    transport, process = fake_transport(monkeypatch, tmp_path, [0])

    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:2])

    process.stdin = ShortWriter()
    try:
        transport.write(b"hello\n")
        assert process.stdin.getvalue() == b"hello\n"
    finally:
        transport.close()


def test_unbuffered_write_rejects_no_progress(monkeypatch, tmp_path):
    transport, process = fake_transport(monkeypatch, tmp_path, [0])
    monkeypatch.setattr(process.stdin, "write", lambda data: 0)
    try:
        with pytest.raises(A.AppServerDisconnected, match="no progress"):
            transport.write(b"hello\n")
    finally:
        transport.close()


def test_real_python_stdio_handshake_and_graceful_exit(tmp_path):
    script = Path(__file__).resolve().parents[2] / "tools" / "demo" / "stub_appserver.py"
    stderr_path = tmp_path / "stderr.log"
    env = dict(os.environ, CODEX_HOME=str(tmp_path))
    transport = A.StdioTransport([sys.executable, str(script)], cwd=str(tmp_path),
                                 env=env, stderr_path=str(stderr_path))
    client = A.AppServerClient(transport, request_timeout=5)
    client.start()
    try:
        session = A.AppServerSession(client)
        assert "serverInfo" in session.initialize(client_name="agent-hub-s1")
        assert session.handshake_complete
        assert session.start_thread(cwd=str(tmp_path)) == "stub-thread-0001"
        assert client.detected_framing == A.FRAMING_LINES
        assert transport.alive and transport.returncode is None
    finally:
        client.close()
    client._reader.join(timeout=2)
    assert not client._reader.is_alive()
    assert transport.returncode == 0, stderr_path.read_text(encoding="utf-8")
    assert not transport.alive
    assert_handles_closed(transport, transport._proc)
    transport.close()


def test_real_python_ignoring_eof_is_terminated(tmp_path, monkeypatch):
    transport = A.StdioTransport([sys.executable, "-c", "import time; time.sleep(60)"],
                                 cwd=str(tmp_path), stderr_path=str(tmp_path / "stderr.log"))
    monkeypatch.setattr(transport, "_EOF_TIMEOUT", 0.05)
    transport.close()
    assert not transport.alive
    assert transport.returncode is not None
    assert_handles_closed(transport, transport._proc)


def read_bounded(stream, operation):
    """Keep a regression in partial-chunk handling from hanging the test suite."""
    results = queue.Queue()
    fills = 0
    original_fill = stream._fill

    def guarded_fill():
        nonlocal fills
        fills += 1
        if fills > 100:
            raise AssertionError("partial buffer spun without consuming the queue")
        return original_fill()

    stream._fill = guarded_fill

    def run():
        try:
            results.put(operation())
        except BaseException as exc:
            results.put(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "read remained blocked after EOF"
    result = results.get_nowait()
    if isinstance(result, BaseException):
        raise result
    return result


@pytest.mark.parametrize("framing", [A.FRAMING_LINES, A.FRAMING_LSP])
def test_queue_stream_assembles_partial_chunks(framing):
    stream = A.QueueStream()
    message = {"id": 1, "result": {"label": "partial chunks"}}
    encoded = A.encode_message(message, framing)
    for start in range(0, len(encoded), 3):
        stream.write(encoded[start:start + 3])
    stream.close()
    assert read_bounded(stream, lambda: A.read_message(stream, framing)) == (message, framing)


@pytest.mark.parametrize("method", ["readline", "read"])
def test_queue_stream_returns_partial_tail_on_eof(method):
    stream = A.QueueStream()
    stream.write(b"part")
    stream.write(b"ial")
    stream.close()
    operation = stream.readline if method == "readline" else lambda: stream.read(20)
    assert read_bounded(stream, operation) == b"partial"
    assert stream.read(1) == b""
