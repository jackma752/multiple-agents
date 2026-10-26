#!/usr/bin/env python3
"""Find out what interface a local service offers, before writing an adapter for it.

The immediate question: can a Hub adapter point at **DSH Desktop** (the app at
``E:\\ds_agent\\DSH Desktop``) instead of ``api.deepseek.com``? That needs an
interface, and "it has a GUI on port 43129" is not one. So this probe answers, in
order:

1. is anything listening on the port;
2. does ``GET /`` look like the DSH web GUI (its boot marker / asset paths);
3. is there an OpenAI-compatible model list (``/v1/models`` …);
4. with ``--fetch-assets``: do the served JS bundles mention chat/completion
   routes at all.

Rules this file follows, because the rest of the project does:

* **read-only** - only ``GET``; no prompt is ever sent, so nothing is billed and
  no model turn happens;
* **no credentials** - no ``Authorization`` header is constructed; a 401 is a
  useful answer and is reported as such;
* **nothing is written** - no files, no database rows, no env changes;
* **honest output** - "cannot connect", "answered 404" and "answered 200 with an
  unknown shape" are three different lines, and none of them is reported as
  "supported" until the shape is actually recognised.

Usage:

    python tools/probes/deepseek_endpoint.py
    python tools/probes/deepseek_endpoint.py --port 43129 --fetch-assets
    python tools/probes/deepseek_endpoint.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import urllib.error
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORTS = (43129, 8080)
MODEL_PATHS = ("/v1/models", "/models", "/api/v1/models")
TIMEOUT = 3.0
ASSET_LIMIT = 3
ASSET_BYTES = 2 * 1024 * 1024

#: Strings that would mean "this service can run a chat turn for us".
ROUTE_HINTS = (
    "/chat/completions",
    "chat/completions",
    "/v1/chat",
    "/v1/models",
    "completions",
)

_ASSET_RE = re.compile(r'(?:src|href)="([^"]+\.(?:js|mjs))"', re.IGNORECASE)


def _request(url: str, limit: int = 4096) -> dict:
    """One GET. Returns a plain dict so the caller never has to guess."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json, text/html;q=0.5")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - localhost only
            body = resp.read(limit).decode("utf-8", "replace")
            return {
                "url": url,
                "outcome": "answered",
                "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
                "body_head": body,
            }
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(min(limit, 512)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is a bonus, never required
            pass
        return {
            "url": url,
            "outcome": "answered",
            "status": exc.code,
            "content_type": exc.headers.get("content-type", "") if exc.headers else "",
            "body_head": body,
        }
    except urllib.error.URLError as exc:
        return {"url": url, "outcome": "unreachable", "detail": str(exc.reason)}
    except (TimeoutError, socket.timeout):
        return {"url": url, "outcome": "timeout"}
    except Exception as exc:  # noqa: BLE001 - never crash the probe on one port
        return {"url": url, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"}


def _model_names(result: dict) -> list:
    """Extract ``data[].id`` from an OpenAI-style model list, if that is what it is."""
    text = result.get("body_head") or ""
    if not text.strip().startswith("{"):
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [str(item.get("id")) for item in data
                if isinstance(item, dict) and item.get("id")]
    models = payload.get("models")
    if isinstance(models, list):
        return [str(m) for m in models]
    return []


def _looks_like_dsh(html: str) -> bool:
    lowered = html.lower()
    return "__dsh_boot__" in lowered or "deepseek-harness" in lowered or "harness" in lowered


def _assets(html: str) -> list:
    return sorted(set(_ASSET_RE.findall(html or "")))


def _scan_assets(base: str, assets: list) -> list:
    """Fetch a few bundles and count the route-ish strings they contain."""
    found = []
    for asset in assets[:ASSET_LIMIT]:
        url = asset if asset.startswith("http") else base.rstrip("/") + "/" + asset.lstrip("/")
        result = _request(url, limit=ASSET_BYTES)
        hits = {}
        if result["outcome"] == "answered" and result["status"] == 200:
            body = result["body_head"]
            for hint in ROUTE_HINTS:
                count = body.count(hint)
                if count:
                    hits[hint] = count
        found.append({"asset": url, "outcome": result["outcome"],
                      "status": result.get("status"), "hits": hits,
                      "bytes": len(result.get("body_head") or "")})
    return found


def probe(host: str, ports: tuple, fetch_assets: bool = False,
          extra_paths: tuple = ()) -> dict:
    report = {"host": host, "ports": list(ports), "findings": [], "assets": []}
    for port in ports:
        base = f"http://{host}:{port}"
        root = _request(base + "/")
        root["role"] = "root"
        if root["outcome"] == "answered" and root["status"] == 200:
            root["dsh_gui"] = _looks_like_dsh(root.get("body_head", ""))
            root["assets"] = _assets(root.get("body_head", ""))
        report["findings"].append(root)
        for path in tuple(MODEL_PATHS) + tuple(extra_paths):
            result = _request(base + path)
            result["role"] = "models"
            result["model_names"] = _model_names(result)
            report["findings"].append(result)
        if fetch_assets and root.get("assets"):
            report["assets"].extend(_scan_assets(base, root["assets"]))
    return report


def render(report: dict) -> str:
    lines = []
    for item in report["findings"]:
        if item["outcome"] == "answered":
            extra = ""
            names = item.get("model_names") or []
            if names:
                extra = "  models=" + ", ".join(names[:6])
            if item.get("dsh_gui"):
                extra += "  (DSH web GUI)"
            lines.append(f"  [{item['status']}] {item['url']}  {item['content_type']}{extra}")
            if item["status"] != 200:
                body = (item.get("body_head") or "").strip().replace("\n", " ")[:200]
                if body:
                    lines.append(f"         body: {body}")
        else:
            lines.append(f"  [--]  {item['url']}  {item['outcome']}"
                         + (f" ({item['detail']})" if item.get("detail") else ""))
    for asset in report.get("assets", []):
        hits = asset.get("hits") or {}
        if hits:
            shown = ", ".join(f"{k}×{v}" for k, v in hits.items())
            lines.append(f"  asset {asset['asset']}  [{asset.get('status')}, "
                         f"{asset['bytes']}B]  {shown}")
        else:
            lines.append(f"  asset {asset['asset']}  [{asset.get('outcome')}, "
                         f"{asset['bytes']}B]  no chat route strings")
    root_assets = []
    for item in report["findings"]:
        root_assets.extend(item.get("assets") or [])
    if root_assets:
        lines.append("  gui assets: " + ", ".join(root_assets[:6]))
    return "\n".join(lines)


def summary(report: dict) -> str:
    findings = report["findings"]
    answered = [f for f in findings if f["outcome"] == "answered"]
    with_models = [f for f in answered if f.get("model_names")]
    gui = [f for f in answered if f.get("dsh_gui")]
    hits = {k: v for a in report.get("assets", []) for k, v in (a.get("hits") or {}).items()}

    if with_models:
        names = sorted({n for f in with_models for n in f["model_names"]})
        return (f"an OpenAI-compatible model list answered; ids: {', '.join(names[:10])}"
                "\n=> a chat-completions adapter can point at this base URL.")
    if gui:
        tail = ""
        if hits:
            tail = ("\n   its bundle mentions: " + ", ".join(sorted(hits))
                    + " - worth opening the real route names, but a model list did not answer")
        return ("this is the DSH Desktop web GUI, and it did **not** answer as an "
                "OpenAI-compatible endpoint." + tail
                + "\n=> pointing an adapter at it is NOT supported by this evidence.")
    if answered:
        statuses = sorted({str(f["status"]) for f in answered})
        return ("something answered (status " + ", ".join(statuses) + ") but no OpenAI-style "
                "model list was found.\n=> not enough evidence to point an adapter here.")
    return ("nothing answered on the probed ports.\n"
            "=> no evidence of an HTTP interface; do not assume one exists.")


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, action="append", default=None,
                        help=f"port to probe (default: {', '.join(map(str, DEFAULT_PORTS))})")
    parser.add_argument("--fetch-assets", action="store_true",
                        help="also GET up to 3 served JS bundles and look for chat routes")
    parser.add_argument("--path", action="append", default=None,
                        help="extra path to GET (repeatable), e.g. --path /openapi.json")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    ports = tuple(args.port) if args.port else DEFAULT_PORTS
    report = probe(args.host, ports, fetch_assets=args.fetch_assets,
                   extra_paths=tuple(args.path or ()))
    if args.json:
        print(json.dumps(report, indent=2)[:20000])
        return 0

    print(f"probing {args.host} on ports {', '.join(map(str, ports))} "
          f"(GET only, no prompts, no credentials)\n")
    print(render(report))
    print("\nsummary: " + summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
