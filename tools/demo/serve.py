r"""Start the Agent Hub Mock API locally.

Puts the package directories on ``sys.path`` (the repo is not pip-installed, so
``uvicorn agenthub_rt.api:app`` alone would not import), then serves the API.

On startup it writes a PID file (``var/serve-<port>.pid``) recording its own
process id, and removes it on a clean exit. Use ``tools/demo/stop.py`` to stop
exactly that process instead of killing every ``python.exe`` on the machine:

    Get-Process python | Stop-Process -Force     # DON'T: kills unrelated work
    python tools/demo/stop.py                    # DO: PID-scoped

Usage (from the repo root, using the project venv):

    .\.venv\Scripts\python.exe tools\demo\serve.py
    .\.venv\Scripts\python.exe tools\demo\serve.py --host 127.0.0.1 --port 8080 --reload

Then open the interactive API page at http://127.0.0.1:8080/docs
"""

from __future__ import annotations

import argparse
import atexit
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _sub in ("packages/contracts", "packages/adapters", "apps/worker", "apps/api"):
    _p = os.path.join(REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pid_file_path(port: int) -> str:
    return os.path.join(REPO_ROOT, "var", f"serve-{port}.pid")


def write_pid_file(port: int) -> str:
    """Record our own PID so the server can be stopped precisely, by id."""
    path = pid_file_path(port)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii") as fh:
        fh.write(str(os.getpid()))

    def _cleanup() -> None:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="ascii") as f:
                    if f.read().strip() == str(os.getpid()):
                        os.remove(path)
        except OSError:
            pass

    atexit.register(_cleanup)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Hub Mock API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (development only)",
    )
    parser.add_argument(
        "--no-pid-file",
        action="store_true",
        help="do not write var/serve-<port>.pid",
    )
    parser.add_argument(
        "--adapter",
        choices=["mock", "codex"],
        default=None,
        help="adapter to serve with: 'mock' is inert (default); 'codex' exposes "
             "records and capabilities, but real launches are blocked until "
             "native-agent controls are verified",
    )
    args = parser.parse_args()

    import uvicorn

    from agenthub_kernel import config

    if args.adapter:
        os.environ["AGENT_HUB_ADAPTER"] = args.adapter

    from agenthub_kernel.adapters import adapter_kind

    kind = adapter_kind()

    print("=" * 72)
    if kind == "codex":
        print("Agent Hub - Codex adapter (real launches blocked; bundled stub allowed)")
    else:
        print("Agent Hub - Mock API (no real Agent is contacted)")
    print(f"  adapter  : {kind}")
    print(f"  API page : http://{args.host}:{args.port}/docs")
    print(f"  console  : http://{args.host}:{args.port}/ui")
    print(f"  OpenAPI  : http://{args.host}:{args.port}/openapi.json")
    print(f"  Database : {config.default_db_url()}")
    print(f"  Data dir : {config.DATA_DIR}")
    if kind == "codex":
        s = config.codex_settings()
        print(f"  codex    : {s['codex_path']}")
        print(f"  version  : {s['version']}")
        print(f"  CODEX_HOME: {s['codex_home']}")
        print(f"  sandbox  : read-only, sub-agents disabled (--disable multi_agent)")
        print(f"  proxy    : {s['proxy'] or '(none)'}")
        print(f"  ca bundle: {s['ca_bundle'] or '(none)'}")
        cred = config.credential_location()
        print(
            "  credential: "
            + ("present (contents never printed)" if os.path.exists(cred) else "not present")
            + f"  at {cred}"
        )
    # NEVER print the token itself: the startup banner ends up in scrollback,
    # logs and screenshots. Report only where it came from.
    if os.environ.get("AGENT_HUB_USER_TOKEN"):
        print("  User token: from $env:AGENT_HUB_USER_TOKEN (value not shown)")
    else:
        print("  User token: using the built-in development default (not shown)")
    if not args.no_pid_file:
        pf = write_pid_file(args.port)
        print(f"  PID      : {os.getpid()}  (recorded in {os.path.relpath(pf, REPO_ROOT)})")
        print(f"  stop with: python tools/demo/stop.py --port {args.port}")
    print("=" * 72)

    if args.reload:
        uvicorn.run(
            "agenthub_rt.api:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[os.path.join(REPO_ROOT, "apps"), os.path.join(REPO_ROOT, "packages")],
        )
        return

    from agenthub_rt.api import create_app

    app = create_app(adapter_kind=kind)
    config_ = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config_)

    # Graceful, opt-in shutdown for local trials:
    # is not possible to terminate this process from outside in every
    # environment (a restricted sandbox can deny os.kill / TerminateProcess), so
    # the server can be asked to stop over HTTP instead - see tools/demo/stop.py.
    # The endpoint is user-token protected and Mock/dev-only.
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)

    server.run()


if __name__ == "__main__":
    main()
