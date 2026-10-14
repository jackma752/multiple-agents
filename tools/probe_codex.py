r"""Probe a locally installed Codex CLI and record REAL capability evidence.

The plan (§2) requires each adapter to submit a capability table whose entries
come from actual self-description or captures - never from memory. This script
regenerates that evidence reproducibly and writes it to
``var/probe-codex.json``.

It performs **no model invocation and no agent task**: it only runs the CLI's
own ``--version``, ``--help`` and ``features list``, which are local
self-description. Nothing here contacts a model or runs a task.

Usage:

    .\.venv\Scripts\python.exe tools\probe_codex.py
    .\.venv\Scripts\python.exe tools\probe_codex.py --exe <path-to-codex.exe>
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_GLOBS = [
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "OpenAI", "Codex", "bin", "*", "codex.exe",
    ),
]


def find_codex(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for pattern in DEFAULT_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]  # newest version dir
    return None


def run(exe: str, args: list[str], timeout: int = 60) -> dict:
    """Run the CLI and capture output.

    NOTE: stdout is captured to a FILE rather than a pipe. Piping a native
    process through PowerShell is denied in this sandbox, and redirection
    through PowerShell silently yields empty files; Python's own subprocess
    with a file handle works (see docs/tooling-diagnostics.md §2).
    """
    out_path = os.path.join(REPO_ROOT, "var", "_probe_out.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", errors="replace") as fh:
        try:
            proc = subprocess.run(
                [exe, *args], stdout=fh, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )
            code = proc.returncode
        except Exception as exc:  # noqa: BLE001
            return {"args": args, "exit": None, "error": f"{type(exc).__name__}: {exc}", "text": ""}
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    return {"args": args, "exit": code, "text": text}


def parse_features(text: str) -> dict[str, dict]:
    """Parse `codex features list` -> {name: {stage, enabled}}."""
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        enabled = parts[-1].lower() in ("true", "false")
        if not enabled and parts[-1].lower() not in ("true", "false"):
            continue
        # stage is everything between the name and the boolean
        stage = " ".join(parts[1:-1])
        out[name] = {"stage": stage, "enabled": parts[-1].lower() == "true"}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe a local Codex CLI (no model calls).")
    parser.add_argument("--exe", default=None, help="path to codex.exe")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "var", "probe-codex.json"))
    args = parser.parse_args()

    exe = find_codex(args.exe)
    if not exe:
        print("codex.exe not found. Pass --exe <path>.")
        return 2

    print(f"probing: {exe}")
    result = {
        "exe": exe,
        "note": "self-description only: no model call, no agent task was run",
        "version": run(exe, ["--version"]),
        "help": run(exe, ["--help"]),
        "exec_help": run(exe, ["exec", "--help"]),
        "features_list": run(exe, ["features", "list"]),
    }
    result["features"] = parse_features(result["features_list"]["text"])

    # The flags that matter for the plan's hard conditions.
    top_help = result["help"]["text"]
    exec_help = result["exec_help"]["text"]
    feats = result["features"]

    interesting = [
        ("multi_agent", "internal sub-agents / fan-out"),
        ("multi_agent_v2", "internal sub-agents (v2)"),
        ("enable_fanout", "fan-out (sub-agent spawning)"),
        ("agent_message_board", "agent message board"),
        ("guardian_approval", "approval routing"),
        ("exec_permission_approvals", "per-exec permission approvals"),
        ("write_stdin_approval", "write_stdin approval"),
        ("request_permissions_tool", "model can request permissions"),
        ("experimental_windows_sandbox", "Windows sandbox"),
        ("windows_sandbox_service", "Windows sandbox service"),
        ("network_proxy", "network egress control"),
        ("hooks", "hooks"),
    ]
    result["plan_relevant_features"] = {
        name: {"why": why, **(feats.get(name) or {"stage": "absent", "enabled": None})}
        for name, why in interesting
    }
    result["plan_relevant_flags"] = {
        "top_level_has_ask_for_approval": "--ask-for-approval" in top_help,
        "exec_has_ask_for_approval": "--ask-for-approval" in exec_help,
        "exec_has_json_events": "--json" in exec_help,
        "exec_has_sandbox": "--sandbox" in exec_help,
        "exec_has_cd": "--cd" in exec_help,
        "exec_has_add_dir": "--add-dir" in exec_help,
        "exec_has_output_last_message": "--output-last-message" in exec_help,
        "exec_has_resume": "resume" in exec_help,
        "exec_has_ephemeral": "--ephemeral" in exec_help,
        "top_level_has_app_server": "app-server" in top_help,
        "top_level_has_mcp": "mcp" in top_help,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    ver = "?"
    for line in (result["version"]["text"] or "").splitlines():
        if "codex-cli" in line:
            ver = line.strip()
            break
    print(f"version: {ver}")
    print(f"features parsed: {len(feats)}")
    for name, info in result["plan_relevant_features"].items():
        print(f"  {name:34s} stage={info['stage']:<20s} enabled={info['enabled']}")
    print(f"flags: {json.dumps(result['plan_relevant_flags'], ensure_ascii=False)}")
    print(f"written: {os.path.relpath(args.out, REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
