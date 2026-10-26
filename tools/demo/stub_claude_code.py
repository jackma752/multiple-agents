"""Stub 'claude' process for OFFLINE testing of the Claude Code adapter.

HAND-WRITTEN FROM DOCUMENTATION, NOT CAPTURED. The result object follows the
documented ``claude -p --output-format json`` shape (code.claude.com headless /
CLI reference and the Agent SDK result message, read 2026-10-23). No real
Claude Code output has been recorded on this machine, so every field here is a
documented assumption until the real test.

It is NOT Claude Code. It never touches the network and never reads credentials.

    stub_claude_code.py --scenario NAME <fixed argv tail>   (prompt on stdin)
    stub_claude_code.py --version | --help                  (zero-model identity check)

Scenarios: success-usage, success-no-usage, error-result, max-turns, malformed,
hang, nonzero-exit, success-nonzero.

If ``STUB_CLAUDE_MARKER`` is set, the stub appends one JSON line there with its
argv, the NAMES of selected environment variables, the cwd and the SHA-256 and
length of stdin - never the prompt itself.
"""

from __future__ import annotations

import hashlib
import datetime as dt
import json
import os
import sys
import time

SCENARIOS = ("success-usage", "success-no-usage", "error-result", "max-turns", "malformed",
             "hang", "nonzero-exit", "success-nonzero", "autopilot")
VERSION = "2.1.308 (Claude Code)"
HELP_FLAGS = ("-p, --print", "--output-format <format>", "--safe-mode", "--tools <tools...>",
              "--strict-mcp-config", "--disable-slash-commands", "--permission-mode <mode>",
              "--max-turns <turns>", "--max-budget-usd <amount>", "--no-session-persistence")
SESSION_ID = "5f0c4d3e-1a2b-4c5d-8e9f-00000000stub"
USAGE = {"input_tokens": 12, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
         "output_tokens": 3, "server_tool_use": {"web_search_requests": 0}, "service_tier": "standard"}


def result(**fields) -> dict:
    base = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200,
            "duration_api_ms": 1100, "num_turns": 1, "session_id": SESSION_ID,
            "permission_denials": [], "uuid": "00000000-0000-4000-8000-0000000000aa"}
    base.update(fields)
    return base


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(VERSION)
        return 0
    if argv[:1] == ["--help"]:
        print("Usage: claude [options] [command] [prompt]\n\nOptions:")
        for flag in HELP_FLAGS:
            print(f"  {flag}")
        return 0
    if len(argv) < 2 or argv[0] != "--scenario" or argv[1] not in SCENARIOS:
        print("stub: unknown invocation", file=sys.stderr)
        return 2
    scenario = argv[1]
    data = sys.stdin.buffer.read()
    marker = os.environ.get("STUB_CLAUDE_MARKER")
    if marker:
        names = sorted(k for k in os.environ if k.upper().startswith(("CLAUDE", "ANTHROPIC", "NODE_"))
                       or k.upper() in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "DISABLE_AUTOUPDATER"))
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "env_names": names, "cwd": os.getcwd(),
                                 "stdin_sha256": hashlib.sha256(data).hexdigest(),
                                 "stdin_bytes": len(data)}) + "\n")
    print(f"stub: scenario {scenario}", file=sys.stderr)

    if scenario == "autopilot":
        # Explicit offline demonstration, never a real quota observation.
        prompt = data.decode("utf-8", errors="replace")
        common = {"total_cost_usd": 0.0123, "usage": USAGE,
                  "modelUsage": {"claude-stub-model": {"inputTokens": 12, "outputTokens": 3}}}
        if "Review the results above" in prompt:
            reply = "VERDICT: PASS\nOffline review fixture passed."
        elif "Propose a plan" in prompt:
            reply = "1. claude-code: Prepare a task list\n2. claude-code: Check the result (after 1)"
        elif "DEMO_QUOTA" in prompt and "Continue the interrupted stage step" not in prompt:
            emit(result(is_error=True, subtype="error_during_execution", result="usage limit reached (offline fixture)",
                        reset_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=5)).isoformat(), **common))
            return 1
        elif "DEMO_RETRY" in prompt and "Repair attempt" not in prompt:
            emit(result(is_error=True, subtype="error_during_execution", result="temporary fixture error", **common))
            return 1
        else:
            reply = "Offline stage step completed."
        emit(result(result=reply, **common))
        return 0

    if scenario == "hang":
        time.sleep(120)
        return 0
    if scenario == "success-usage":
        emit(result(result="OK", total_cost_usd=0.0123, usage=USAGE,
                    modelUsage={"claude-stub-model": {"inputTokens": 12, "outputTokens": 3}}))
        return 0
    if scenario == "success-no-usage":
        emit(result(result="OK"))
        return 0
    if scenario == "error-result":
        # Documented: a failure inside the run is printed as the result on stdout.
        emit(result(is_error=True, result="Not logged in · Please run /login", num_turns=0))
        return 1
    if scenario == "max-turns":
        emit(result(subtype="error_max_turns", is_error=True, errors=["Reached maximum number of turns (1)"],
                    total_cost_usd=0.01, usage=USAGE))
        return 1
    if scenario == "malformed":
        sys.stdout.write('{"type": "result", "subtype": "success", "result": "O')
        return 0
    if scenario == "nonzero-exit":
        print("error: unknown option '--stub-flag'", file=sys.stderr)
        return 1
    if scenario == "success-nonzero":
        emit(result(result="OK", total_cost_usd=0.0123, usage=USAGE))
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
