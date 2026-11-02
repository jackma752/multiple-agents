"""Bounded HTTP GET for paper tools.

Every hop (the first URL and each redirect target) is checked by the caller's
``allowed(url) -> (ok, why)`` before it is contacted, so a source cannot
redirect a download to loopback, a LAN address or an unapproved host. Bodies are
capped at ``max_bytes``; nothing is executed or rendered.
"""

from __future__ import annotations

import socket
import ssl
import gzip
import io
import zlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

from agenthub.papers import MAX_REDIRECTS

USER_AGENT = "AgentHub-PaperResearch/0.1 (local research assistant)"


@dataclass
class FetchResult:
    ok: bool
    status: Optional[int] = None
    content_type: str = ""
    body: bytes = b""
    final_url: str = ""
    error: str = ""  # machine code when not ok
    hops: list = field(default_factory=list)
    wire_bytes: int = 0
    encoding: str = "identity"


def resolve_host(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


class HttpFetcher:
    """Real network access; used only when a tool step is allowed to run."""

    simulated = False

    def __init__(self, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        context = ssl.create_default_context()
        try:
            import certifi

            context.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - the system store stays in use
            pass
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def resolve(self, host: str) -> list[str]:
        return resolve_host(host)

    def fetch(self, url: str, *, allowed: Callable[[str], tuple], max_bytes: int,
              accept: str = "*/*") -> FetchResult:
        hops: list = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ok, why = allowed(current)
            hops.append({"url": current, "allowed": ok, "why": why})
            if not ok:
                return FetchResult(False, error=why, final_url=current, hops=hops)
            request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                                               "Accept-Encoding": "gzip"})
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                location = exc.headers.get("Location")
                exc.close()
                if status in (301, 302, 303, 307, 308) and location:
                    current = urljoin(current, location)
                    continue
                return FetchResult(False, status=status, error=f"http_{status}", final_url=current, hops=hops)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return FetchResult(False, error="unreachable:" + type(exc).__name__, final_url=current, hops=hops)
            with response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    return FetchResult(False, status=response.status, error="too_large", final_url=current, hops=hops)
                size = len(body)
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding == "gzip":
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as decoded:
                            body = decoded.read(max_bytes + 1)
                    except (OSError, EOFError, ValueError, zlib.error):
                        return FetchResult(False, status=response.status, error="bad_content_encoding", final_url=current, hops=hops)
                    if len(body) > max_bytes:
                        return FetchResult(False, status=response.status, error="too_large", final_url=current, hops=hops)
                elif encoding not in ("identity", ""):
                    return FetchResult(False, status=response.status, error="unsupported_content_encoding", final_url=current, hops=hops)
                return FetchResult(True, status=response.status,
                                   content_type=response.headers.get("Content-Type", ""),
                                   body=body, final_url=current, hops=hops, wire_bytes=size, encoding=encoding or "identity")
        return FetchResult(False, error="too_many_redirects", final_url=current, hops=hops)


class FakeFetcher:
    """Offline fetcher for tests and demos: ``routes(url) -> (status, content_type, body) | None``.

    Follows the same per-hop checks as :class:`HttpFetcher`; ``calls`` records
    every URL actually contacted (after its check passed).
    """

    simulated = True

    def __init__(self, routes: Callable[[str], Optional[tuple]], *,
                 addresses: Optional[dict] = None) -> None:
        self.routes = routes
        self.addresses = addresses or {}
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[str]:
        if host in self.addresses:
            return list(self.addresses[host])
        return ["93.184.216.34"]  # a public documentation-range stand-in

    def fetch(self, url: str, *, allowed: Callable[[str], tuple], max_bytes: int,
              accept: str = "*/*") -> FetchResult:
        hops: list = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            ok, why = allowed(current)
            hops.append({"url": current, "allowed": ok, "why": why})
            if not ok:
                return FetchResult(False, error=why, final_url=current, hops=hops)
            self.calls.append(current)
            answer = self.routes(current)
            if answer is None:
                return FetchResult(False, error="unreachable:fixture", final_url=current, hops=hops)
            status, content_type, body = answer
            if status in (301, 302, 303, 307, 308):
                current = urljoin(current, body.decode("utf-8") if isinstance(body, bytes) else body)
                continue
            if status != 200:
                return FetchResult(False, status=status, error=f"http_{status}", final_url=current, hops=hops)
            if len(body) > max_bytes:
                return FetchResult(False, status=status, error="too_large", final_url=current, hops=hops)
            return FetchResult(True, status=status, content_type=content_type, body=body, final_url=current,
                               hops=hops)
        return FetchResult(False, error="too_many_redirects", final_url=current, hops=hops)
