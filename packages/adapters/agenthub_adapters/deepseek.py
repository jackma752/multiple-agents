"""DeepSeek adapter: one OpenAI-compatible ``chat/completions`` call per turn.

Why this shape (P5, ``docs/p5-deepseek-adapter.md``)
---------------------------------------------------
DeepSeek's public API is a plain HTTP endpoint. There is no local process to
launch, no tool channel and no sub-agent channel, so this adapter is the one
place in the project where "the model cannot act on this machine" is a property
of the **request we build**, not a promise made by a vendor:

* the body contains exactly one user message - :attr:`InvocationSnapshot.task_input`
  - and no ``tools``/``functions`` key at all (asserted in the tests), so the
    reply can only be text;
* nothing is executed locally, so there is no process tree, no work directory and
  no shell to bound;
* the credential never enters a payload, a log line or an event.

What it deliberately does **not** claim:

* no mid-turn cancel - once the request is away we can only stop reading
  (``cancel`` reports ``unsupported`` rather than pretending);
* no native resume - the API is stateless, so ``supports_native_resume`` is
  False and the Worker refuses a ``native_resume`` call before consuming its
  approval;
* a stream that dies before ``finish_reason`` is *unknown* (``terminal=False``):
  tokens may already have been spent, and we cannot prove the reply completed.

Failure semantics, mirroring the rest of the project: a local refusal (no
credential, bad path, missing gate token) is **terminal and costs nothing**; an
authenticated 401/402 is terminal too (nothing was generated); a 429 carries
``provider_quota`` evidence so the stage layer can wait; a transport failure is
**not** terminal.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Iterable, Iterator, Optional

from agenthub.contracts import (
    AdapterContract,
    AdapterStartResult,
    ExecutionAdmission,
    InvocationSnapshot,
    STREAM_DELTA,
    STREAM_TURN_ENDED,
    STREAM_TURN_STARTED,
)
from agenthub.usage import MAPPING_DEEPSEEK, MAPPING_NONE
from agenthub_adapters.quota import quota_error
from agenthub_adapters.store import (
    FAILED,
    FINISHED,
    RUNNING,
    ExecutionStore,
)

#: The agent_id this adapter serves (the console fills its agent field from it).
AGENT_IDS = frozenset({"deepseek"})

#: Path appended to the configured base URL. Kept separate so a base URL that
#: already ends in ``/v1`` (an OpenAI-compatible proxy) still works.
CHAT_PATH = "/chat/completions"

#: Every key this adapter is allowed to put in a request body. Anything else
#: (``tools`` above all) would be a capability this channel does not have.
ALLOWED_BODY_KEYS = frozenset({"model", "messages", "stream", "stream_options"})

CREDENTIALS_FILENAME = "credentials.json"

_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redact(text: Any) -> str:
    """Remove anything shaped like an API key from text that may be stored."""
    return _KEY_PATTERN.sub("sk-***", str(text))


class DeepSeekRefused(Exception):
    """A local or authenticated refusal: terminal, and nothing was generated."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = redact(detail)


class DeepSeekRateLimited(Exception):
    """HTTP 429: the provider said "wait", with optional machine-readable reset."""

    def __init__(self, evidence: dict, detail: str = "") -> None:
        super().__init__(detail or "rate limited")
        self.evidence = evidence
        self.detail = redact(detail)


class DeepSeekTransportError(Exception):
    """Connection dropped, timed out or answered something unparsable."""


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def load_api_key(credentials_path: str, *, repo_root: str = "") -> str:
    """Read ``{"api_key": "sk-..."}`` from ``credentials_path``.

    Deliberately narrow:

    * the file must live **outside the repository** - a key under version
      control is one ``git add -f`` away from leaving the machine;
    * no environment variable is consulted, so a stray ``DEEPSEEK_API_KEY`` in a
      shell cannot silently change which account pays (``AGENT_HUB_DEEPSEEK_HOME``
      only redirects the file);
    * only the ``api_key`` field is read; anything else in the file is ignored.
    """
    path = os.path.abspath(credentials_path)
    if repo_root:
        root = os.path.abspath(repo_root)
        if path == root or path.startswith(root + os.sep):
            raise DeepSeekRefused(
                "deepseek_credentials_inside_repository",
                f"credential path is inside the repository: {path}",
            )
    if not os.path.isfile(path):
        raise DeepSeekRefused(
            "deepseek_credentials_missing",
            f"no credential file at {path} (write {{\"api_key\": \"sk-...\"}} there)",
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise DeepSeekRefused("deepseek_credentials_unreadable", f"{path}: {exc.strerror or exc}") from None
    except ValueError:
        raise DeepSeekRefused("deepseek_credentials_invalid", f"{path} is not valid JSON") from None
    key = document.get("api_key") if isinstance(document, dict) else None
    if not isinstance(key, str):
        raise DeepSeekRefused(
            "deepseek_credentials_invalid",
            f'{path} must contain {{"api_key": "sk-..."}}',
        )
    # Some Windows hidden-input terminals retain a literal Ctrl+V after a paste.
    # It is not a token character, and sending it in ``Authorization`` makes the
    # gateway reject the request with an empty 400 (billed nothing, but reported
    # as an API error, which is the misleading part). Normalize this known paste
    # artifact in memory; the stored file is never rewritten.
    key = key.strip().rstrip("\x16").strip()
    if len(key) < 8 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise DeepSeekRefused(
            "deepseek_credentials_invalid",
            "API key is too short or contains an embedded control character; re-enter the key",
        )
    return key


def credential_status(credentials_path: str) -> dict:
    """What the console may show: whether a usable file exists - never its content."""
    path = os.path.abspath(credentials_path)
    exists = os.path.isfile(path)
    return {
        "credentials_path": path,
        "exists": exists,
        "size": os.path.getsize(path) if exists else None,
    }


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #

class HttpTransport:
    """POST one streaming request and yield the parsed SSE events.

    Kept as a small object so tests (and the offline demo) can inject a fake
    with the same signature: ``transport(payload, headers) -> Iterator[dict]``.
    """

    def __init__(self, url: str, api_key: str, *, proxy: str = "", ca_bundle: str = "",
                 timeout: float = 120.0) -> None:
        self.url = url
        self._api_key = api_key
        self.proxy = proxy
        self.ca_bundle = ca_bundle
        self.timeout = timeout

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        context = ssl.create_default_context(cafile=self.ca_bundle or None)
        handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def __call__(self, payload: dict, headers: dict) -> Iterator[dict]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")
        for name, value in headers.items():
            request.add_header(name, value)
        request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            response = self._opener().open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            yield from _raise_for_status(exc)
            return
        except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError) as exc:
            raise DeepSeekTransportError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None
        try:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    return
                try:
                    event = json.loads(chunk)
                except ValueError:
                    raise DeepSeekTransportError("unparsable SSE data line") from None
                if isinstance(event, dict):
                    yield event
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing must never mask the outcome
                pass


def _raise_for_status(exc: urllib.error.HTTPError) -> Iterator[dict]:
    """Turn an HTTP error into the right exception, without keeping the body."""
    detail = ""
    try:
        detail = exc.read(2000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the body is a bonus
        detail = ""
    status = int(getattr(exc, "code", 0) or 0)
    if status == 429:
        retry_after = ""
        try:
            retry_after = str(exc.headers.get("retry-after") or "") if exc.headers else ""
        except Exception:  # noqa: BLE001
            retry_after = ""
        evidence = quota_error({
            "status_code": 429,
            "retry_after": retry_after,
            "message": detail[:500],
        })
        raise DeepSeekRateLimited(evidence or {"kind": "provider_quota", "reset_at": None}, detail[:500])
    if status in (401, 403):
        raise DeepSeekRefused("deepseek_unauthorized", f"HTTP {status}: {detail[:300]}")
    if status == 402:
        raise DeepSeekRefused("deepseek_insufficient_balance", f"HTTP {status}: {detail[:300]}")
    raise DeepSeekRefused(f"deepseek_http_{status}", f"HTTP {status}: {detail[:300]}")


class StubTransport:
    """Offline canned replies (``AGENT_HUB_DEEPSEEK_STUB``).

    ``ok``        - two deltas, a finish_reason and a usage chunk;
    ``quota``     - raises the 429 path without touching the network;
    ``unauthorized`` - raises the 401 path;
    ``truncated`` - deltas then the stream simply stops (no finish_reason);
    ``empty``     - nothing at all.
    """

    SCENARIOS = ("ok", "quota", "unauthorized", "truncated", "empty")

    def __init__(self, scenario: str = "ok") -> None:
        self.scenario = scenario if scenario in self.SCENARIOS else "ok"
        self.payloads: list[dict] = []

    def __call__(self, payload: dict, headers: dict) -> Iterator[dict]:
        self.payloads.append({"payload": payload, "headers": {k: v for k, v in headers.items()
                                                             if k.lower() != "authorization"}})
        if self.scenario == "quota":
            raise DeepSeekRateLimited({"kind": "provider_quota", "reset_at": None}, "offline quota")
        if self.scenario == "unauthorized":
            raise DeepSeekRefused("deepseek_unauthorized", "offline 401")
        if self.scenario == "empty":
            return
        pieces = ["离线", "回复。"]
        for index, piece in enumerate(pieces):
            last = index == len(pieces) - 1
            # ``truncated`` must look like a stream that dies mid-reply: content
            # arrives, but the provider's own terminal marker never does.
            stopped = "stop" if last and self.scenario != "truncated" else None
            yield {
                "id": "stub-chunk",
                "model": "deepseek-chat-stub",
                "choices": [{
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": stopped,
                }],
            }
        if self.scenario == "truncated":
            return
        yield {"id": "stub-chunk", "model": "deepseek-chat-stub", "choices": [],
               "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15,
                         "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 11}}


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #

class DeepSeekAdapter(AdapterContract):
    """One streaming chat completion per Hub call."""

    name = "deepseek"
    version = "0.1.0"
    AGENT_IDS = AGENT_IDS

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        credentials_path: str,
        store: Optional[ExecutionStore] = None,
        transport: Optional[Callable[[dict, dict], Iterable[dict]]] = None,
        proxy: str = "",
        ca_bundle: str = "",
        timeout_seconds: float = 120.0,
        repo_root: str = "",
        stub: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model or "deepseek-chat"
        self.credentials_path = credentials_path
        self.proxy = proxy
        self.ca_bundle = ca_bundle
        self.timeout_seconds = timeout_seconds
        self.repo_root = repo_root
        self.stub = stub
        self.store = store or ExecutionStore(":memory:", table="deepseek_executions")
        self._transport = transport
        self._lock = threading.Lock()

    # -- capability & admission -------------------------------------------

    @property
    def url(self) -> str:
        # ``/v1`` is part of the documented base for some compatible servers;
        # accept both "https://api.deepseek.com" and ".../v1".
        return self.base_url + CHAT_PATH

    def capabilities(self) -> dict[str, Any]:
        return {
            "post_message": True,
            "stream": True,
            # Stateless HTTP: no thread to continue, and stopping means closing
            # the socket - not something we can confirm from outside the call.
            "resume_session": False,
            "cancel": False,
            "limit_dir": False,
            "intercept_tool": False,
            "control_subagent": False,
            "real_usage": True,
        }

    def health(self) -> bool:
        """True without a network round trip: a probe would spend quota to answer."""
        return True

    def execution_admission(self) -> ExecutionAdmission:
        """Strict is honest here: the request we build cannot act on this machine.

        Compare the Codex/Claude channels, which are blocked in strict mode
        because a local process can spawn children we do not control. This
        channel has no process, no tool definitions and no sub-agent channel, so
        there is nothing to control - the remaining risks (prompt egress to the
        provider, and no mid-turn cancel) are stated rather than hidden.
        """
        return ExecutionAdmission(
            True,
            "strict",
            "no_local_execution_channel",
            "DeepSeek 通道只发送一条用户消息，不携带工具、不启动本地进程、不能派生"
            "子 Agent；提示词会发送到 provider，且无法中途取消（中断按未确认处理）。",
        )

    def supervised_execution_admission(self) -> ExecutionAdmission:
        return self.execution_admission()

    def supports_native_resume(self) -> bool:
        return False

    def context_profile(self) -> dict:
        return {"adds_prompt_text": False, "observed": []}

    def binding_identity(self) -> str:
        """What an approval binds to: this model at this endpoint (no local binary)."""
        return f"deepseek:{self.model}@{_host_of(self.base_url)}"

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str] = None,
    ) -> AdapterStartResult:
        return self._run(invocation_id, snapshot, gate_token=gate_token, emit=None)

    def start_streaming(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str] = None,
        emit: Optional[Callable[[str, dict], None]] = None,
        **_: Any,
    ) -> AdapterStartResult:
        return self._run(invocation_id, snapshot, gate_token=gate_token, emit=emit)

    def _run(
        self,
        invocation_id: str,
        snapshot: InvocationSnapshot,
        *,
        gate_token: Optional[str],
        emit: Optional[Callable[[str, dict], None]],
    ) -> AdapterStartResult:
        # A launch without a consumed approval is a bug, not a mode: refuse loudly
        # and leave no record, so nothing looks like it ran.
        if not gate_token:
            return AdapterStartResult(
                adapter_invocation_id="",
                ok=False,
                terminal=True,
                detail="refused: missing_gate_token",
            )
        try:
            # The offline stub needs no credential; a real call always does.
            api_key = "" if self.stub else load_api_key(self.credentials_path, repo_root=self.repo_root)
        except DeepSeekRefused as exc:
            # No record was written: the kernel may then treat this as a known
            # zero rather than "unknown".
            return AdapterStartResult(
                adapter_invocation_id="", ok=False, terminal=True, detail=f"refused: {exc.code}"
            )

        payload = self._payload(snapshot)
        if set(payload) - ALLOWED_BODY_KEYS:  # defensive: never widen this channel
            return AdapterStartResult(
                adapter_invocation_id="", ok=False, terminal=True,
                detail="refused: unexpected_request_keys",
            )

        adapter_id = f"deepseek-{uuid.uuid4().hex[:12]}"
        self.store.record_start(invocation_id, adapter_id)
        started = time.monotonic()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        transport = self._transport_for(api_key)

        text_parts: list[str] = []
        usage: Optional[dict] = None
        reported_model: Optional[str] = None
        finish_reason: Optional[str] = None
        events = 0

        if emit is not None:
            _safe_emit(emit, STREAM_TURN_STARTED,
                       {"thread_id": adapter_id, "turn_id": adapter_id, "resumed": False})

        # ``stream_options`` is not implemented by every gateway in front of this
        # API, and a 400 bills nothing. So the very same call is retried once
        # without it, and the first error is kept: a 400 about something else
        # must never be reported as "a field was dropped and it worked".
        attempts = [payload]
        if "stream_options" in payload:
            attempts.append({k: v for k, v in payload.items() if k != "stream_options"})
        dropped: Optional[dict] = None
        try:
            for index, body_payload in enumerate(attempts):
                try:
                    for event in transport(body_payload, headers):
                        events += 1
                        if isinstance(event.get("model"), str) and event["model"]:
                            reported_model = event["model"]
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                        for choice in event.get("choices") or []:
                            if not isinstance(choice, dict):
                                continue
                            delta = choice.get("delta") or {}
                            piece = delta.get("content") if isinstance(delta, dict) else None
                            if isinstance(piece, str) and piece:
                                text_parts.append(piece)
                                if emit is not None:
                                    _safe_emit(emit, STREAM_DELTA, {"item_id": adapter_id, "text": piece})
                            if choice.get("finish_reason"):
                                finish_reason = str(choice["finish_reason"])
                    break
                except DeepSeekRefused as exc:
                    if exc.code == "deepseek_http_400" and index + 1 < len(attempts):
                        dropped = {
                            "first_error": exc.code,
                            "first_detail": redact(exc.detail)[:300],
                            "retried_without_stream_options": True,
                        }
                        continue
                    raise
        except DeepSeekRateLimited as exc:
            self._finish_failed(invocation_id, adapter_id, "rate_limited",
                                detail=exc.detail,
                                extra={"provider_quota": exc.evidence, **(dropped or {})},
                                elapsed=time.monotonic() - started)
            return AdapterStartResult(adapter_invocation_id=adapter_id, ok=False, terminal=True,
                                      detail=f"refused: deepseek_rate_limited")
        except DeepSeekRefused as exc:
            self._finish_failed(invocation_id, adapter_id, exc.code, detail=exc.detail,
                                extra=dropped, elapsed=time.monotonic() - started)
            return AdapterStartResult(adapter_invocation_id=adapter_id, ok=False, terminal=True,
                                      detail=f"refused: {exc.code}")
        except DeepSeekTransportError as exc:
            # Unknown, not failed: the request may have been accepted and billed.
            self._record_unknown(invocation_id, adapter_id, detail=str(exc),
                                 elapsed=time.monotonic() - started)
            return AdapterStartResult(adapter_invocation_id=adapter_id, ok=False, terminal=False,
                                      detail=f"unknown: transport_error")

        elapsed = time.monotonic() - started
        reply = "".join(text_parts)
        if finish_reason is None:
            # The stream ended without the provider's own terminal marker. Nothing
            # keeps running (we closed the socket), but the reply is incomplete, so
            # it settles as failed - never as a success with a truncated answer.
            self._finish_failed(invocation_id, adapter_id, "stream_incomplete",
                                detail="stream ended before finish_reason",
                                extra={"events": events, "chars": len(reply),
                                       "usage": usage, "model": reported_model},
                                elapsed=elapsed)
            if emit is not None:
                _safe_emit(emit, STREAM_TURN_ENDED,
                           {"turn_status": "incomplete", "verdict": "failed", "terminal": True})
            return AdapterStartResult(adapter_invocation_id=adapter_id, ok=False, terminal=True,
                                      detail="failed: stream ended before finish_reason")

        self.store.set_status(invocation_id, FINISHED, f"finished:{finish_reason}")
        self.store.set_payload(invocation_id, {
            # ``final_message`` is the key the kernel reads in
            # ``conversations.capture_reply``; without it a finished turn leaves no
            # assistant message, so a handoff downstream sees "no result".
            # ``reply`` stays for callers that read the adapter payload directly.
            "final_message": reply,
            "reply": reply,
            "finish_reason": finish_reason,
            "model": reported_model or self.model,
            "usage": usage,
            "events": events,
            "elapsed": round(elapsed, 3),
            "adapter_invocation_id": adapter_id,
        })
        if emit is not None:
            _safe_emit(emit, STREAM_TURN_ENDED,
                       {"turn_status": "completed", "verdict": "finished", "terminal": True})
        return AdapterStartResult(
            adapter_invocation_id=adapter_id, ok=True, terminal=True,
            detail=f"finished: {finish_reason}",
        )

    # -- helpers -----------------------------------------------------------

    def _payload(self, snapshot: InvocationSnapshot) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": snapshot.task_input}],
            "stream": True,
            # Without this the provider sends no usage at all, and the ledger
            # would record "unknown" for every call.
            "stream_options": {"include_usage": True},
        }
        return body

    def _transport_for(self, api_key: str) -> Callable[[dict, dict], Iterable[dict]]:
        if self._transport is not None:
            return self._transport
        if self.stub:
            return StubTransport(self.stub)
        return HttpTransport(self.url, api_key, proxy=self.proxy, ca_bundle=self.ca_bundle,
                             timeout=self.timeout_seconds)

    def _finish_failed(self, invocation_id: str, adapter_id: str, code: str, *,
                       detail: str, extra: Optional[dict] = None,
                       elapsed: float = 0.0) -> None:
        payload = {
            "error": code,
            "detail": redact(detail)[:400],
            "elapsed": round(elapsed, 3),
            "adapter_invocation_id": adapter_id,
        }
        payload.update(extra or {})
        self.store.set_status(invocation_id, FAILED, code)
        self.store.set_payload(invocation_id, payload)

    def _record_unknown(self, invocation_id: str, adapter_id: str, *,
                        detail: str, elapsed: float) -> None:
        # Status stays RUNNING on purpose: the external side cannot prove the
        # request never took effect, so nothing here may confirm a stop.
        self.store.set_payload(invocation_id, {
            "error": "transport_error",
            "detail": redact(detail)[:400],
            "elapsed": round(elapsed, 3),
            "adapter_invocation_id": adapter_id,
        })

    # -- stop / status -----------------------------------------------------

    def cancel(self, adapter_invocation_id: str) -> dict[str, Any]:
        # Honest refusal: a stream already sent cannot be interrupted from here.
        return {"ok": False, "reason": "unsupported",
                "detail": "chat completions 无法中途取消；请求已发出时只能停止读取"}

    def cancel_invocation(self, invocation_id: str) -> dict[str, Any]:
        record = self.store.get(invocation_id)
        return {"ok": False, "reason": "unsupported",
                "known": record is not None}

    def get_status(self, adapter_invocation_id: str) -> dict[str, Any]:
        for record in self.store.all_records():
            if record.get("adapter_invocation_id") == adapter_invocation_id:
                return {"ok": True, "status": record.get("status"),
                        "adapter_invocation_id": adapter_invocation_id}
        return {"ok": False, "reason": "unknown_adapter_invocation"}

    def lookup_by_invocation(self, invocation_id: str) -> Optional[dict[str, Any]]:
        return self.store.get(invocation_id)

    def find_invocation_for_adapter_id(self, adapter_invocation_id: str) -> Optional[str]:
        for record in self.store.all_records():
            if record.get("adapter_invocation_id") == adapter_invocation_id:
                return str(record["invocation_id"])
        return None

    # -- usage -------------------------------------------------------------

    def usage_report(self, invocation_id: str) -> Optional[dict[str, Any]]:
        """This call's usage for the ledger; ``None`` when nothing was launched."""
        record = self.store.get(invocation_id)
        if record is None:
            return None
        payload = record.get("payload") or {}
        usage = payload.get("usage")
        if not isinstance(usage, dict) or not usage:
            return {"mapping": MAPPING_NONE, "usage": {}, "source": "deepseek_no_usage",
                    "notes": ["no_usage_reported"], "model": payload.get("model")}
        return {
            "mapping": MAPPING_DEEPSEEK,
            "usage": usage,
            "source": "sse_final_chunk",
            "notes": [],
            "model": payload.get("model") or self.model,
        }


def _safe_emit(emit: Callable[[str, dict], None], etype: str, payload: dict) -> None:
    """A display hint must never break a paid call."""
    try:
        emit(etype, payload)
    except Exception:  # noqa: BLE001
        pass


def _host_of(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    return without_scheme.split("/", 1)[0]
