"""DeepSeek wire defaults and persisted HTTP failures; all transports are offline."""
import io
import json
import urllib.error
import uuid
from email.message import Message

from fastapi.testclient import TestClient

from agenthub.contracts import InvocationSnapshot
from agenthub_adapters.deepseek import DeepSeekAdapter, HttpTransport
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel import config
from agenthub_kernel.db import Database
from agenthub_rt.api import create_app

USER = {"Authorization": "Bearer dev-user-token"}
KEY = "test-secret-without-sk-prefix"
PROMPT = "private input must not appear in a diagnostic"


def make_adapter(tmp_path, monkeypatch, *, response_body=b"", status=400, model=""):
    credential = tmp_path / "credential.json"
    credential.write_text(json.dumps({"api_key": KEY}), encoding="utf-8")
    transport = HttpTransport("https://api.deepseek.com/chat/completions", KEY, proxy="http://localhost:8090")
    requests = []
    errors = []

    class Opener:
        def open(self, request, timeout=None):
            requests.append(request)
            headers = Message()
            headers["Server"] = "offline-gateway"
            headers["Content-Length"] = str(len(response_body))
            headers["Set-Cookie"] = "must-not-be-retained"
            headers["X-Request-Id"] = "offline-request-id"
            error = urllib.error.HTTPError(request.full_url, status, "rejected", headers, io.BytesIO(response_body))
            errors.append(error)
            raise error

    monkeypatch.setattr(transport, "_opener", lambda: Opener())
    adapter = DeepSeekAdapter(
        "https://api.deepseek.com", model, credentials_path=str(credential), transport=transport,
        store=ExecutionStore(":memory:", table="test_ds_" + uuid.uuid4().hex),
    )
    return adapter, requests, errors


def test_default_model_reaches_wire_and_empty_400_does_not_retry(tmp_path, monkeypatch):
    monkeypatch.delenv(config.ENV_DEEPSEEK_MODEL, raising=False)
    call, requests, errors = make_adapter(tmp_path, monkeypatch)
    assert config.deepseek_settings()["model"] == call.model == "deepseek-flash"
    snap = InvocationSnapshot(agent_id="deepseek", task_input=PROMPT, depth=0,
                              parent_task_id=None, adapter_version=call.binding_identity())
    result = call.start("inv-test", snap, gate_token="approved")
    assert not result.ok and result.terminal
    assert len(requests) == 1
    assert json.loads(requests[0].data)["model"] == "deepseek-flash"
    assert errors[0].fp.closed
    payload = call.store.get("inv-test")["payload"]
    assert "empty response body" in payload["detail"]
    assert payload["http"] == {
        "status": 400, "body_state": "empty",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "request_model": "deepseek-flash", "proxy_configured": True,
        "headers": {"server": "offline-gateway", "content-length": "0", "x-request-id": "offline-request-id"},
    }
    assert "empty response body" in result.detail


def test_error_echo_does_not_persist_credential_or_prompt(tmp_path, monkeypatch):
    body = json.dumps({"error": {"message": f"bad request: {KEY}; {PROMPT}"}}).encode()
    call, _, _ = make_adapter(tmp_path, monkeypatch, response_body=body)
    snap = InvocationSnapshot(agent_id="deepseek", task_input=PROMPT, depth=0,
                              parent_task_id=None, adapter_version=call.binding_identity())
    result = call.start("inv-test", snap, gate_token="approved")
    saved = json.dumps(call.store.get("inv-test")) + result.detail
    assert KEY not in saved and PROMPT not in saved and "must-not-be-retained" not in saved
    assert "[redacted]" in saved


def test_current_default_model_completes_stream_and_keeps_usage(tmp_path, monkeypatch):
    call, _, _ = make_adapter(tmp_path, monkeypatch)
    seen = []

    class Opener:
        def open(self, request, timeout=None):
            body = json.loads(request.data)
            seen.append(body)
            assert body["model"] == "deepseek-flash"
            event = {"model": body["model"], "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6,
                               "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 5}}
            return io.BytesIO(b'data: ' + json.dumps(event).encode() + b'\n\ndata: [DONE]\n')

    monkeypatch.setattr(call._transport, "_opener", lambda: Opener())
    snap = InvocationSnapshot(agent_id="deepseek", task_input="你好", depth=0,
                              parent_task_id=None, adapter_version=call.binding_identity())
    result = call.start("inv-test", snap, gate_token="approved")
    assert result.ok and result.terminal and len(seen) == 1
    assert seen[0]["messages"][0]["content"] == "你好"
    assert call.store.get("inv-test")["payload"]["reply"] == "ok"
    assert call.usage_report("inv-test")["model"] == "deepseek-flash"
    assert call.usage_report("inv-test")["usage"]["total_tokens"] == 6


def test_unreadable_error_body_is_distinguished_from_empty():
    from agenthub_adapters.deepseek import DeepSeekRefused, _raise_for_status
    import pytest

    class BrokenBody(io.BytesIO):
        def read(self, size):
            raise OSError("offline broken body")

    error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 400, "bad", {}, BrokenBody())
    with pytest.raises(DeepSeekRefused) as caught:
        list(_raise_for_status(error))
    assert caught.value.http["body_state"] == "unreadable"
    assert "could not be read" in caught.value.detail


def test_explicit_model_override_is_not_silently_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_DEEPSEEK_MODEL, "gateway-custom-model")
    assert config.deepseek_settings()["model"] == "gateway-custom-model"
    call, requests, _ = make_adapter(tmp_path, monkeypatch, model="deepseek-chat")
    snap = InvocationSnapshot(agent_id="deepseek", task_input="test", depth=0,
                              parent_task_id=None, adapter_version=call.binding_identity())
    result = call.start("inv-test", snap, gate_token="approved")
    assert json.loads(requests[0].data)["model"] == "deepseek-chat"
    assert "retired model name" in result.detail
    assert "AGENT_HUB_DEEPSEEK_MODEL=deepseek-flash" in result.detail


def test_failure_reason_survives_chat_reload_and_request_needs_approval(tmp_path, monkeypatch):
    adapter, requests, _ = make_adapter(tmp_path, monkeypatch)
    app = create_app(db=Database(f"sqlite:///{(tmp_path / 'chat.db').as_posix()}"), adapter=adapter)
    with TestClient(app) as client:
        cid = client.post("/api/conversations", headers=USER, json={"title": "failure"}).json()["conversation_id"]
        response = client.post(f"/api/conversations/{cid}/invocations", headers=USER, json={
            "agent_id": "deepseek", "text": PROMPT, "client_request_id": "one",
        })
        assert response.status_code == 200, response.text
        turn = response.json()
        assert requests == []
        denied = client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER, params={"wait": "true"})
        assert denied.status_code == 409 and requests == []
        assert client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=USER).status_code == 200
        client.post(f"/api/invocations/{turn['invocation_id']}/run", headers=USER, params={"wait": "true"})
        assert len(requests) == 1
        page = client.get(f"/api/conversations/{cid}/messages", headers=USER).json()["messages"]
        failed = next(m["invocation"] for m in page if m.get("invocation"))
        assert failed["status"] == "failed"
        assert failed["approval_status"] == "consumed"
        assert "deepseek_http_400" in failed["error"] and "empty response body" in failed["error"]
        assert PROMPT not in failed["error"] and KEY not in failed["error"]
    app.state.db.engine.dispose()
