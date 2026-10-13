r"""Start the Agent Hub Mock API locally.

Puts the package directories on ``sys.path`` (the repo is not pip-installed, so
``uvicorn agenthub_rt.api:app`` alone would not import), then serves the API.

Usage (from the repo root, using the project venv):

    .\.venv\Scripts\python.exe tools\demo\serve.py
    .\.venv\Scripts\python.exe tools\demo\serve.py --host 127.0.0.1 --port 8080 --reload

Then open the interactive API page at http://127.0.0.1:8080/docs
"""

from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _sub in ("packages/contracts", "packages/adapters", "apps/worker", "apps/api"):
    _p = os.path.join(REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Hub Mock API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (development only)",
    )
    args = parser.parse_args()

    import uvicorn

    from agenthub_kernel import config

    print("=" * 72)
    print("Agent Hub - Mock API (no real Agent is contacted)")
    print(f"  API page : http://{args.host}:{args.port}/docs")
    print(f"  OpenAPI  : http://{args.host}:{args.port}/openapi.json")
    print(f"  Database : {config.default_db_url()}")
    print(f"  Data dir : {config.DATA_DIR}")
    token = os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token")
    print(f"  User token (from AGENT_HUB_USER_TOKEN or default): {token}")
    print("=" * 72)

    if args.reload:
        uvicorn.run(
            "agenthub_rt.api:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[os.path.join(REPO_ROOT, "apps"), os.path.join(REPO_ROOT, "packages")],
        )
    else:
        from agenthub_rt.api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
