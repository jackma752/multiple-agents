"""Stub 'codex' process for OFFLINE integration testing.

It emits exactly the JSONL stream captured from the verified real probe, so the
whole stack - API, Worker, approval gate, quota gate, subprocess machinery,
event parsing, console rendering - can be exercised end to end without invoking
Codex and without spending quota.

It is NOT Codex. It never touches the network.

Behaviour is switched by the prompt text so a demo can show several outcomes:

  * prompt containing "FAIL"   -> emits turn.failed and exits 1
  * prompt containing "SLEEP"  -> hangs (to exercise the adapter timeout)
  * anything else              -> emits the verified success stream and exits 0

It also writes a marker file next to itself recording the argv it was given, so
the integration can assert what the adapter actually launched.
"""

from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
# Log under var/ (gitignored) so the repository stays clean.
ARGV_LOG = os.environ.get(
    "STUB_CODEX_ARGV_LOG", os.path.join(REPO, "var", "stub-codex-argv.log")
)

THREAD_ID = "stub-thread-0001"
USAGE = {
    "input_tokens": 14481,
    "cached_input_tokens": 12544,
    "cache_write_input_tokens": 0,
    "output_tokens": 5,
    "reasoning_output_tokens": 0,
}


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[-1] if argv else ""
    env_keys = sorted(k for k in os.environ if "PROXY" in k.upper() or k in ("CODEX_HOME", "CODEX_CA_CERTIFICATE"))
    with open(ARGV_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "env_keys": env_keys}) + "\n")

    if "SLEEP" in prompt.upper():
        print("stub: sleeping to trigger the adapter timeout", flush=True)
        time.sleep(120)
        return 0

    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})

    if "FAIL" in prompt.upper():
        emit({"type": "error", "message": "stub: simulated turn failure"})
        emit({"type": "turn.failed", "error": {"message": "stub: simulated turn failure"}})
        return 1

    emit({"type": "item.completed",
          "item": {"id": "item_0", "type": "agent_message", "text": "OK"}})
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


if __name__ == "__main__":
    sys.exit(main())
