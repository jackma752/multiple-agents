"""One raw POST to the DeepSeek endpoint, with everything printed.

The adapter reports ``HTTP 400`` with an **empty** body, which means the refusal
did not come from a JSON error document. This tool answers "who said 400?" by
printing the status, a few response headers (``server``, ``content-type``,
``via``…) and the raw body, for one or two request variants.

    python tools/demo/deepseek_probe.py                 # proxy from config
    python tools/demo/deepseek_probe.py --no-proxy      # direct connection
    python tools/demo/deepseek_probe.py --no-stream-options

A 400 costs nothing; a 200 costs a few tokens (the reply is not stored). The key
is read from the credential file, never printed, and never echoed in errors.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _sub in ("packages/contracts", "packages/adapters", "apps/worker"):
    _p = os.path.join(REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

INTERESTING_HEADERS = ("server", "content-type", "content-length", "via", "x-ds-trace-id",
                       "cf-ray", "date", "connection", "transfer-encoding")


def _mask(key: str) -> str:
    return f"{key[:5]}…{key[-4:]} ({len(key)} chars)" if len(key) > 9 else "***"


def main(argv=None) -> int:
    from agenthub_kernel import config  # noqa: PLC0415 - after sys.path setup
    from agenthub_adapters.deepseek import load_api_key  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Probe the DeepSeek chat endpoint once.")
    parser.add_argument("--no-proxy", action="store_true", help="ignore the configured proxy")
    parser.add_argument("--no-stream-options", action="store_true",
                        help="omit stream_options (the field the adapter now retries without)")
    parser.add_argument("--prompt", default="你好", help="user message to send")
    parser.add_argument("--model", default="", help="override the model id")
    args = parser.parse_args(argv)

    settings = config.deepseek_settings()
    url = settings["base_url"].rstrip("/") + "/chat/completions"
    model = args.model or settings["model"]
    proxy = "" if args.no_proxy else (settings["proxy"] or "")
    ca_bundle = settings["ca_bundle"] or ""

    try:
        api_key = load_api_key(settings["credentials_path"], repo_root=config.REPO_ROOT)
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        print(f"credential: unusable -> {exc}")
        return 2

    payload = {"model": model, "messages": [{"role": "user", "content": args.prompt}], "stream": True}
    if not args.no_stream_options:
        payload["stream_options"] = {"include_usage": True}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy
                                            else {})]
    context = ssl.create_default_context(cafile=ca_bundle or None)
    handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)

    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "text/event-stream")
    request.add_header("Authorization", f"Bearer {api_key}")

    print(f"url      : {url}")
    print(f"model    : {model}")
    print(f"proxy    : {proxy or '(none, direct)'}")
    print(f"ca       : {ca_bundle or '(system)'}")
    print(f"key      : {_mask(api_key)}")
    print(f"body     : {json.dumps(payload, ensure_ascii=False)[:200]}")
    print()

    try:
        with opener.open(request, timeout=float(settings["timeout_seconds"])) as response:
            status = response.status
            headers = dict(response.headers)
            first = b""
            for raw in response:
                first = raw
                break
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = dict(exc.headers or {})
        try:
            first = exc.read(4000)
        except Exception as read_exc:  # noqa: BLE001
            first = f"<body unreadable: {read_exc!r}>".encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - connection level problems land here
        print(f"transport failure: {type(exc).__name__}: {getattr(exc, 'reason', exc)}")
        return 3

    print(f"status   : {status}")
    for name in INTERESTING_HEADERS:
        for key, value in headers.items():
            if key.lower() == name:
                print(f"header   : {key}: {value}")
    text = first.decode("utf-8", "replace")
    print(f"body     : {text[:600]!r}")
    return 0 if status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
