"""Stub 'codex' process for OFFLINE integration testing.

It emits a JSONL stream with the same shape as the stream captured from the
verified real probe, so the whole stack - API, Worker, approval gate, quota gate,
subprocess machinery, event parsing, console rendering - can be exercised end to
end without invoking Codex and without spending quota.

It is NOT Codex. It never touches the network.

Behaviour is switched by the prompt text for the demos:

  * prompt containing "FAIL"   -> emits turn.failed and exits 1
  * prompt containing "SLEEP"  -> hangs (to exercise the adapter timeout)
  * anything else              -> emits the verified success stream and exits 0

For the capability PROBES (`tools/probes/capability_probe.py`) the outcome comes
from the ``STUB_CODEX_PROBE_OUTCOME`` environment variable instead of the prompt,
so the prompt a reviewer reads is byte-identical in stub and real mode: the probe
tool sets only the environment, never the text.

It also writes a marker file recording the argv it was given, so the integration
can assert what the adapter actually launched.
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

#: File the read-only probe asks for. The stub creates it only when simulating a
#: sandbox that does NOT refuse writes.
PROBE_FILE = "PROBE_WRITE.txt"


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def workdir_from_argv(argv: list[str]) -> str:
    """The directory the real CLI would treat as the working root (`-C DIR`)."""
    if "-C" in argv:
        index = argv.index("-C")
        if index + 1 < len(argv):
            return argv[index + 1]
    return os.getcwd()


def message(text: str) -> None:
    emit({"type": "item.completed",
          "item": {"id": "item_0", "type": "agent_message", "text": text}})


def main() -> int:
    argv = sys.argv[1:]
    prompt = argv[-1] if argv else ""
    env_keys = sorted(k for k in os.environ if "PROXY" in k.upper() or k in ("CODEX_HOME", "CODEX_CA_CERTIFICATE"))
    with open(ARGV_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "env_keys": env_keys}) + "\n")

    probe = os.environ.get("STUB_CODEX_PROBE_OUTCOME", "").strip().lower()

    if probe == "slow" or (not probe and "SLEEP" in prompt.upper()):
        print("stub: sleeping to trigger the adapter timeout", flush=True)
        time.sleep(120)
        return 0

    emit({"type": "thread.started", "thread_id": THREAD_ID})
    emit({"type": "turn.started"})

    if probe == "refuse":
        # A sandbox that refuses the write, and an agent that reports the refusal.
        emit({"type": "error",
              "message": "sandbox: read-only filesystem; write to PROBE_WRITE.txt denied"})
        message("REFUSED")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "write":
        # A sandbox that does NOT refuse: the file really appears.
        target = os.path.join(workdir_from_argv(argv), PROBE_FILE)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("hello\n")
        except OSError as exc:  # pragma: no cover - only on an unusable workdir
            print(f"stub: could not write probe file: {exc}", flush=True)
        message("WROTE")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "lie":
        # Claims success while nothing happened: contradictory evidence.
        message("WROTE")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "spawn":
        emit({"type": "thread.started", "thread_id": "stub-subagent-0002"})
        emit({"type": "item.completed",
              "item": {"id": "item_1", "type": "subagent_started", "text": "SUB"}})
        message("SPAWNED")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "no-spawn":
        message("NO-SUBAGENT")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "command":
        emit({"type": "item.completed",
              "item": {"id": "item_1", "type": "command_execution",
                       "command": "echo probe-ok", "exit_code": 0}})
        message("RAN")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if probe == "approval":
        emit({"type": "approval_request",
              "item": {"type": "command_execution", "command": "echo probe-ok"}})
        message("BLOCKED")
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if "FAIL" in prompt.upper():
        emit({"type": "error", "message": "stub: simulated turn failure"})
        emit({"type": "turn.failed", "error": {"message": "stub: simulated turn failure"}})
        return 1

    message("OK")
    emit({"type": "turn.completed", "usage": USAGE})
    return 0


if __name__ == "__main__":
    sys.exit(main())
