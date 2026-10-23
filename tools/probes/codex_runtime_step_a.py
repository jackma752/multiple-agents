"""Zero-model Step A for a staged Codex runtime (``--capture`` needs the user's approval).

``--capture`` first checks, without executing anything, that every staged file
still has its reviewed hash and a valid signature and that the alpha.11
baseline capture is unchanged. Then it runs exactly ``--version``, ``app-server
generate-json-schema --help`` and ``app-server generate-json-schema
[--experimental] --out <dir>`` from the staged folder, with a fresh empty
``CODEX_HOME`` (the long-term home and its credential are never used). No
app-server session, no thread, no model turn.

The capture is compared with the baseline - Step A ``identity-hkzdmoqj``, the
alpha.11 build S3-S6-3 ran on: every protocol fact S3, S4, S5 and the app-server
adapter rely on must still be present, and every schema file that was added,
removed or changed is listed (with the definitions that changed) so the
differences can be reviewed before the adapter is re-pinned.
``--compare BASELINE CAPTURE`` does the comparison offline.

Nothing here reads, copies or prints credentials.
"""
from __future__ import annotations

import argparse
import hashlib
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
import appserver_s3_identity as ID  # noqa: E402
import appserver_s4 as S4  # noqa: E402
import appserver_s5 as S5  # noqa: E402
import codex_runtime_stage as STAGE  # noqa: E402
from agenthub_adapters import appserver as A  # noqa: E402
from agenthub_adapters import appserver_adapter as AD  # noqa: E402
from agenthub_adapters import proc_tree  # noqa: E402

#: Step A of 2026-10-22 on alpha.11 (docs/appserver-s3-identity-2026-10-22.md); files stay out of git.
BASELINE = Path(r"E:\plug\agent-hub\.worktrees\appserver-s3-probe\var\appserver-s3\identity-hkzdmoqj\schema")
BASELINE_MANIFEST = "12c012a180048bbbff489295813f54c70130bf32edfe8394ffd3547b4e39ea6f"
BASELINE_VERSION = "codex-cli 0.163.0-alpha.11"
RUN_ROOT = REPO / "var" / "codex-runtime"
COMMAND_TIMEOUT = 60


def file_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): STAGE.sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def manifest(root: Path) -> str:
    """The registered manifest hash: sorted POSIX paths, "<sha256>  <path>" lines joined by LF, trailing LF."""
    lines = "".join(f"{digest}  {path}\n" for path, digest in sorted(file_hashes(root).items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def adapter_facts(capture: Path) -> dict:
    """Methods and item types ``AppServerCodexAdapter`` sends, waits for or classifies."""
    v2 = ID._defs(ID._load(capture / "codex_app_server_protocol.v2.schemas.json"))
    client = ID._methods(ID._load(capture / "ClientRequest.json"))
    server_requests = ID._methods(ID._load(capture / "ServerRequest.json"))
    notifications = ID._methods(ID._load(capture / "ServerNotification.json"))
    item_types = ID._variant_types(v2.get("ThreadItem"))
    return {
        **{f"request:{m}": m in client
           for m in (A.INITIALIZE, A.THREAD_START, A.THREAD_RESUME, A.TURN_START, A.TURN_INTERRUPT)},
        **{f"notification:{m}": m in notifications
           for m in (A.THREAD_STARTED, A.TURN_STARTED, A.TURN_COMPLETED, A.ITEM_STARTED, A.ITEM_COMPLETED,
                     A.DELTA_AGENT_MESSAGE, A.DELTA_COMMAND_OUTPUT, A.FILE_CHANGE_PATCH_UPDATED,
                     AD.TOKEN_USAGE_UPDATED, "serverRequest/resolved")},
        **{f"server_request:{m}": m in server_requests
           for m in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                     "item/permissions/requestApproval")},
        **{f"thread_item:{t}": t in item_types
           for t in sorted(AD.NATIVE_AGENT_ITEM_TYPES | AD.TOOL_ITEM_TYPES | {"agentMessage", "userMessage"})},
    }


def facts(capture: Path) -> dict:
    """Every fact, prefixed by who relies on it (True = present)."""
    out = {f"s3|{k}": v for k, v in ID.facts(capture).items()}
    out.update({f"s4|{k}": v for k, v in S4.schema_facts(capture)["facts"].items()})
    out.update({f"s5|{k}": v for k, v in S5.schema_facts(capture)["facts"].items()})
    out.update({f"adapter|{k}": v for k, v in adapter_facts(capture).items()})
    return out


def schema_diff(old: Path, new: Path) -> dict:
    """Files added/removed/changed, and for changed JSON files the top-level definitions that differ."""
    a, b = file_hashes(old), file_hashes(new)
    changed = sorted(p for p in a.keys() & b.keys() if a[p] != b[p])
    definitions: dict[str, list[str]] = {}
    for path in changed:
        if not path.endswith(".json"):
            continue
        before, after = ID._load(old / path), ID._load(new / path)
        da, db = ID._defs(before), ID._defs(after)
        names = sorted(k for k in da.keys() | db.keys() if da.get(k) != db.get(k))
        rest = {k: v for k, v in before.items() if k not in ("definitions", "$defs")} != \
               {k: v for k, v in after.items() if k not in ("definitions", "$defs")}
        definitions[path] = names + (["<top level>"] if rest else [])
    return {"identical": a == b, "files_old": len(a), "files_new": len(b),
            "added": sorted(b.keys() - a.keys()), "removed": sorted(a.keys() - b.keys()),
            "changed": changed, "definitions_changed": definitions}


def compare(old: Path, new: Path) -> dict:
    before, after = facts(old), facts(new)
    missing_old = sorted(k for k, v in before.items() if not v)
    missing_new = sorted(k for k, v in after.items() if not v)
    diff = schema_diff(old, new)
    if missing_old or missing_new:
        verdict = "STOP: protocol facts missing; do not re-pin"
    elif diff["identical"]:
        verdict = "IDENTICAL_SCHEMA: every fact present, no schema file differs"
    else:
        verdict = "COMPATIBLE_REVIEW_DIFF: every fact present; review the listed differences before re-pinning"
    return {"compatible": not missing_old and not missing_new, "facts_checked": len(after),
            "missing_in_old": missing_old, "missing_in_new": missing_new, "schema": diff, "verdict": verdict}


def _codex_processes() -> list:
    table = proc_tree.process_table()
    if table is None:
        return None
    return sorted((pid, name) for pid, _ppid, name in table if "codex" in (name or "").lower())


def preflight(label: str, *, root: Path = STAGE.RUNTIME_ROOT, baseline: Path = BASELINE,
              baseline_manifest: str = BASELINE_MANIFEST, sign=STAGE.signature) -> dict:
    """Passive checks; nothing is executed. Any entry in "problems" stops the capture."""
    problems = []
    candidate = STAGE.CANDIDATES.get(label)
    if candidate is None:
        return {"problems": [f"unknown_candidate:{label}"]}
    if candidate["purpose"] != "runtime":
        problems.append("not_a_runtime")  # an archive lacks companions; it is never run as one
    problems += STAGE.verify(label, root)
    folder = STAGE.target_dir(label, root)
    signatures = {}
    if not problems:
        for _source, name, _digest, _size in candidate["files"]:
            signatures[name] = (sign(folder / name) or {}).get("status")
        problems += [f"signature:{n}:{s}" for n, s in signatures.items() if s != "Valid"]
    if not baseline.is_dir():
        problems.append("baseline_missing")
    elif manifest(baseline) != baseline_manifest:
        problems.append("baseline_changed")
    return {"folder": str(folder), "signatures": signatures, "problems": problems}


def capture(label: str, *, root: Path = STAGE.RUNTIME_ROOT, baseline: Path = BASELINE,
            baseline_manifest: str = BASELINE_MANIFEST, run_root: Path = RUN_ROOT, sign=STAGE.signature) -> int:
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"step-a-{label}-", dir=run_root))
    report: dict = {"mode": "capture", "candidate": label, "model_turns": 0, "steps": [],
                    "version_inferred": (STAGE.CANDIDATES.get(label) or {}).get("version_inferred")}

    def finish(code: int) -> int:
        (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: report.get(k) for k in ("verdict", "version", "compatible")}, ensure_ascii=False))
        print(f"Evidence: {run_dir / 'report.json'}")
        return code

    checks = preflight(label, root=root, baseline=baseline, baseline_manifest=baseline_manifest, sign=sign)
    report["preflight"] = checks
    if checks["problems"]:
        report["verdict"] = "STOP: preflight failed; nothing executed"
        return finish(2)
    codex = Path(checks["folder"]) / "codex.exe"
    report["binary"] = str(codex)
    report["codex_processes_before"] = _codex_processes()
    scratch_home = Path(tempfile.mkdtemp(prefix="agenthub-stepa-home-"))
    env = {**os.environ, "CODEX_HOME": str(scratch_home)}

    def run(args: list[str], name: str) -> subprocess.CompletedProcess:
        started = time.monotonic()
        proc = subprocess.run([str(codex), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=COMMAND_TIMEOUT, env=env, cwd=str(scratch_home),
                              stdin=subprocess.DEVNULL)
        (run_dir / f"{name}.txt").write_text(proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""),
                                            encoding="utf-8")
        report["steps"].append({"args": args, "exit_code": proc.returncode,
                                "seconds": round(time.monotonic() - started, 2)})
        return proc

    def steps() -> int:
        version = run(["--version"], "version")
        report["version"] = version.stdout.strip().splitlines()[0] if version.stdout.strip() else None
        report["version_matches_inference"] = report["version"] == report["version_inferred"]
        helptext = run(["app-server", "generate-json-schema", "--help"], "schema-help")
        if version.returncode != 0 or helptext.returncode != 0 or "--out" not in helptext.stdout:
            report["verdict"] = "STOP: cannot capture the schema with this binary's CLI"
            return 3
        schema_dir = run_dir / "schema"
        args = ["app-server", "generate-json-schema"]
        if "--experimental" in helptext.stdout:
            args.append("--experimental")
        gen = run([*args, "--out", str(schema_dir)], "schema-generate")
        if gen.returncode != 0 or not (schema_dir / "ServerNotification.json").is_file():
            report["verdict"] = "STOP: schema generation failed"
            return 4
        report["capture_manifest"] = manifest(schema_dir)
        result = compare(baseline, schema_dir)
        report.update(result)
        return 0 if result["compatible"] else 5

    code = 6
    try:
        code = steps()
    except Exception as exc:  # noqa: BLE001 - a timeout or crash is a STOP, recorded by type only
        report["verdict"] = f"STOP: {type(exc).__name__}"
    finally:
        # Recorded before the report is written, so the evidence always shows the state after the run.
        report["codex_processes_after"] = _codex_processes()
        try:
            scratch_home.rmdir()  # only if Codex left it empty
            report["scratch_home_removed"] = True
        except OSError:
            report["scratch_home_removed"] = False
            report["scratch_home"] = str(scratch_home)
    return finish(code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "CAPTURE"))
    parser.add_argument("--capture", metavar="CANDIDATE", choices=sorted(STAGE.CANDIDATES))
    parser.add_argument("--approve-zero-model-identity-check", action="store_true")
    args = parser.parse_args()
    if args.compare:
        result = compare(Path(args.compare[0]), Path(args.compare[1]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["compatible"] else 5
    if args.capture:
        if not args.approve_zero_model_identity_check:
            print("REFUSED: Step A executes the staged Codex binary and needs the user's approval.")
            return 2
        return capture(args.capture)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
