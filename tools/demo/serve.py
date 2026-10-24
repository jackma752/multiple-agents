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
    # chat page demo against the offline app-server stub, separate data:
    .\.venv\Scripts\python.exe tools\demo\serve.py --port 8170 --appserver-stub demo --data-dir var\demo-chat

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
        choices=["mock", "codex", "claude-code", "multi"],
        default=None,
        help="adapter to serve with: 'mock' is inert (default); 'codex' exposes "
             "records and capabilities; real launches require explicit per-call "
             "supervised consent while strict controls remain unverified; "
             "'multi' serves codex and claude-code side by side, each call on "
             "the adapter of its own agent",
    )
    parser.add_argument(
        "--claude-code-stub",
        metavar="SCENARIO",
        default=None,
        help="serve Claude Code against the bundled OFFLINE stub with this "
             "scenario (e.g. 'success'); no Claude Code, no model",
    )
    parser.add_argument(
        "--appserver-stub",
        metavar="SCENARIO",
        default=None,
        help="serve Codex over the app-server transport against the bundled "
             "OFFLINE stub with this scenario (e.g. 'demo'); no Codex, no model",
    )
    parser.add_argument(
        "--codex-transport",
        choices=["exec", "app-server"],
        default=None,
        help="with --adapter codex: the Codex transport. 'app-server' uses the pinned "
             "identity in code (never the AGENT_HUB_CODEX_* variables) and is admitted "
             "in supervised mode only",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="runtime data directory (database, run dirs); default var/",
    )
    parser.add_argument(
        "--files-root",
        default=None,
        help="the only directory tree messages may reference files from (@ references); "
             "default: none, references refused",
    )
    args = parser.parse_args()

    # Environment first: config resolves DATA_DIR when it is imported.
    if args.data_dir:
        os.environ["AGENT_HUB_DATA_DIR"] = os.path.abspath(args.data_dir)
    if args.files_root:
        os.environ["AGENT_HUB_FILES_ROOT"] = os.path.abspath(args.files_root)
    if args.appserver_stub:
        os.environ["AGENT_HUB_ADAPTER"] = "multi" if args.adapter == "multi" else "codex"
        os.environ["AGENT_HUB_CODEX_TRANSPORT"] = "app-server"
        os.environ["AGENT_HUB_APPSERVER_STUB"] = args.appserver_stub
    if args.claude_code_stub:
        os.environ["AGENT_HUB_CLAUDE_CODE_STUB"] = args.claude_code_stub

    import uvicorn

    from agenthub_kernel import config

    if args.adapter and not args.appserver_stub:
        os.environ["AGENT_HUB_ADAPTER"] = args.adapter
    if args.codex_transport and not args.appserver_stub:
        os.environ["AGENT_HUB_CODEX_TRANSPORT"] = args.codex_transport

    from agenthub_kernel.adapters import adapter_kind

    kind = adapter_kind()
    adapter = None

    print("=" * 72)
    if kind == "multi":
        from agenthub_kernel.adapters import build_adapter, describe

        # Built once here and handed to the app, so the banner shows exactly
        # what will serve each agent (transport, bound identity, admission).
        adapter = build_adapter(kind)
        print("Agent Hub - codex + claude-code side by side (each call checked and run on its own agent's adapter)")
        for agent_id, info in describe(adapter)["agents"].items():
            sup = info.get("supervised_admission") or {}
            print(f"  {agent_id:<11}: transport={info.get('transport')}  supervised={sup.get('mode')}"
                  f"{'' if sup.get('allowed') else ' (REFUSED: ' + str(sup.get('code')) + ')'}")
            print(f"  {'':<11}  binding={info.get('binding_identity')}")
    elif args.appserver_stub:
        print("Agent Hub - app-server transport against the OFFLINE STUB (no Codex, no model)")
    elif kind == "codex":
        print("Agent Hub - Codex adapter (strict blocked; explicit supervised approval available)")
    else:
        print("Agent Hub - Mock API (no real Agent is contacted)")
    print(f"  adapter  : {kind}")
    print(f"  API page : http://{args.host}:{args.port}/docs")
    print(f"  console  : http://{args.host}:{args.port}/ui")
    print(f"  chat     : http://{args.host}:{args.port}/chat")
    print(f"  OpenAPI  : http://{args.host}:{args.port}/openapi.json")
    print(f"  Database : {config.default_db_url()}")
    print(f"  Data dir : {config.DATA_DIR}")
    print(f"  Files    : {config.files_root() or '(none: file references refused)'}")
    if args.appserver_stub:
        print(f"  stub     : tools/demo/stub_appserver.py --scenario {args.appserver_stub}")
    elif kind == "codex" and config.codex_transport() == "app-server":
        from agenthub_adapters.appserver_adapter import PINNED_APPSERVER

        s = config.codex_settings()
        print("  transport: app-server (strict blocked; supervised only, per-call consent)")
        print(f"  codex    : {PINNED_APPSERVER['path']}  (pinned in code)")
        print(f"  version  : {PINNED_APPSERVER['version']}")
        print(f"  sha256   : {PINNED_APPSERVER['sha256']}  size {PINNED_APPSERVER['size']}")
        overrides = s.get("identity_env_overrides") or ()
        print("  overrides: " + (", ".join(overrides) + "  -> supervised admission refused"
                                 if overrides else "none"))
        print(f"  CODEX_HOME: {s['codex_home']}")
        print(f"  proxy    : {s['proxy'] or '(none)'}")
        print(f"  ca bundle: {s['ca_bundle'] or '(none)'}")
        print(f"  work dirs: {s['workdir_root']}")
    elif kind == "codex":
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

    app = create_app(adapter=adapter) if adapter is not None else create_app(adapter_kind=kind)
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
