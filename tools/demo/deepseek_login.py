"""Store the DeepSeek API key where the adapter expects it.

    python tools/demo/deepseek_login.py                 # status only
    python tools/demo/deepseek_login.py --set           # prompts, input hidden
    python tools/demo/deepseek_login.py --set --from-env DEEPSEEK_KEY

The key is written to ``{"api_key": "sk-..."}`` at
``%LOCALAPPDATA%\\AgentHub\\deepseek\\credentials.json`` (override the directory
with ``AGENT_HUB_DEEPSEEK_HOME``). The file is never printed, never passed on a
command line by this script's own examples, and a path inside the repository is
refused — the adapter refuses it too, so a key stored there would only produce a
confusing "credential inside repository" refusal later.

Exit codes: 0 ok, 1 refusal, 2 usage error.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bootstrap() -> None:
    """Make the repository's packages importable the way serve.py does."""
    for rel in ("packages/contracts", "packages/adapters", "apps/worker"):
        path = os.path.join(REPO_ROOT, rel)
        if path not in sys.path:
            sys.path.insert(0, path)


def _inside(path: str, root: str) -> bool:
    a = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(root))
    return a == b or a.startswith(b + os.sep)


def _mask(key: str) -> str:
    body = key.strip()
    if len(body) <= 8:
        return "*" * len(body)
    return f"{body[:5]}…{body[-4:]} ({len(body)} chars)"


def _read_status(path: str) -> dict:
    if not os.path.isfile(path):
        return {"exists": False, "size": None, "valid": False, "preview": ""}
    size = os.path.getsize(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        key = document.get("api_key") if isinstance(document, dict) else None
    except Exception:  # noqa: BLE001 - any unreadable file is "not valid"
        return {"exists": True, "size": size, "valid": False, "preview": ""}
    valid = isinstance(key, str) and key.strip().startswith("sk-")
    return {
        "exists": True,
        "size": size,
        "valid": valid,
        "preview": _mask(key) if valid else "",
    }


def _write(path: str, key: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    document = {"api_key": key}
    handle, tmp = tempfile.mkstemp(prefix=".credentials-", suffix=".json", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    _bootstrap()
    from agenthub_kernel import config  # noqa: PLC0415 - after _bootstrap

    parser = argparse.ArgumentParser(description="Store the DeepSeek API key for the Hub adapter.")
    parser.add_argument("--set", action="store_true", help="prompt for a key and store it")
    parser.add_argument("--from-env", default="", metavar="VAR",
                        help="read the key from this environment variable instead of prompting")
    parser.add_argument("--status", action="store_true", help="only report the current credential")
    args = parser.parse_args(argv)

    path = config.deepseek_credentials_path()
    settings = config.deepseek_settings()

    if _inside(path, REPO_ROOT):
        print(f"refused: {path} is inside the repository {REPO_ROOT}")
        print("set AGENT_HUB_DEEPSEEK_HOME to a directory outside the repository")
        return 1

    if not args.set or args.status:
        status = _read_status(path)
        print(f"path     : {path}")
        print(f"endpoint : {settings['base_url']}")
        print(f"model    : {settings['model']}")
        if not status["exists"]:
            print("credential: MISSING (every real call refuses before sending anything)")
            print(f"next     : & '{sys.executable}' '{os.path.abspath(__file__)}' --set")
            return 1
        label = "valid" if status["valid"] else 'NOT a usable {"api_key": "sk-..."} file'
        print(f"credential: present, {status['size']} bytes, {label}")
        if status["preview"]:
            print(f"key      : {status['preview']}  (contents otherwise never printed)")
        return 0 if status["valid"] else 1

    key = ""
    if args.from_env:
        key = os.environ.get(args.from_env, "")
        if not key:
            print(f"refused: {args.from_env} is not set")
            return 2
    else:
        key = getpass.getpass("DeepSeek API key (input hidden): ")

    key = key.strip()
    if not key.startswith("sk-") or len(key) < 20:
        print("refused: that does not look like a DeepSeek API key (expects sk-…, >= 20 chars)")
        return 2

    _write(path, key)
    status = _read_status(path)
    print(f"stored   : {path} ({status['size']} bytes)")
    print(f"key      : {_mask(key)}")
    print("next     : restart the server with --adapter deepseek (or multi) and make one call")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
