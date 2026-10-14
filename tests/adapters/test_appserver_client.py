"""The app-server prototype, verified offline against a scripted server.

The prototype adds a SECOND transport next to `codex exec`, so the failures that
matter are protocol-level rather than model-level:

* request-id matching - a late or duplicate response must be dropped, never
  matched to another call;
* notification ORDER - a delta may precede the response to the request that caused
  it, but notifications must never be reordered among themselves;
* message increments assembled in arrival order;
* approvals: surfaced, explicitly answered, never automatic, and silence is not
  consent;
* interrupts: `turn/interrupt` returning OK is NOT a stop - only a later
  `turn/completed` with a terminal `turn.status` settles the call;
* disconnect and timeout: unconfirmed, so the kernel keeps the quota slot.

Method names and payload shapes follow the LOCAL schema capture
(`codex app-server generate-json-schema --experimental`): `thread/start`,
`thread/resume`, `turn/start`, `turn/interrupt`, `thread/started`, `turn/started`,
`item/started`, `item/completed`, `item/agentMessage/delta`,
`item/commandExecution/requestApproval`, and `turn/completed` carrying
`turn.status` in {completed, interrupted, failed, inProgress}.

No Codex process, no network, no model call: the server is scripted and the
streams are in-memory queues.
"""

from __future__ import annotations

import time

import pytest

from agenthub_adapters import appserver as A
from agenthub_adapters import appserver_stub as S


class StreamTransport:
    """Adapts the in-memory stream pair to the transport interface."""

    def __init__(self, reader, writer) -> None:
        self.reader = reader
        self._writer = writer
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise A.AppServerDisconnected("transport closed")
        self._writer.write(data)

    def close(self) -> None:
        self.closed = True
        self._writer.close()
        self.reader.close()


class Harness:
    """A scripted server plus a client, torn down together."""

    def __init__(self, scenario: str, *, request_timeout: float = 5.0) -> None:
        import threading

        self.server, streams = S.make_pair(scenario)
        self.transport = StreamTransport(streams["reader"], streams["writer"])
        self.client = A.AppServerClient(self.transport, request_timeout=request_timeout)
        self.session = A.AppServerSession(self.client)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Harness":
        self.client.start()
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.stop()
        self.client.close()


@pytest.fixture
def harness():
    made: list[Harness] = []

    def build(scenario: str, *, request_timeout: float = 5.0) -> Harness:
        instance = Harness(scenario, request_timeout=request_timeout)
        made.append(instance)
        return instance.__enter__()

    yield build
    for instance in made:
        instance.__exit__()


# --------------------------------------------------------------------------- #
# framing: the real encoding is UNVERIFIED, so both are supported
# --------------------------------------------------------------------------- #


def test_line_framing_round_trip():
    payload = A.encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"}, A.FRAMING_LINES)
    assert payload.endswith(b"\n")
    message, framing = A.read_message(_BytesReader(payload), A.FRAMING_AUTO)
    assert framing == A.FRAMING_LINES and message["method"] == "x"


def test_lsp_framing_round_trip():
    payload = A.encode_message({"jsonrpc": "2.0", "id": 2, "method": "y"}, A.FRAMING_LSP)
    assert payload.startswith(b"Content-Length: ")
    message, framing = A.read_message(_BytesReader(payload), A.FRAMING_AUTO)
    assert framing == A.FRAMING_LSP and message["id"] == 2


def test_framing_is_auto_detected_rather_than_assumed():
    """`codex app-server --help` documents the transport, not the framing."""
    for framing in (A.FRAMING_LINES, A.FRAMING_LSP):
        payload = A.encode_message({"jsonrpc": "2.0", "method": "m"}, framing)
        message, detected = A.read_message(_BytesReader(payload), A.FRAMING_AUTO)
        assert detected == framing and message["method"] == "m"


def test_garbage_is_not_silently_accepted():
    with pytest.raises(A.AppServerProtocolError):
        A.read_message(_BytesReader(b"not json at all\n"), A.FRAMING_AUTO)


def test_empty_stream_reports_disconnect():
    with pytest.raises(A.AppServerDisconnected):
        A.read_message(_BytesReader(b""), A.FRAMING_AUTO)


def test_lsp_framing_without_length_header_is_an_error():
    with pytest.raises(A.AppServerProtocolError):
        A.read_message(_BytesReader(b"X-Thing: 1\r\n\r\n{}"), A.FRAMING_AUTO)


# --------------------------------------------------------------------------- #
# happy path: thread, turn, deltas, terminal status
# --------------------------------------------------------------------------- #


def test_thread_turn_and_delta_stream_end_to_end(harness):
    h = harness("happy")
    thread_id = h.session.start_thread(cwd="C:\\stub")
    assert thread_id == "stub-thread-0001"

    turn_id = h.session.start_turn("say OK")
    assert turn_id == "stub-turn-0001"

    outcome = h.session.await_turn_end(timeout=3.0)
    assert outcome.terminal is True
    assert outcome.verdict == "finished"
    assert outcome.turn_status == A.TURN_STATUS_COMPLETED
    assert h.session.collect_deltas() == "OK", "increments must assemble in order"


def test_response_ids_are_matched_not_guessed(harness):
    h = harness("happy")
    first = h.session.start_thread()
    second = h.session.start_turn("x", thread_id=first)
    assert first == "stub-thread-0001"
    assert second == "stub-turn-0001", "a response was matched to the wrong request"


def test_a_duplicate_response_for_an_unknown_id_is_dropped(harness):
    """The id-confusion bug this prototype must not have: a late reply stealing a call."""
    h = harness("duplicate-ids")
    h.session.start_thread()
    assert h.session.start_turn("x") == "stub-turn-0001"
    assert h.client.request(A.THREAD_START, {})["thread"]["id"] == "stub-thread-0001"


def test_notifications_may_precede_the_causing_response(harness):
    """Out of order relative to the response, never reordered among themselves."""
    h = harness("out-of-order")
    h.session.start_thread()
    h.session.start_turn("x")
    early = h.client.wait_for(lambda n: n.method == A.DELTA_AGENT_MESSAGE, timeout=3.0)
    assert early is not None and early.params["delta"] == "before-response"


def test_a_failed_turn_is_terminal_and_reported_as_failed(harness):
    h = harness("turn-failed")
    h.session.start_thread()
    h.session.start_turn("x")
    outcome = h.session.await_turn_end(timeout=3.0)
    assert outcome.terminal is True and outcome.verdict == "failed"


def test_in_progress_status_is_not_terminal():
    """`turn/completed` is the notification NAME; the status is what decides."""
    verdict, terminal, detail = A.judge_turn_outcome(
        terminal_notification=A.TURN_COMPLETED, turn_status=A.TURN_STATUS_IN_PROGRESS
    )
    assert terminal is False and verdict == "unconfirmed"
    assert "not terminal" in detail


# --------------------------------------------------------------------------- #
# approvals: never automatic
# --------------------------------------------------------------------------- #


def test_approval_request_is_surfaced_and_answered_explicitly(harness):
    h = harness("approval-allow")
    h.session.start_thread()
    h.session.start_turn("run a command")

    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)
    assert A.requires_explicit_decision(request.method)
    assert request.answered is False, "an approval must not be pre-answered"

    h.client.answer_approval(request, "accept")
    outcome = h.session.await_turn_end(timeout=3.0)
    assert outcome.terminal is True
    assert h.server.answered_approvals() == [{"id": request.id, "decision": "accept"}]


def test_approval_params_match_the_schema_and_are_content_free(harness):
    h = harness("approval-silent")
    h.session.start_thread()
    h.session.start_turn("x")
    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    for required in ("itemId", "startedAtMs", "threadId", "turnId"):
        assert required in request.params, f"schema requires {required}"
    summary = A.describe_approval(request.params)
    assert summary["command"] == "echo stub"
    assert summary["threadId"] == "stub-thread-0001" and summary["turnId"] == "stub-turn-0001"


def test_declining_a_command_does_not_fail_the_turn(harness):
    h = harness("approval-decline")
    h.session.start_thread()
    h.session.start_turn("run a command")
    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    h.client.answer_approval(request, "decline")
    outcome = h.session.await_turn_end(timeout=3.0)
    assert outcome.terminal is True and outcome.verdict == "finished"
    assert h.server.answered_approvals() == [{"id": request.id, "decision": "decline"}]


def test_silence_is_never_approval(harness):
    h = harness("approval-silent")
    h.session.start_thread()
    h.session.start_turn("x")
    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    assert request.answered is False
    assert h.client.approval_policy.default_decision == "decline"
    assert h.server.answered_approvals() == []


def test_the_policy_answers_pending_requests_and_defaults_to_decline(harness):
    h = harness("approval-silent")
    h.session.start_thread()
    h.session.start_turn("x")
    _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    answered = h.client.auto_answer_pending()
    assert answered and answered[0]["decision"] == "decline"
    assert h.client.approval_policy.decisions[0]["decision"] == "decline"


def test_unsupported_and_structured_decisions_are_refused(harness):
    """Structured decisions persist policy for FUTURE commands: not this client's job."""
    h = harness("approval-silent")
    h.session.start_thread()
    h.session.start_turn("x")
    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    for bad in ("acceptEverything", "yes", "approved", "timed_out"):
        with pytest.raises(A.AppServerError):
            h.client.answer_approval(request, bad)
    assert "acceptWithExecpolicyAmendment" not in A.SUPPORTED_DECISIONS
    assert "applyNetworkPolicyAmendment" not in A.SUPPORTED_DECISIONS


def test_an_approval_cannot_be_answered_twice(harness):
    h = harness("approval-silent")
    h.session.start_thread()
    h.session.start_turn("x")
    request = _wait_for_server_request(h.client, S.APPROVAL_REQUEST)

    h.client.answer_approval(request, "decline")
    with pytest.raises(A.AppServerError):
        h.client.answer_approval(request, "accept")


def test_approval_summary_drops_prompt_text():
    summary = A.describe_approval({
        "command": "echo hi", "cwd": "C:\\w", "availableDecisions": ["accept"],
        "prompt": "user secret content", "text": "more secret",
    })
    assert summary["command"] == "echo hi"
    assert "prompt" not in summary and "text" not in summary


# --------------------------------------------------------------------------- #
# interrupt: accepted is NOT stopped
# --------------------------------------------------------------------------- #


def test_interrupt_accepted_without_terminal_status_is_not_a_stop():
    verdict, terminal, detail = A.judge_turn_outcome(
        interrupt_requested=True, interrupt_accepted=True
    )
    assert terminal is False, "an accepted interrupt must not settle a call"
    assert "may still be running" in detail


def test_interrupt_then_terminal_status_settles_the_call():
    verdict, terminal, _detail = A.judge_turn_outcome(
        terminal_notification=A.TURN_COMPLETED,
        turn_status=A.TURN_STATUS_INTERRUPTED,
        interrupt_requested=True,
        interrupt_accepted=True,
    )
    assert terminal is True and verdict == "cancelled"


def test_transport_close_is_not_a_stop_either():
    _verdict, terminal, detail = A.judge_turn_outcome(transport_closed=True)
    assert terminal is False and "cannot claim" in detail


def test_timeout_is_unconfirmed():
    _verdict, terminal, _detail = A.judge_turn_outcome(timed_out=True)
    assert terminal is False


def test_terminal_statuses_map_to_the_exec_adapters_vocabulary():
    for status, expected in (
        (A.TURN_STATUS_COMPLETED, "finished"),
        (A.TURN_STATUS_FAILED, "failed"),
        (A.TURN_STATUS_INTERRUPTED, "cancelled"),
    ):
        verdict, terminal, _detail = A.judge_turn_outcome(
            terminal_notification=A.TURN_COMPLETED, turn_status=status
        )
        assert (verdict, terminal) == (expected, True)


def test_interrupt_accepted_then_nothing_is_the_dangerous_case(harness):
    """End-to-end shape of the rule: accepted, no terminal status, still unconfirmed."""
    h = harness("interrupt-accepted")
    h.session.start_thread()
    h.session.start_turn("long task")
    reply = h.session.interrupt_turn()      # the server answers with an empty object
    assert isinstance(reply, dict), "the schema defines an empty-object response"

    outcome = h.session.await_turn_end(timeout=0.6, interrupt_requested=True,
                                       interrupt_accepted=True)
    assert outcome.terminal is False, "an accepted interrupt must not free the slot"
    assert outcome.verdict == "unconfirmed"


def test_interrupt_then_terminal_status_end_to_end(harness):
    h = harness("interrupt-terminal")
    h.session.start_thread()
    h.session.start_turn("long task")
    h.session.interrupt_turn()

    outcome = h.session.await_turn_end(timeout=3.0, interrupt_requested=True,
                                       interrupt_accepted=True)
    assert outcome.terminal is True
    assert outcome.verdict == "cancelled"
    assert outcome.turn_status == A.TURN_STATUS_INTERRUPTED


def test_interrupt_requires_both_ids(harness):
    h = harness("happy")
    with pytest.raises(A.AppServerError):
        h.session.interrupt_turn()


# --------------------------------------------------------------------------- #
# disconnect and timeout
# --------------------------------------------------------------------------- #


def test_disconnect_mid_turn_marks_unconfirmed_and_blocks_further_calls(harness):
    h = harness("disconnect")
    h.session.start_thread()
    h.session.start_turn("x")

    assert _wait_until(lambda: h.client.disconnected, timeout=3.0)
    assert h.client.transport_closed_confirmed is True

    outcome = h.session.await_turn_end(timeout=0.5)
    assert outcome.terminal is False
    with pytest.raises(A.AppServerDisconnected):
        h.client.request(A.THREAD_START, {})


def test_a_turn_without_terminal_status_times_out_unconfirmed(harness):
    h = harness("slow-terminal")
    h.session.start_thread()
    h.session.start_turn("x")
    outcome = h.session.await_turn_end(timeout=0.4)
    assert outcome.terminal is False and outcome.verdict == "unconfirmed"
    assert "deadline" in outcome.detail


def test_writing_to_a_closed_transport_reports_disconnect(harness):
    h = harness("happy")
    h.client.close()
    with pytest.raises(A.AppServerDisconnected):
        h.client.request(A.THREAD_START, {})


def test_json_rpc_error_becomes_a_response_error(harness):
    h = harness("happy")
    with pytest.raises(A.AppServerResponseError) as caught:
        h.client.request("__no_such_method__", {}, timeout=3.0)
    assert caught.value.code == -32601


def test_initialize_is_optional_and_its_response_is_recorded(harness):
    """The handshake is NOT assumed: nothing requires it until a real session shows it does."""
    h = harness("happy")
    result = h.session.initialize(client_name="agent-hub-test", version="9.9.9")
    assert result["serverInfo"]["name"] == "stub-appserver"
    assert h.session.initialized == result
    # A session that never initialized still works (the stub does not require it).
    h2 = harness("happy")
    assert h2.session.initialized is None
    assert h2.session.start_thread() == "stub-thread-0001"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0

    def readline(self) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        index = self._payload.find(b"\n", self._pos)
        if index < 0:
            line = self._payload[self._pos:]
            self._pos = len(self._payload)
            return line
        line = self._payload[self._pos:index + 1]
        self._pos = index + 1
        return line

    def read(self, count: int = -1) -> bytes:
        if count < 0:
            count = len(self._payload) - self._pos
        piece = self._payload[self._pos:self._pos + count]
        self._pos += len(piece)
        return piece


def _wait_until(predicate, *, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _wait_for_server_request(client, method: str, *, timeout: float = 3.0):
    for _ in range(int(timeout / 0.02)):
        for request in client.server_requests():
            if request.method == method:
                return request
        time.sleep(0.02)
    raise AssertionError(f"server never sent {method}")
