r"""Run ONE real Codex call and record a structured result.

Built for the P3 controlled-integration verification, to be run by the operator
on a machine (or terminal) where the OpenAI endpoints are reachable. This script
itself is deliberately conservative:

* runs exactly ONE invocation and NEVER retries;
* read-only sandbox and native sub-agents disabled by default;
* writes a machine-readable result next to the raw capture, so the evidence can
  be reviewed without pasting credentials anywhere;
* does not print the contents of any auth file it copies.

Why a script and not a one-liner: this environment blocks chatgpt.com at the
network layer (see docs/agent-codex-capabilities.md §5.4), so the call has to be
performed where the network permits it; the result still has to land in the same
shape so the capability table can be filled in.

Usage
-----
    # 1) dedicated, writable HOME (keeps Codex from writing ~/.codex/tmp).
    #    Defaults OUTSIDE the repo because it receives a credential copy.
    .\.venv\Scripts\python.exe tools\codex_one_call.py --init-home "$env:LOCALAPPDATA\AgentHub\codex-home"

    # 2) the single call (uses the same default home, outside the repo)
    .\.venv\Scripts\python.exe tools\codex_one_call.py --prompt "Reply with exactly: OK. Do not modify any files."

Exit codes: 0 the call succeeded, 1 the call failed, 2 setup/config problem.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CODEX_GLOB = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe"
)
DEFAULT_CODEX_HOME = os.path.join(os.environ["USERPROFILE"], ".codex")


def default_home() -> str:
    """Where this tool keeps CODEX_HOME by default.

    OUTSIDE the repository on purpose: this directory receives a copy of the
    Codex login credential, and nothing holding a credential belongs under the
    repo tree - a stray ``git add -f``, an archive or a backup would leak it.
    """
    override = os.environ.get("AGENT_HUB_CODEX_HOME")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AgentHub", "codex-home")

# Sub-agent feature that must stay off for a controlled single call.
SUBAGENT_FEATURE = "multi_agent"


def find_codex(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    hits = sorted(glob.glob(DEFAULT_CODEX_GLOB))
    return hits[-1] if hits else None


def init_home(home: str) -> int:
    """Create the dedicated CODEX_HOME and carry auth over (contents untouched)."""
    os.makedirs(home, exist_ok=True)
    probe = os.path.join(home, ".write-probe")
    try:
        with open(probe, "w", encoding="ascii") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        print(f"ERROR: {home} is not writable: {exc}")
        return 2

    src = os.path.join(DEFAULT_CODEX_HOME, "auth.json")
    dst = os.path.join(home, "auth.json")
    if os.path.exists(src) and not os.path.exists(dst):
        shutil.copy2(src, dst)
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
        print(f"copied auth.json into {home} ({os.path.getsize(dst)} bytes; contents not shown)")
    print(f"CODEX_HOME ready: {home}")
    print("NOTE: this directory holds a credential copy. Keep it OUTSIDE the repo")
    print("      (and gitignored if it ever lands inside), and delete auth.json")
    print("      when you are done verifying.")
    return 0


def run_one(args: argparse.Namespace) -> int:
    codex = find_codex(args.codex)
    if not codex:
        print("ERROR: codex.exe not found; pass --codex")
        return 2

    home = os.path.abspath(args.codex_home)
    if not os.path.isdir(home):
        print(f"ERROR: CODEX_HOME {home} does not exist; run --init-home first")
        return 2

    if args.proxy:
        # Codex reads the proxy from the environment (verified with codex doctor:
        # 'proxy env vars present'), NOT from the WinINET system setting and NOT
        # from '-c network.proxy_url' (which doctor reports as ignored).
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            os.environ[k] = args.proxy
        os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
        os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

    if args.ca_bundle:
        ca = os.path.abspath(args.ca_bundle)
        if not os.path.isfile(ca):
            print(f"ERROR: --ca-bundle {ca} is not a readable file")
            return 2
        # Without this, an intercepting proxy yields
        # 'invalid peer certificate: UnknownIssuer'.
        os.environ["CODEX_CA_CERTIFICATE"] = ca

    workdir = os.path.abspath(args.workdir) if args.workdir else os.path.join(
        os.environ.get("TEMP", REPO_ROOT), "codex-onecall-" + time.strftime("%Y%m%d-%H%M%S")
    )
    os.makedirs(workdir, exist_ok=True)
    if os.listdir(workdir):
        print(f"ERROR: --workdir {workdir} is not empty; refusing (use an empty dir)")
        return 2

    argv = [
        codex, "exec",
        "--json",
        "--sandbox", args.sandbox,
        "--skip-git-repo-check",
        "-C", workdir,
        args.prompt,
    ]
    if args.disable_subagents:
        argv[3:3] = ["--disable", SUBAGENT_FEATURE]
    if args.model:
        argv[3:3] = ["-m", args.model]

    print("FULL COMMAND:")
    print("  " + " ".join(f'"{a}"' if " " in a else a for a in argv))
    print(f"CODEX_HOME  : {home}")
    print(f"WORKDIR (-C): {workdir}")
    print("-" * 72)

    env = dict(os.environ)
    env["CODEX_HOME"] = home

    raw_path = os.path.join(args.out_dir, "codex_onecall_raw.txt")
    os.makedirs(args.out_dir, exist_ok=True)

    start = time.time()
    with open(raw_path, "w", encoding="utf-8", errors="replace") as fh:
        try:
            proc = subprocess.run(argv, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=args.timeout, check=False, env=env,
                                  stdin=subprocess.DEVNULL)
            code, err = proc.returncode, None
        except subprocess.TimeoutExpired:
            code, err = None, f"TIMEOUT after {args.timeout}s"
        except Exception as exc:  # noqa: BLE001
            code, err = None, f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start

    with open(raw_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    events = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    types: dict[str, int] = {}
    for e in events:
        t = e.get("type") or (e.get("msg") or {}).get("type") or "?"
        types[t] = types.get(t, 0) + 1
    usage = [e for e in events if "usage" in json.dumps(e).lower()]
    last_message = ""
    for e in events:
        item = e.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            last_message = item["text"]
    thread_id = ""
    for e in events:
        if e.get("type") == "thread.started":
            thread_id = e.get("thread_id", "")

    result = {
        "exit_code": code,
        "error": err,
        "elapsed_seconds": round(elapsed, 1),
        "argv": argv,
        "codex_home": home,
        "workdir": workdir,
        "proxy": args.proxy or "(none)",
        "ca_bundle": os.path.abspath(args.ca_bundle) if args.ca_bundle else "(none)",
        "json_events": len(events),
        "event_types": types,
        "usage_events": len(usage),
        "usage_sample": usage[:2],
        "thread_id": thread_id,
        "last_agent_message": last_message,
        "workdir_entries_after": sorted(os.listdir(workdir)),
        "raw_capture": os.path.relpath(raw_path, REPO_ROOT),
    }
    out_path = os.path.join(args.out_dir, "codex_onecall_result.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)

    print(f"EXIT CODE: {code}   elapsed {elapsed:.1f}s")
    print(f"JSON EVENTS: {len(events)}  types={types}")
    print(f"USAGE EVENTS: {len(usage)}")
    print(f"THREAD ID: {thread_id or '(none)'}")
    print(f"LAST MESSAGE: {last_message[:200]!r}")
    print(f"WORKDIR AFTER (must stay empty for a read-only check): {sorted(os.listdir(workdir))}")
    print(f"RESULT: {os.path.relpath(out_path, REPO_ROOT)}")
    print("\n--- raw capture ---")
    print(text)
    return 0 if code == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Run one real Codex call (no retries).")
    p.add_argument("--init-home", metavar="DIR", help="create a dedicated CODEX_HOME and exit")
    p.add_argument("--codex", default=None, help="path to codex.exe")
    p.add_argument(
        "--codex-home",
        default=default_home(),
        help="CODEX_HOME; defaults outside the repo (may hold a credential copy)",
    )
    p.add_argument("--workdir", default=None, help="empty working root for the call")
    p.add_argument("--prompt", default="Reply with exactly: OK. Do not modify any files.")
    p.add_argument("--sandbox", default="read-only",
                   choices=["read-only", "workspace-write", "danger-full-access"])
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "var"))
    p.add_argument(
        "--proxy",
        default=None,
        help="HTTP proxy for THIS process only, e.g. http://127.0.0.1:8090 "
             "(sets HTTPS_PROXY/HTTP_PROXY; Codex ignores the WinINET setting "
             "and '-c network.proxy_url')",
    )
    p.add_argument(
        "--ca-bundle",
        default=None,
        help="readable PEM bundle set as CODEX_CA_CERTIFICATE; required when the "
             "proxy intercepts TLS (otherwise: UnknownIssuer)",
    )
    p.add_argument("--disable-subagents", dest="disable_subagents", action="store_true",
                   default=True, help="disable the multi_agent feature (default)")
    p.add_argument("--allow-subagents", dest="disable_subagents", action="store_false",
                   help="leave multi_agent enabled (needed only for the step-3 comparison)")
    args = p.parse_args()

    if args.init_home:
        return init_home(os.path.abspath(args.init_home))
    return run_one(args)


if __name__ == "__main__":
    sys.exit(main())
