"""Zero-model identity and protocol check for the binary S3 is pinned to.

Two modes:

* ``--compare OLD NEW`` (offline): extract the protocol facts the S3 evidence
  judge relies on from two ``generate-json-schema`` captures and compare them.
* ``--capture`` (REAL, needs the user's approval): verify the pinned file's
  size and SHA-256, then run exactly ``--version``, ``app-server
  generate-json-schema --help`` and, if the help offers ``--out``,
  ``app-server generate-json-schema [--experimental] --out <dir>``. No
  ``app-server`` session, no thread, no model turn. ``CODEX_HOME`` points at a
  fresh empty directory so the long-term home and its credential are never
  used. Then compare with the old capture and stop on any missing fact.

Nothing here reads, copies or prints credentials.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "packages/contracts"), str(REPO / "packages/adapters"),
               str(REPO / "apps/worker")]
import appserver_s1 as S1  # noqa: E402
import appserver_s3 as S3  # noqa: E402

OLD_CAPTURE = REPO.parents[1] / "var" / "codex-schema"  # main checkout var/, captured 2026-10-14
RUN_ROOT = REPO / "var" / "appserver-s3"

NOTIFICATIONS = ("serverRequest/resolved", "item/started", "item/completed", "turn/started",
                 "turn/completed", "item/agentMessage/delta", "thread/tokenUsage/updated",
                 "thread/started")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _defs(doc: dict) -> dict:
    return doc.get("definitions") or doc.get("$defs") or {}


def _props(schema: dict) -> set:
    return set((schema or {}).get("properties", {}))


def _enum_values(schema: dict, defs: dict) -> set:
    """Enum strings of a definition, following oneOf/anyOf variants."""
    out: set = set()
    if not isinstance(schema, dict):
        return out
    if "$ref" in schema:
        return _enum_values(defs.get(schema["$ref"].split("/")[-1], {}), defs)
    for value in schema.get("enum", []) or []:
        if isinstance(value, str):
            out.add(value)
    if isinstance(schema.get("const"), str):
        out.add(schema["const"])
    for key in ("oneOf", "anyOf", "allOf"):
        for sub in schema.get(key, []) or []:
            out |= _enum_values(sub, defs)
    return out


def _methods(doc: dict) -> set:
    """``method`` enum values of a ServerRequest/ServerNotification union."""
    found: set = set()
    for variant in doc.get("oneOf", []) or []:
        method = (variant.get("properties") or {}).get("method") or {}
        found |= {m for m in method.get("enum", []) or [] if isinstance(m, str)}
    return found


def _variant_types(schema: dict) -> set:
    """``type`` tags of a tagged union (``oneOf`` variants with properties.type)."""
    tags: set = set()
    for variant in (schema or {}).get("oneOf", []) or []:
        tag = (variant.get("properties") or {}).get("type") or {}
        tags |= {t for t in tag.get("enum", []) or [] if isinstance(t, str)}
        if isinstance(tag.get("const"), str):
            tags.add(tag["const"])
    return tags


def _thread_item_variant(defs: dict, kind: str) -> dict:
    for variant in (defs.get("ThreadItem") or {}).get("oneOf", []) or []:
        if kind in ((variant.get("properties") or {}).get("type") or {}).get("enum", []):
            return variant
    return {}


def facts(capture: Path) -> dict:
    """The protocol facts S3 depends on, as booleans (True = present)."""
    v2 = _defs(_load(capture / "codex_app_server_protocol.v2.schemas.json"))
    params = _load(capture / "CommandExecutionRequestApprovalParams.json")
    response = _load(capture / "CommandExecutionRequestApprovalResponse.json")
    response_defs = _defs(response) | v2
    server_requests = _methods(_load(capture / "ServerRequest.json"))
    notifications = _methods(_load(capture / "ServerNotification.json"))
    command_item = _thread_item_variant(v2, "commandExecution")
    decision = (response.get("properties") or {}).get("decision") or {}
    out = {
        "request:item/commandExecution/requestApproval": S3.APPROVAL in server_requests,
        **{f"notification:{m}": m in notifications for m in NOTIFICATIONS},
        "approval_params:" + ",".join(sorted(
            {"itemId", "threadId", "turnId", "command", "cwd", "approvalId",
             "availableDecisions"})): {"itemId", "threadId", "turnId", "command", "cwd",
                                       "approvalId", "availableDecisions"} <= _props(params),
        "approval_response:decision has decline": "decline" in _enum_values(decision, response_defs),
        "thread_start_params:approvalPolicy,approvalsReviewer,sandbox,cwd":
            {"approvalPolicy", "approvalsReviewer", "sandbox", "cwd"} <= _props(v2.get("ThreadStartParams")),
        "thread_start_response:approvalPolicy,approvalsReviewer,sandbox,cwd,thread":
            {"approvalPolicy", "approvalsReviewer", "sandbox", "cwd", "thread"} <= _props(v2.get("ThreadStartResponse")),
        "turn_start_params:threadId,input,approvalPolicy,approvalsReviewer,sandboxPolicy":
            {"threadId", "input", "approvalPolicy", "approvalsReviewer", "sandboxPolicy"} <= _props(v2.get("TurnStartParams")),
        "ask_for_approval has on-request": "on-request" in _enum_values(v2.get("AskForApproval"), v2),
        "approvals_reviewer has user": "user" in _enum_values(v2.get("ApprovalsReviewer"), v2),
        "sandbox_mode has read-only": "read-only" in _enum_values(v2.get("SandboxMode"), v2),
        "sandbox_policy has readOnly": "readOnly" in _variant_types(v2.get("SandboxPolicy")),
        "command_status has inProgress,declined,completed":
            {"inProgress", "declined", "completed"} <= _enum_values(v2.get("CommandExecutionStatus"), v2),
        "command_item:id,command,cwd,status,processId,exitCode,aggregatedOutput":
            {"id", "command", "cwd", "status", "processId", "exitCode", "aggregatedOutput"} <= _props(command_item),
        "resolved:threadId,requestId":
            {"threadId", "requestId"} <= _props(v2.get("ServerRequestResolvedNotification")),
    }
    return out


def compare(old: Path, new: Path) -> dict:
    before, after = facts(old), facts(new)
    missing_before = sorted(k for k, v in before.items() if not v)
    missing_after = sorted(k for k, v in after.items() if not v)
    return {"compatible": not missing_after and not missing_before,
            "facts_checked": len(after), "missing_in_old": missing_before,
            "missing_in_new": missing_after}


def capture(identity: tuple[str, str, int]) -> int:
    path, digest, size = identity
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="identity-", dir=RUN_ROOT))
    report = {"mode": "capture", "binary": path, "model_turns": 0, "steps": []}

    def finish(code: int) -> int:
        (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report.get(k) for k in ("verdict", "version", "compatible")}, ensure_ascii=False))
        print(f"Evidence: {run_dir / 'report.json'}")
        return code

    ok_file = os.path.isfile(path) and os.path.getsize(path) == size and S1.sha256(path) == digest
    report["pinned_identity_matches"] = ok_file
    if not ok_file:
        report["verdict"] = "STOP: pinned file missing or changed; nothing executed"
        return finish(2)
    scratch_home = Path(tempfile.mkdtemp(prefix="agenthub-s3-idhome-"))
    env = {**os.environ, "CODEX_HOME": str(scratch_home)}

    def run(args: list[str], name: str) -> subprocess.CompletedProcess:
        started = time.monotonic()
        proc = subprocess.run([path, *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, env=env, cwd=str(scratch_home),
                              stdin=subprocess.DEVNULL)
        (run_dir / f"{name}.txt").write_text(proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""),
                                            encoding="utf-8")
        report["steps"].append({"args": args, "exit_code": proc.returncode,
                                "seconds": round(time.monotonic() - started, 2)})
        return proc

    version = run(["--version"], "version")
    report["version"] = version.stdout.strip().splitlines()[0] if version.stdout.strip() else None
    helptext = run(["app-server", "generate-json-schema", "--help"], "schema-help")
    if version.returncode != 0 or helptext.returncode != 0 or "--out" not in helptext.stdout:
        report["verdict"] = "STOP: cannot capture schema with this binary's CLI; no S3 turn"
        return finish(3)
    schema_dir = run_dir / "schema"
    args = ["app-server", "generate-json-schema"]
    if "--experimental" in helptext.stdout:
        args.append("--experimental")
    gen = run([*args, "--out", str(schema_dir)], "schema-generate")
    if gen.returncode != 0 or not (schema_dir / "ServerNotification.json").is_file():
        report["verdict"] = "STOP: schema generation failed; no S3 turn"
        return finish(4)
    result = compare(OLD_CAPTURE, schema_dir)
    report.update(result)
    report["verdict"] = ("COMPATIBLE: every protocol fact S3 relies on is present"
                         if result["compatible"] else "STOP: protocol facts differ; no S3 turn")
    try:
        scratch_home.rmdir()  # only if Codex left it empty
        report["scratch_home_removed"] = True
    except OSError:
        report["scratch_home_removed"] = False
        report["scratch_home"] = str(scratch_home)
    return finish(0 if result["compatible"] else 5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"))
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--approve-zero-model-identity-check", action="store_true")
    args = parser.parse_args()
    if args.compare:
        print(json.dumps(compare(Path(args.compare[0]), Path(args.compare[1])), ensure_ascii=False, indent=2))
        return 0
    if args.capture:
        if not args.approve_zero_model_identity_check:
            print("REFUSED: the identity check executes the pinned Codex binary and needs approval.")
            return 2
        return capture(S3.S3_CODEX_IDENTITY)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
