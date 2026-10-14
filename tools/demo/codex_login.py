r"""Long-term Codex credential home: report its state, and print the login steps.

This tool does NOT log you in and never reads a credential. Logging in is an
interactive, browser-based act that belongs to the operator, and it writes a real
credential to disk - so this prints the exact commands for you to run in your own
terminal, in an environment that can write there.

Why the location matters: ``CODEX_HOME`` may hold ``auth.json``. It must live
OUTSIDE the repository (a stray ``git add -f``, an archive or a backup would leak
it), and outside a temporary directory (Codex refuses to create helper binaries
under Temp and says so in a WARNING).

Usage:
    .\.venv\Scripts\python.exe tools\demo\codex_login.py            # state + steps
    .\.venv\Scripts\python.exe tools\demo\codex_login.py --status   # then run `codex login status`
    .\.venv\Scripts\python.exe tools\demo\codex_login.py --login    # run the login flow
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [
    os.path.join(REPO, "packages", "contracts"),
    os.path.join(REPO, "packages", "adapters"),
    os.path.join(REPO, "apps", "worker"),
]

from agenthub_kernel import config  # noqa: E402


def report() -> dict:
    """The resolved home, as structured facts (used by tests too)."""
    return config.codex_home_status()


def pinned_codex_path() -> str:
    """The executable the adapter would launch (env override wins)."""
    return os.environ.get("AGENT_HUB_CODEX_PATH") or config.PINNED_CODEX_PATH


def print_report(status: dict) -> None:
    print("=" * 78)
    print("CODEX_HOME (long-term, outside the repository)")
    print("=" * 78)
    print(f"  path            : {status['codex_home']}")
    print(f"  source          : {'$env:' + status['override_env'] if status['override_env'] else 'default (%LOCALAPPDATA%\\AgentHub\\codex-home)'}")
    print(f"  outside repo    : {status['outside_repository']}")
    print(f"  exists          : {status['exists']}")
    print(f"  writable        : {status['writable']}")
    print(f"  credential      : {status['credential']}")
    print(
        "  credential set  : "
        + ("yes (contents are never read or printed)" if status["credential_present"] else "no")
    )


def print_steps(status: dict) -> None:
    home = status["codex_home"]
    codex = pinned_codex_path()
    print()
    print("=" * 78)
    print("STEPS FOR YOU TO RUN (in a terminal that may write outside the repo)")
    print("=" * 78)
    print()
    print("1) Create the home and log in once. Codex opens its own browser/device")
    print("   flow; the credential lands in this directory only:")
    print()
    print(f'   New-Item -ItemType Directory -Force "{home}"')
    print(f'   $env:CODEX_HOME = "{home}"')
    print(f'   & "{codex}" login')
    print()
    print("2) Confirm the login WITHOUT making a model call:")
    print()
    print(f'   & "{codex}" login status      # expects: Logged in using ChatGPT')
    print()
    print("3) Point Agent Hub at the same home (it is the default, so this is only")
    print("   needed if you chose a different directory):")
    print()
    print(f'   $env:AGENT_HUB_CODEX_HOME = "{home}"')
    print()
    print("4) Check this tool again; `credential set` must read `yes`:")
    print()
    print("   .\\.venv\\Scripts\\python.exe tools\\demo\\codex_login.py")
    print()
    print("NEVER paste the credential anywhere, and never copy it into the repository.")
    if not status["outside_repository"]:
        print()
        print("!! REFUSE: this path is INSIDE the repository. Set AGENT_HUB_CODEX_HOME")
        print("   to a directory outside the repo before logging in.")
    if os.path.abspath(home).lower().startswith(
        os.path.abspath(os.environ.get("TEMP") or "/nonexistent").lower()
    ):
        print()
        print("NOTE: this path is under %TEMP%. Codex refuses to create PATH-alias")
        print("      helper binaries in temporary directories (it warns and continues).")
        print("      Prefer a normal directory for long-term use.")


def run_codex(args: list[str], home: str) -> int:
    """Run `codex login` / `codex login status` with CODEX_HOME set."""
    codex = pinned_codex_path()
    if not os.path.isfile(codex):
        print(f"REFUSING: pinned Codex executable not found at {codex}")
        return 2
    if not os.path.isdir(home):
        print(f"REFUSING: {home} does not exist; create it first (step 1 above).")
        return 2
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    print(f"\n$ $env:CODEX_HOME = '{home}'; & '{codex}' {' '.join(args)}\n")
    # stdout is inherited: this is the operator's own interactive login.
    completed = subprocess.run([codex, *args], env=env, check=False)
    return completed.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Long-term Codex credential home.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true",
                       help="also run `codex login status` (no model call)")
    group.add_argument("--login", action="store_true",
                       help="run the interactive `codex login` flow")
    args = ap.parse_args()

    status = report()
    print_report(status)

    if not status["outside_repository"]:
        print_steps(status)
        return 2
    if args.status or args.login:
        return run_codex(["login", "status"] if args.status else ["login"],
                         status["codex_home"])
    print_steps(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
