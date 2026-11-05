"""Bounded real test of the minimum Claude Code path. Offline (stub) by default.

Two steps, each run on its own (docs/claude-code-adapter.md, real-test request):

* ``--step identity`` (zero model): ``claude --version`` and ``claude --help``
  with the adapter's child environment, before any login is used. Real mode
  needs ``--approve-zero-model-identity-check``. Judged on: both exit 0, the
  version line names the pinned version, the file hash is the pinned one before
  and after. Flags of the fixed argv missing from ``--help`` make it
  INCONCLUSIVE (the docs say ``--help`` does not list every flag).
* ``--step turn`` (at most ONE model turn): through the Hub in this process -
  API, approval with the supervised consent, Worker, adapter - with the fixed
  prompt and no tools. Real mode needs ``--approve-one-model-turn`` and
  ``--acknowledge-supervised-boundary`` and refuses unless the identity report
  has been recorded in code (``ZERO_MODEL_IDENTITY_REPORT``).

Evidence is written under var/claude-code-min/ (gitignored); only hashes are
registered. No prompt or reply text is written to the report: the reply is
judged as "exactly OK" or not. The driver never retries and never sends a
second turn.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for sub in ("packages/contracts", "packages/adapters", "apps/worker", "apps/api"):
    path = str(REPO / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

RUN_ROOT = REPO / "var" / "claude-code-min"
PROMPT = "Reply with exactly: OK. Do not use tools, create subagents, or modify any files."
EXPECTED_REPLY = "OK"
TURN_TIMEOUT_SECONDS = 180
IDENTITY_TIMEOUT_SECONDS = 30
#: certifi 2026.10.19 cacert.pem, recorded in docs/codex-proxy-setup.md §7.1.
CA_BUNDLE_SHA256 = "50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785"
#: Flags of the fixed argv that --help is expected to list.
HELP_FLAGS = ("--output-format", "--safe-mode", "--tools", "--strict-mcp-config", "--disable-slash-commands",
              "--permission-mode", "--max-turns", "--no-session-persistence")


class Refused(Exception):
    """A precondition failed; nothing was launched."""


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: str, root: Path) -> bool:
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    r = os.path.normcase(os.path.abspath(str(root))).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


@contextlib.contextmanager
def _environment(values: dict):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write(run_dir: Path, result: dict) -> str:
    path = run_dir / "report.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return _sha256(str(path))


# --------------------------------------------------------------------------- #
# Step A: zero-model identity
# --------------------------------------------------------------------------- #


def _run_plain(argv: list, env: dict, cwd: str, out_path: Path) -> dict:
    with open(out_path, "wb") as out:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                env=env, cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            code = proc.wait(timeout=IDENTITY_TIMEOUT_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            code, timed_out = None, True
    text = out_path.read_bytes().decode("utf-8", errors="replace")
    return {"exit_code": code, "timed_out": timed_out, "output_sha256": _sha256(str(out_path)),
            "output_bytes": out_path.stat().st_size, "text": text}


def judge_identity(evidence: dict, *, version: str) -> tuple[str, list]:
    codes = []
    for name in ("version", "help"):
        if evidence[name]["exit_code"] != 0 or evidence[name]["timed_out"]:
            codes.append(f"{name}_failed")
    if not evidence["version_line"].startswith(version):
        codes.append("version_mismatch")
    if not (evidence["hash_before_matches"] and evidence["hash_after_matches"]):
        codes.append("file_identity_changed")
    if codes:
        return "FAIL", codes
    if evidence["help_missing_flags"]:
        return "INCONCLUSIVE", ["help_missing_flags"]
    return "PASS", []


def run_identity(*, real: bool = False, approved: bool = False, run_root: Path = RUN_ROOT) -> int:
    if real and not approved:
        print("REFUSED: the identity step needs --approve-zero-model-identity-check.")
        return 2
    from agenthub_adapters.claude_code import PINNED_CLAUDE_CODE, bundled_stub_path, build_env
    from agenthub_kernel import config

    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="identity-real-" if real else "identity-stub-", dir=run_root))
    cwd = tempfile.mkdtemp(prefix="agenthub-cc-identity-")  # outside the repository
    pinned = PINNED_CLAUDE_CODE
    if real:
        exe = pinned["path"]
        if not os.path.isfile(exe) or os.path.getsize(exe) != pinned["size"]:
            print("REFUSED: pinned executable missing or size changed; nothing was run.")
            return 2
        before = _sha256(exe) == pinned["sha256"]
        if not before:
            print("REFUSED: pinned executable hash changed; nothing was run.")
            return 2
        prefix, version = [exe], pinned["version"]
    else:
        prefix, version, before = [sys.executable, bundled_stub_path()], pinned["version"], True
    env, stripped, added = build_env(config_dir=config.claude_config_dir(), proxy=None)
    result = {"mode": "real" if real else "stub", "step": "identity", "executable": prefix[-1],
              "env_stripped": stripped, "env_set": added}
    for name in ("version", "help"):
        out = _run_plain([*prefix, f"--{name}"], env, cwd, run_dir / f"{name}.txt")
        result[name] = {k: v for k, v in out.items() if k != "text"}
        result[f"_{name}_text"] = out["text"]
    version_text = result.pop("_version_text")
    help_text = result.pop("_help_text")
    result["version_line"] = version_text.strip().splitlines()[0] if version_text.strip() else ""
    result["help_missing_flags"] = [f for f in HELP_FLAGS if f not in help_text]
    result["hash_before_matches"] = before
    result["hash_after_matches"] = (_sha256(prefix[0]) == pinned["sha256"]) if real else True
    result["verdict"], result["reason_codes"] = judge_identity(result, version=version)
    digest = _write(run_dir, result)
    print(f"IDENTITY {result['verdict']} {result['reason_codes']}")
    print(f"Report: {run_dir / 'report.json'}  sha256={digest}")
    return 0 if result["verdict"] == "PASS" else 1


# --------------------------------------------------------------------------- #
# Step B: one model turn through the Hub
# --------------------------------------------------------------------------- #


def preflight_turn(adapter) -> dict:
    from agenthub.contracts import execution_admission
    from agenthub_adapters import claude_code as cc

    launcher = adapter.launcher
    checks = {
        "identity_report_recorded": bool(cc.ZERO_MODEL_IDENTITY_REPORT),
        "launcher_is_pinned_real": type(launcher) is cc.ClaudeCodeLauncher,
        "config_dir_outside_repository": not _inside(launcher.config_dir, REPO),
        # Presence only; the file is never opened.
        "config_dir_has_login": os.path.isfile(os.path.join(launcher.config_dir, ".credentials.json")),
        "workdir_outside_repository": not _inside(adapter.workdir_root, REPO),
        "ca_bundle_hash_matches": bool(launcher.ca_bundle and os.path.isfile(launcher.ca_bundle)
                                       and _sha256(launcher.ca_bundle) == CA_BUNDLE_SHA256),
        "supervised_admission": execution_admission(adapter, "supervised").mode == "supervised",
        "strict_still_blocked": not execution_admission(adapter, "strict").allowed,
    }
    ok, _why = launcher.identity_matches() if checks["launcher_is_pinned_real"] else (False, "")
    checks["pinned_identity_matches"] = bool(ok)
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise Refused("preflight failed: " + ", ".join(failed))
    return checks


def judge_turn(evidence: dict) -> tuple[str, list]:
    fail, unsure = [], []
    if evidence["status"] != "succeeded":
        fail.append(f"status_{evidence['status']}")
    if evidence["start_count"] != 1:
        fail.append("start_count_not_one")
    if evidence["held_slots"] != 0:
        fail.append("quota_slot_held")
    if not evidence["binding_matches"]:
        fail.append("binding_mismatch")
    record = evidence["record"]
    if record.get("timed_out"):
        fail.append("timed_out")
    if fail:
        return "FAIL", fail
    if not evidence["reply_exact"]:
        unsure.append("reply_not_exact")
    if not record.get("usage"):
        unsure.append("no_usage")
    if record.get("num_turns") != 1:
        unsure.append("num_turns_not_one")
    if record.get("permission_denials_count") not in (0, None):
        unsure.append("permission_denials")  # a tool was attempted although none was offered
    if evidence["workdir_empty"] is not True:
        unsure.append("workdir_not_empty")
    return ("INCONCLUSIVE", unsure) if unsure else ("PASS", [])


def run_turn(*, real: bool = False, approved: bool = False, acknowledged: bool = False,
             scenario: str = "success-usage", run_root: Path = RUN_ROOT) -> int:
    if real and not (approved and acknowledged):
        print("REFUSED: the turn step needs --approve-one-model-turn and --acknowledge-supervised-boundary.")
        return 2
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="turn-real-" if real else f"turn-stub-{scenario}-", dir=run_root))
    data_dir = Path(tempfile.mkdtemp(prefix="agenthub-cc-data-"))  # outside the repository
    result = {"mode": "real" if real else "stub", "step": "turn", "scenario": scenario, "verdict": "FAIL",
              "reason_codes": [], "refused": None, "data_dir": str(data_dir),
              "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest()}
    env = {"AGENT_HUB_ADAPTER": "claude-code",
           "AGENT_HUB_CLAUDE_CODE_STUB": None if real else scenario,
           "AGENT_HUB_CLAUDE_CODE_TIMEOUT": str(TURN_TIMEOUT_SECONDS),
           "AGENT_HUB_CLAUDE_CODE_WORKDIR": str(data_dir / "runs"),
           "AGENT_HUB_USER_TOKEN": secrets.token_hex(16)}  # per run, never stored
    from agenthub_kernel import config

    saved_data_dir = config.DATA_DIR
    try:
        with _environment(env):
            config.DATA_DIR = str(data_dir)
            from fastapi.testclient import TestClient
            from sqlalchemy import func, select

            from agenthub_kernel.adapters import adapter_binding, build_adapter
            from agenthub_kernel.db import Database, InvocationRow, QuotaReservationRow
            from agenthub_rt.api import create_app

            adapter = build_adapter("claude-code")
            result["binding_identity"] = adapter_binding(adapter)
            if real:
                result["preflight"] = preflight_turn(adapter)
            headers = {"Authorization": f"Bearer {os.environ['AGENT_HUB_USER_TOKEN']}"}
            db = Database(f"sqlite:///{(data_dir / 'agent-hub.db').as_posix()}")
            app = create_app(db=db, adapter=adapter)
            with TestClient(app) as client:
                cid = client.post("/api/conversations", headers=headers,
                                  json={"title": "claude-code-min"}).json()["conversation_id"]
                turn = client.post(f"/api/conversations/{cid}/invocations", headers=headers, json={
                    "text": PROMPT, "agent_id": "claude-code", "client_request_id": "cc-min",
                    "execution_mode": "supervised"})
                if turn.status_code != 200:
                    raise Refused(f"submit refused: {turn.status_code}")
                turn = turn.json()
                consent = client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=headers,
                                      json={"accept_unmanaged_native_agents": True})
                if consent.status_code != 200:
                    raise Refused(f"approval refused: {consent.status_code}")
                started = time.monotonic()
                ran = client.post(f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"},
                                  headers=headers)
                result["run_http_status"] = ran.status_code
                result["run_seconds"] = round(time.monotonic() - started, 2)
                messages = client.get(f"/api/conversations/{cid}/messages", headers=headers).json()["messages"]
            inv_id = turn["invocation_id"]
            with db.session() as s:
                row = s.get(InvocationRow, inv_id)
                status, snapshot = row.status, dict(row.snapshot)
                held = s.scalar(select(func.count()).select_from(QuotaReservationRow)
                                .where(QuotaReservationRow.released_at.is_(None)))
            db.engine.dispose()
            record = (adapter.store.get(inv_id) or {}).get("payload") or {}
            replies = [m["text"] for m in messages if m["role"] == "assistant"]
            workdir = record.get("workdir")
            evidence = {
                "status": status, "start_count": adapter.start_count, "held_slots": held,
                "binding_matches": snapshot.get("adapter_version") == result["binding_identity"],
                "reply_exact": len(replies) == 1 and replies[0].strip() == EXPECTED_REPLY,
                "workdir_empty": (not any(Path(workdir).iterdir())) if workdir and Path(workdir).is_dir() else None,
                # Structural facts only: no reply, prompt or error text.
                "record": {k: record.get(k) for k in (
                    "session_id", "usage", "cost_usd_estimate", "models", "num_turns", "subtype", "is_error",
                    "permission_denials_count", "output_state", "exit_code", "timed_out", "elapsed",
                    "input_written", "stdout_bytes", "stderr_bytes", "stderr_sha256", "env_stripped",
                    "env_set", "argv_tail", "verdict", "terminal", "detail")},
            }
            result.update(invocation_id=inv_id, **evidence)
            result["verdict"], result["reason_codes"] = judge_turn(evidence)
    except Refused as exc:
        result["refused"] = str(exc)
        result["reason_codes"] = ["refused"]
    finally:
        config.DATA_DIR = saved_data_dir
        digest = _write(run_dir, result)
    print(f"TURN {result['verdict']} {result['reason_codes']}")
    print(f"Report: {run_dir / 'report.json'}  sha256={digest}")
    return 0 if result["verdict"] == "PASS" else 1


def main() -> int:
    from agenthub_adapters.claude_code import STUB_SCENARIOS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=("identity", "turn"), required=True)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=STUB_SCENARIOS, default="success-usage")
    parser.add_argument("--approve-zero-model-identity-check", action="store_true")
    parser.add_argument("--approve-one-model-turn", action="store_true")
    parser.add_argument("--acknowledge-supervised-boundary", action="store_true")
    args = parser.parse_args()
    if args.step == "identity":
        return run_identity(real=args.mode == "real", approved=args.approve_zero_model_identity_check)
    return run_turn(real=args.mode == "real", approved=args.approve_one_model_turn,
                    acknowledged=args.acknowledge_supervised_boundary, scenario=args.scenario)


if __name__ == "__main__":
    raise SystemExit(main())
