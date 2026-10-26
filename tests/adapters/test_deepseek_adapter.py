"""DeepSeek adapter tests - fully offline.

The network is never touched: every call is driven through an injected
``transport`` (a plain callable) or the canned :class:`StubTransport`. The
assertions focus on the properties that make this channel worth having at all:

* the request body carries one user message and no ``tools`` - so the reply can
  only be text;
* the credential is read from one file outside the repository, is never taken
  from the environment, and never reaches a payload or a stored record;
* a local refusal is terminal and costs nothing, a 429 carries quota evidence,
  and a broken stream settles as unknown rather than as a success;
* ``cancel`` says ``unsupported`` instead of pretending.
"""

from __future__ import annotations

import io
import json
import urllib.error
import uuid

import pytest

from agenthub.contracts import STREAM_DELTA, STREAM_TURN_ENDED, STREAM_TURN_STARTED, InvocationSnapshot
from agenthub_adapters.deepseek import (
    ALLOWED_BODY_KEYS,
    CHAT_PATH,
    DeepSeekAdapter,
    DeepSeekRateLimited,
    DeepSeekRefused,
    DeepSeekTransportError,
    HttpTransport,
    StubTransport,
    credential_status,
    load_api_key,
    redact,
)
from agenthub_adapters.store import FAILED, FINISHED, RUNNING, ExecutionStore

KEY = "sk-test-0123456789abcdef"


def snapshot(text: str = "你好，请用一句话介绍你自己。") -> InvocationSnapshot:
    return InvocationSnapshot(
        agent_id="deepseek",
        task_input=text,
        parent_task_id=None,
        depth=0,
        adapter_version="0.1.0",
        execution_mode="strict",
    )


def adapter(tmp_path, *, transport=None, stub="", base_url="https://api.deepseek.com") -> DeepSeekAdapter:
    return DeepSeekAdapter(
        base_url,
        "deepseek-chat",
        credentials_path=str(tmp_path / "deepseek" / "credentials.json"),
        # A fresh table per test: ``:memory:`` stores with the same table name are
        # shared process-wide, so reusing one would leak records between tests.
        store=ExecutionStore(":memory:", table="de_" + uuid.uuid4().hex),
        transport=transport,
        stub=stub,
    )


def write_credentials(tmp_path, key: str = KEY) -> str:
    path = tmp_path / "deepseek" / "credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"api_key": key}), encoding="utf-8")
    return str(path)


class Recording:
    """A transport that records the payload and replays canned events."""

    def __init__(self, events: list[dict] | None = None) -> None:
        self.events = events or []
        self.payloads: list[dict] = []
        self.headers: list[dict] = []

    def __call__(self, payload, headers):
        self.payloads.append(payload)
        self.headers.append(dict(headers))
        yield from self.events


def ok_events(text_parts=("你好", "，我是 DeepSeek。"), usage=None) -> list[dict]:
    events: list[dict] = []
    for index, piece in enumerate(text_parts):
        last = index == len(text_parts) - 1
        events.append({
            "id": "chunk",
            "model": "deepseek-chat",
            "choices": [{"index": 0, "delta": {"content": piece},
                         "finish_reason": "stop" if last else None}],
        })
    events.append({
        "id": "chunk",
        "model": "deepseek-chat",
        "choices": [],
        "usage": usage if usage is not None else {
            "prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26,
            "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 12,
        },
    })
    return events


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def test_load_api_key_reads_one_field_from_one_file(tmp_path):
    path = write_credentials(tmp_path)
    assert load_api_key(path) == KEY


def test_missing_credentials_refuse_with_a_named_code(tmp_path):
    with pytest.raises(DeepSeekRefused) as caught:
        load_api_key(str(tmp_path / "nope" / "credentials.json"))
    assert caught.value.code == "deepseek_credentials_missing"


def test_credentials_inside_the_repository_are_refused(tmp_path):
    repo = tmp_path / "repo"
    path = repo / "credentials.json"
    repo.mkdir()
    path.write_text(json.dumps({"api_key": KEY}), encoding="utf-8")
    with pytest.raises(DeepSeekRefused) as caught:
        load_api_key(str(path), repo_root=str(repo))
    assert caught.value.code == "deepseek_credentials_inside_repository"


def test_invalid_json_and_missing_field_share_one_code(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(DeepSeekRefused) as caught:
        load_api_key(str(path))
    assert caught.value.code == "deepseek_credentials_invalid"

    path.write_text(json.dumps({"token": KEY}), encoding="utf-8")
    with pytest.raises(DeepSeekRefused) as caught:
        load_api_key(str(path))
    assert caught.value.code == "deepseek_credentials_invalid"


def test_a_key_in_the_environment_is_never_consulted(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment-000000")
    with pytest.raises(DeepSeekRefused) as caught:
        load_api_key(str(tmp_path / "absent.json"))
    assert caught.value.code == "deepseek_credentials_missing"


def test_credential_status_never_returns_the_content(tmp_path):
    path = write_credentials(tmp_path)
    status = credential_status(path)
    assert status["exists"] is True and status["size"] > 0
    assert KEY not in json.dumps(status)


def test_redact_removes_anything_key_shaped():
    assert "sk-***" in redact(f"Bearer {KEY} failed")
    assert KEY not in redact(f"Bearer {KEY} failed")


# --------------------------------------------------------------------------- #
# The request we build
# --------------------------------------------------------------------------- #

def test_request_carries_one_user_message_and_no_tools(tmp_path):
    write_credentials(tmp_path)
    recorder = Recording(ok_events())
    call = adapter(tmp_path, transport=recorder)
    call.start("inv-1", snapshot(), gate_token="gate-1")

    payload = recorder.payloads[0]
    assert set(payload) <= ALLOWED_BODY_KEYS
    assert "tools" not in payload and "functions" not in payload
    assert payload["messages"] == [{"role": "user", "content": "你好，请用一句话介绍你自己。"}]
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["model"] == "deepseek-chat"


def test_the_key_never_reaches_the_payload_or_the_stored_record(tmp_path):
    write_credentials(tmp_path)
    recorder = Recording(ok_events())
    call = adapter(tmp_path, transport=recorder)
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert KEY not in json.dumps(recorder.payloads[0])
    record = call.store.get("inv-1")
    assert KEY not in json.dumps(record)
    assert KEY not in json.dumps(result.__dict__)
    # The one place it may appear is the Authorization header of the real request.
    assert "Authorization" not in recorder.headers[0]


def test_url_appends_the_chat_path_once(tmp_path):
    assert adapter(tmp_path).url == "https://api.deepseek.com" + CHAT_PATH
    assert adapter(tmp_path, base_url="https://proxy.local/v1").url == "https://proxy.local/v1" + CHAT_PATH


# --------------------------------------------------------------------------- #
# Refusals that cost nothing
# --------------------------------------------------------------------------- #

def test_missing_gate_token_refuses_and_leaves_no_record(tmp_path):
    write_credentials(tmp_path)
    recorder = Recording(ok_events())
    call = adapter(tmp_path, transport=recorder)
    result = call.start("inv-1", snapshot())

    assert result.ok is False and result.terminal is True
    assert result.detail == "refused: missing_gate_token"
    assert recorder.payloads == []
    assert call.store.get("inv-1") is None


def test_missing_credentials_refuse_before_any_request(tmp_path):
    recorder = Recording(ok_events())
    call = adapter(tmp_path, transport=recorder)
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert result.ok is False and result.terminal is True
    assert result.detail == "refused: deepseek_credentials_missing"
    assert recorder.payloads == []
    # Nothing was launched, so the ledger may record a known zero.
    assert call.usage_report("inv-1") is None


def test_admission_is_strict_and_says_what_it_cannot_do(tmp_path):
    admission = adapter(tmp_path).execution_admission()
    assert admission.allowed is True
    assert admission.mode == "strict"
    assert admission.code == "no_local_execution_channel"
    assert "无法中途取消" in admission.reason


def test_capabilities_do_not_claim_tools_cancel_or_subagents(tmp_path):
    caps = adapter(tmp_path).capabilities()
    assert caps["stream"] is True and caps["real_usage"] is True
    assert caps["cancel"] is False
    assert caps["intercept_tool"] is False
    assert caps["control_subagent"] is False
    assert caps["resume_session"] is False
    assert caps["limit_dir"] is False


def test_native_resume_is_not_supported(tmp_path):
    call = adapter(tmp_path)
    assert call.supports_native_resume() is False
    assert call.context_profile() == {"adds_prompt_text": False, "observed": []}


# --------------------------------------------------------------------------- #
# A finished turn
# --------------------------------------------------------------------------- #

def test_finished_turn_streams_deltas_and_records_the_reply(tmp_path):
    write_credentials(tmp_path)
    recorder = Recording(ok_events())
    call = adapter(tmp_path, transport=recorder)
    seen: list[tuple[str, dict]] = []
    result = call.start_streaming("inv-1", snapshot(), gate_token="gate-1",
                                  emit=lambda etype, payload: seen.append((etype, payload)))

    assert result.ok is True and result.terminal is True
    assert result.detail == "finished: stop"
    record = call.store.get("inv-1")
    assert record["status"] == FINISHED
    assert record["payload"]["reply"] == "你好，我是 DeepSeek。"
    assert record["payload"]["finish_reason"] == "stop"
    assert call.find_invocation_for_adapter_id(result.adapter_invocation_id) == "inv-1"

    types = [etype for etype, _ in seen]
    assert types[0] == STREAM_TURN_STARTED
    assert types.count(STREAM_DELTA) == 2
    assert types[-1] == STREAM_TURN_ENDED
    assert [payload["text"] for etype, payload in seen if etype == STREAM_DELTA] == ["你好", "，我是 DeepSeek。"]
    assert seen[-1][1]["terminal"] is True


def test_usage_report_uses_the_final_chunk(tmp_path):
    write_credentials(tmp_path)
    call = adapter(tmp_path, transport=Recording(ok_events()))
    call.start("inv-1", snapshot(), gate_token="gate-1")

    report = call.usage_report("inv-1")
    assert report["mapping"] == "deepseek.chat_completions.usage"
    assert report["source"] == "sse_final_chunk"
    assert report["usage"]["prompt_tokens"] == 20


def test_a_turn_without_usage_reports_none_rather_than_zero(tmp_path):
    write_credentials(tmp_path)
    events = ok_events()
    events[-1].pop("usage")
    call = adapter(tmp_path, transport=Recording(events))
    call.start("inv-1", snapshot(), gate_token="gate-1")

    report = call.usage_report("inv-1")
    assert report["mapping"] == "none"
    assert "no_usage_reported" in report["notes"]


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #

def test_a_stream_that_ends_without_finish_reason_is_failed_not_success(tmp_path):
    write_credentials(tmp_path)
    events = ok_events()
    events = [e for e in events if not any(c.get("finish_reason") for c in e["choices"])]
    call = adapter(tmp_path, transport=Recording(events))
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert result.ok is False and result.terminal is True
    assert result.detail == "failed: stream ended before finish_reason"
    record = call.store.get("inv-1")
    assert record["status"] == FAILED
    assert record["payload"]["error"] == "stream_incomplete"


def test_rate_limit_carries_quota_evidence_for_the_stage_layer(tmp_path):
    write_credentials(tmp_path)
    call = adapter(tmp_path, stub="quota")
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert result.ok is False and result.terminal is True
    assert result.detail == "refused: deepseek_rate_limited"
    record = call.store.get("inv-1")
    assert record["status"] == FAILED
    assert record["payload"]["provider_quota"]["kind"] == "provider_quota"


def test_unauthorized_is_a_terminal_refusal(tmp_path):
    write_credentials(tmp_path)
    call = adapter(tmp_path, stub="unauthorized")
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert result.ok is False and result.terminal is True
    assert result.detail == "refused: deepseek_unauthorized"


def test_a_transport_error_is_unknown_and_keeps_the_slot(tmp_path):
    write_credentials(tmp_path)

    def broken(payload, headers):
        raise DeepSeekTransportError("connection reset")
        yield  # pragma: no cover - makes this a generator

    call = adapter(tmp_path, transport=broken)
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert result.ok is False
    assert result.terminal is False, "a dropped stream may already have been billed"
    assert result.detail == "unknown: transport_error"
    # The record stays RUNNING: nothing here may confirm the call stopped.
    assert call.store.get("inv-1")["status"] == RUNNING


def test_cancel_reports_unsupported_instead_of_pretending(tmp_path):
    write_credentials(tmp_path)
    call = adapter(tmp_path, transport=Recording(ok_events()))
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    assert call.cancel(result.adapter_invocation_id)["reason"] == "unsupported"
    assert call.cancel_invocation("inv-1") == {"ok": False, "reason": "unsupported", "known": True}


def test_status_lookup_by_adapter_id(tmp_path):
    write_credentials(tmp_path)
    call = adapter(tmp_path, transport=Recording(ok_events()))
    result = call.start("inv-1", snapshot(), gate_token="gate-1")

    status = call.get_status(result.adapter_invocation_id)
    assert status["ok"] is True and status["status"] == FINISHED
    assert call.get_status("deepseek-unknown")["ok"] is False


# --------------------------------------------------------------------------- #
# SSE parsing (the HTTP transport itself)
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


def test_http_transport_parses_data_lines_and_stops_at_done(tmp_path, monkeypatch):
    lines = [
        b": keep-alive\n",
        b"\n",
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
        b"data: [DONE]\n",
        b'data: {"choices":[{"delta":{"content":"never"}}]}\n',
    ]
    response = FakeResponse(lines)

    class Opener:
        def open(self, request, timeout=None):
            assert request.method == "POST"
            assert request.get_header("Authorization") == f"Bearer {KEY}"
            return response

    transport = HttpTransport("https://api.deepseek.com/chat/completions", KEY)
    monkeypatch.setattr(transport, "_opener", lambda: Opener())

    events = list(transport({"model": "deepseek-chat"}, {"Content-Type": "application/json"}))
    assert events == [{"choices": [{"delta": {"content": "hi"}}]}]
    assert response.closed is True


@pytest.mark.parametrize("status,expected", [
    (401, "deepseek_unauthorized"),
    (403, "deepseek_unauthorized"),
    (402, "deepseek_insufficient_balance"),
    (500, "deepseek_http_500"),
])
def test_http_errors_map_to_named_refusals(status, expected):
    error = urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions", status, "boom", None,
        io.BytesIO(b'{"error":"x"}'),
    )
    from agenthub_adapters.deepseek import _raise_for_status
    with pytest.raises(DeepSeekRefused) as caught:
        list(_raise_for_status(error))
    assert caught.value.code == expected


def test_http_429_maps_to_rate_limited_with_retry_after():
    headers = {"Retry-After": "30"}
    error = urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions", 429, "slow down", headers,
        io.BytesIO(b'{"error":"rate limit"}'),
    )
    from agenthub_adapters.deepseek import _raise_for_status
    with pytest.raises(DeepSeekRateLimited) as caught:
        list(_raise_for_status(error))
    assert caught.value.evidence["kind"] == "provider_quota"


def test_unparsable_sse_line_is_a_transport_error():
    class Opener:
        def open(self, request, timeout=None):
            return FakeResponse([b"data: {not json}\n"])

    transport = HttpTransport("https://api.deepseek.com/chat/completions", KEY)
    transport._opener = lambda: Opener()  # type: ignore[method-assign]
    with pytest.raises(DeepSeekTransportError):
        list(transport({}, {}))


# --------------------------------------------------------------------------- #
# Offline stub scenarios
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("scenario,ok,terminal", [
    ("ok", True, True),
    ("quota", False, True),
    ("unauthorized", False, True),
    ("truncated", False, True),
    ("empty", False, True),
])
def test_stub_scenarios_never_claim_success_without_a_finish_reason(tmp_path, scenario, ok, terminal):
    call = adapter(tmp_path, stub=scenario)
    result = call.start("inv-1", snapshot(), gate_token="gate-1")
    assert (result.ok, result.terminal) == (ok, terminal)
    if scenario in ("quota", "unauthorized", "truncated", "empty"):
        assert result.detail.startswith(("refused:", "failed:"))


def test_stub_truncated_has_content_but_no_finish_reason(tmp_path):
    """The truncated fixture must actually look truncated, not merely short."""
    events = list(StubTransport("truncated")({}, {}))
    assert any(e["choices"] and e["choices"][0]["delta"].get("content") for e in events)
    assert not any(e["choices"] and e["choices"][0].get("finish_reason") for e in events)


def test_unknown_stub_scenario_falls_back_to_ok(tmp_path):
    assert StubTransport("nonsense").scenario == "ok"


def test_stub_needs_no_credential(tmp_path):
    call = adapter(tmp_path, stub="ok")  # credentials file was never written
    result = call.start("inv-1", snapshot(), gate_token="gate-1")
    assert result.ok is True
