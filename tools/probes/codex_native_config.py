"""Explicit zero-model check of agents.enabled on the pinned real app-server.

Uses a new empty home, no credential reads, no thread/start or turn/start.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import uuid

from agenthub_adapters.appserver import AppServerClient, AppServerSession, StdioTransport
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER, STRICT_APPSERVER_ARGV_TAIL, verify_native_disabled
from agenthub_adapters.codex import build_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--approve-zero-model-check", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    pin = PINNED_APPSERVER
    executable = Path(pin["path"])
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        raise SystemExit("unreviewed executable; nothing started")
    scratch = Path("E:/plug/.native-strict-probes") / ("codex-" + uuid.uuid4().hex)
    scratch.mkdir(parents=True)
    new_home = scratch / "empty-home"
    new_home.mkdir()
    source = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OPENAI", "CODEX", "AGENT_HUB"))}
    env = build_env(codex_home=str(new_home), proxy="http://127.0.0.1:1", base_env=source)
    client = AppServerClient(StdioTransport([str(executable), *STRICT_APPSERVER_ARGV_TAIL],
        env=env, cwd=str(scratch), stderr_path=str(scratch / "stderr.txt")), request_timeout=10)
    result = {"binary_sha256": digest, "real_model_requests": 0, "thread_starts": 0,
              "turn_starts": 0, "scratch": str(scratch)}
    try:
        client.start()
        AppServerSession(client).initialize()
        readback = client.request("config/read", {"includeLayers": False, "cwd": str(scratch)})
        result["agents_enabled"] = (readback.get("config", {}).get("agents") or {}).get("enabled")
        result["disabled_confirmed"] = verify_native_disabled(readback)
        # Only this fresh empty home's configuration is saved, never the user's.
        (scratch / "config-read.json").write_text(json.dumps(readback, indent=2), encoding="utf-8")
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["disabled_confirmed"] = False
    finally:
        client.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["disabled_confirmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
