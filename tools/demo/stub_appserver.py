r"""Scripted app-server over stdio, for exercising the prototype against a process.

    .\.venv\Scripts\python.exe tools\demo\stub_appserver.py --scenario happy
    .\.venv\Scripts\python.exe tools\demo\stub_appserver.py --list

It is a transport-level stub of the Codex app-server: same framing options, same
method names as the local schema capture - and NO model behind it. It exists so the
client can be driven end to end without a real session, which this environment
cannot start (piped stdio to native processes is denied here).

Scenarios are named; the client's behaviour, not the request text, selects nothing
here. See `packages/adapters/agenthub_adapters/appserver_stub.py` for the scripted
outcomes and `docs/appserver-feasibility.md` for what is and is not verified.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [
    os.path.join(REPO, "packages", "contracts"),
    os.path.join(REPO, "packages", "adapters"),
    os.path.join(REPO, "apps", "worker"),
]

from agenthub_adapters import appserver_stub as S  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Scripted app-server (no model).")
    ap.add_argument("--scenario", default="happy", choices=list(S.SCENARIOS))
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = ap.parse_args()

    if args.list:
        print("scenarios:")
        for name in S.SCENARIOS:
            print(f"  {name}")
        print("\nThis is not Codex: it never contacts a model and never touches disk")
        print("outside its own streams. Real app-server behaviour is UNVERIFIED.")
        return 0

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    print(f"stub app-server: scenario={args.scenario} (stdio, no model)", file=sys.stderr)
    return S.run_stdio(args.scenario)


if __name__ == "__main__":
    sys.exit(main())
