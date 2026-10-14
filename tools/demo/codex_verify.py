r"""Single-invocation Codex verification over the real HTTP stack.

One script, two modes:

    --mode stub   (default) drives the whole stack with the stub process. No
                  Codex, no network, no quota spent. Use this to prove the path.
    --mode real   drives the REAL pinned Codex executable. Requires the operator's
                  explicit approval for the single call this performs.

What it guarantees for the run:

* an INDEPENDENT, EMPTY database: a fresh file is created and asserted empty
  before anything is scheduled, so no earlier task can take part;
* exactly ONE task tree and ONE invocation, created with agent_id="codex"
  explicitly - the script verifies the agent id it sent;
* the unapproved path is exercised FIRST and must launch nothing;
* after approval exactly ONE process launch happens, and the script says so -
  for a real run it also prints the exit code and JSONL event count, because a
  record alone only proves a launch was ATTEMPTED;
* real mode refuses to start unless CODEX_HOME is outside the repo AND holds a
  login credential, so it can never spend a call on a guaranteed auth failure.

It never prints the user token or the contents of any credential file.

Usage:
    .\.venv\Scripts\python.exe tools\demo\codex_verify.py --mode stub
    .\.venv\Scripts\python.exe tools\demo\codex_verify.py --mode real --i-understand-this-spends-quota

Override the home with `--codex-home <dir>` (must also be outside the repo).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx

# This console is not UTF-8 by default on Windows, so a Chinese check label would
# come out as mojibake exactly where the operator reads the verdict.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001  (not available on every stream type)
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRATCH = os.path.join(REPO, "var")
RUN_DIR = os.path.join(SCRATCH, "codex-verify")
STUB = os.path.join(HERE, "stub_codex.py")
ARGV_LOG = os.path.join(RUN_DIR, "stub-argv.log")
PORT = 8145
BASE = f"http://127.0.0.1:{PORT}"
USER = {"Authorization": "Bearer " + os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token")}

PROMPT = "Reply with exactly: OK. Do not modify any files."


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human_seconds(s: float) -> str:
    return f"{s:.1f}s"


def adapter_result(client, invocation_id: str) -> dict:
    return client.get(f"/api/invocations/{invocation_id}/adapter-result", headers=USER).json()


def launch_attempts(client, invocation_id: str, mode: str) -> int:
    """How many process launches were attempted for this invocation.

    Stub mode counts entries in the stub's own argv log - one line per process
    the adapter really started. Real mode uses the adapter's execution record:
    the adapter writes it only AFTER its pre-launch refusals, so a refused start
    can never be counted as a launch here.
    """
    if mode == "stub":
        if not os.path.exists(ARGV_LOG):
            return 0
        with open(ARGV_LOG, encoding="utf-8") as fh:
            return len([ln for ln in fh.read().splitlines() if ln.strip()])
    return 1 if adapter_result(client, invocation_id).get("available") else 0


def resolve_codex_home(mode: str) -> str:
    """Where the server process will keep CODEX_HOME.

    Stub mode needs no credential, so a temp directory is enough. Real mode needs
    a LOGGED-IN home, otherwise the call is guaranteed to fail authentication and
    we would be spending a real call to learn nothing. Both candidates are
    outside the repository, because CODEX_HOME can hold an ``auth.json`` copy.
    """
    override = os.environ.get("AGENT_HUB_CODEX_HOME")
    if override:
        return override
    if mode == "stub":
        return os.path.join(tempfile.gettempdir(), "agenthub-codex-verify-home")
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "AgentHub", "codex-home"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".codex"),
    ]
    for path in [p for p in candidates if p]:
        if os.path.isfile(os.path.join(path, "auth.json")):
            return path
    return candidates[0]


def build_env(mode: str, db_path: str, codex_home: str) -> dict:
    """Environment for the server process. Process-scoped only."""
    env = dict(os.environ)
    env.update({
        "AGENT_HUB_ADAPTER": "codex",
        "AGENT_HUB_DB_URL": "sqlite:///" + db_path.replace("\\", "/"),
        "AGENT_HUB_DATA_DIR": RUN_DIR,
        # CODEX_HOME stays OUTSIDE the repo: it can hold a credential copy.
        "AGENT_HUB_CODEX_HOME": codex_home,
        "AGENT_HUB_CODEX_TIMEOUT": os.environ.get("AGENT_HUB_CODEX_TIMEOUT", "300"),
        # Explicit, so an inherited value can never change what this run did.
        "AGENT_HUB_CODEX_SANDBOX": "read-only",
    })
    if mode == "stub":
        env.update({
            "AGENT_HUB_CODEX_PATH": STUB,
            "AGENT_HUB_CODEX_SHA256": sha256(STUB),
            "AGENT_HUB_CODEX_SIZE": str(os.path.getsize(STUB)),
            "AGENT_HUB_CODEX_PREFIX": json.dumps([sys.executable]),
            # Tell the stub where to log its argv so this script can count launches.
            "STUB_CODEX_ARGV_LOG": ARGV_LOG,
        })
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description="Single-invocation Codex verification.")
    ap.add_argument("--mode", choices=["stub", "real"], default="stub")
    ap.add_argument("--i-understand-this-spends-quota", action="store_true",
                    help="required for --mode real")
    ap.add_argument("--codex-home", default=None,
                    help="CODEX_HOME for the server (must be outside the repo)")
    ap.add_argument("--prompt", default=PROMPT,
                    help="task text; in stub mode 'FAIL'/'SLEEP' switch the outcome "
                         "(used as a negative control for the PASS conditions)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    global BASE
    BASE = f"http://127.0.0.1:{args.port}"
    mode = args.mode

    if mode == "real" and not args.i_understand_this_spends_quota:
        print("Refusing --mode real without --i-understand-this-spends-quota.")
        print("A real run launches the pinned Codex executable once and spends quota;")
        print("it needs the operator's explicit approval.")
        return 2

    codex_home = os.path.abspath(args.codex_home or resolve_codex_home(mode))
    repo = os.path.abspath(REPO)
    if codex_home.lower().startswith(repo.lower() + os.sep):
        print(f"REFUSING: CODEX_HOME is inside the repository: {codex_home}")
        print("It can hold a login credential; keep it outside the repo tree.")
        return 2
    credential = os.path.join(codex_home, "auth.json")
    credential_present = os.path.isfile(credential)
    if mode == "real" and not credential_present:
        # Refuse BEFORE spending a call: without a login this can only fail.
        print(f"REFUSING: no Codex login credential in CODEX_HOME: {codex_home}")
        print(f"  looked for: {credential}")
        print("A real call from an unauthenticated home would only spend a call to")
        print("learn that it is not logged in. Pass --codex-home <logged-in home>.")
        return 2
    if mode == "real":
        # Codex needs a WRITABLE home (logs/state/tmp). A sandbox policy can deny
        # writes to a directory that exists, so ask instead of assuming - and say
        # which of the two it is, because "denied" is not "missing".
        sys.path[:0] = [os.path.join(REPO, "apps", "worker")]
        from agenthub_kernel import config as kernel_config

        write_ok, write_detail = kernel_config.codex_home_write_check()
        if not write_ok:
            print(f"REFUSING: {write_detail}")
            print("The directory EXISTS - this is an ACCESS denial, not a missing home.")
            print("Run this command where it can be written, or pass a writable --codex-home.")
            return 2

    # ---- independent, empty database -----------------------------------
    os.makedirs(RUN_DIR, exist_ok=True)
    db_path = os.path.join(RUN_DIR, f"verify-{mode}-{int(time.time())}.db")
    assert not os.path.exists(db_path)
    if os.path.exists(ARGV_LOG):
        os.remove(ARGV_LOG)

    env = build_env(mode, db_path, codex_home)
    print("=" * 78)
    print(f"CODEX VERIFICATION - mode={mode}")
    print("=" * 78)
    print(f"  database   : {db_path}   (created fresh, asserted empty)")
    print(f"  CODEX_HOME : {codex_home}   (outside the repository)")
    print(
        "  credential : "
        + ("present; contents are never read or printed" if credential_present
           else "not present (not needed for this mode)")
    )
    if mode == "real":
        print("  write check: OK (CODEX_HOME is writable by this process)")
        print("  sandbox    : read-only (set explicitly for this run)")
    if mode == "real":
        default_home = os.path.abspath(resolve_codex_home("real"))
        if codex_home == default_home and default_home.endswith(os.path.join(".codex")):
            print("               NOTE: this is the operator's own Codex home; the run's")
            print("               session record will be written there.")
    if mode == "stub":
        print(f"  process    : STUB {STUB} (no Codex, no network)")
    else:
        print(f"  process    : REAL Codex (pinned; identity checked by the adapter)")

    server = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "serve.py"), "--port", str(args.port),
         "--no-pid-file"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=REPO,
    )

    try:
        c = httpx.Client(base_url=BASE, trust_env=False, timeout=120)
        for _ in range(60):
            try:
                if c.get("/ui").status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        else:
            print("server did not come up")
            return 2

        # ---- 1. the database must be empty ----------------------------
        tasks0 = c.get("/api/tasks", headers=USER).json()
        assert tasks0 == [], f"database was NOT empty: {tasks0}"
        print(f"\n[1] empty-database precondition: /api/tasks -> {tasks0}   OK")

        info = c.get("/api/adapter", headers=USER).json()
        print(f"[2] adapter: kind={info.get('kind')} identity_ok={info.get('identity_ok')} "
              f"sandbox={info.get('sandbox')} subagents_disabled={info.get('subagents_disabled')}")
        if mode == "real" and not info.get("identity_ok"):
            print(f"    REFUSING: executable identity check failed ({info.get('identity_detail')})")
            return 2

        # ---- 3. exactly one invocation, agent_id="codex" ---------------
        created = c.post("/api/invocations", json={
            "task_id": "-", "agent_id": "codex", "task_input": args.prompt,
        }).json()
        root, inv_id, appr_id = created["task_id"], created["invocation_id"], created["approval_id"]
        print(f"\n[3] created exactly one invocation:")
        print(f"      agent_id   = codex (as sent)")
        print(f"      task_id    = {root}")
        print(f"      invocation = {inv_id}")
        print(f"      approval   = {appr_id}  status={created['status']}")
        if created.get("adapter_warning"):
            print(f"      !! adapter_warning: {created['adapter_warning']}")

        tasks1 = c.get("/api/tasks", headers=USER).json()
        assert len(tasks1) == 1, f"expected exactly one task tree, got {len(tasks1)}"
        view = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        assert len(view["invocations"]) == 1, "expected exactly one invocation in the tree"
        assert view["invocations"][0]["id"] == inv_id
        print(f"      verified: 1 task tree, 1 invocation, 0 others in this database")

        # ---- 4. unapproved must launch nothing -------------------------
        c.post("/api/worker/drain", headers=USER)
        unapproved_launches = launch_attempts(c, inv_id, mode)
        print(f"\n[4] drain WITHOUT approval -> process launches = {unapproved_launches}   (must be 0)")
        assert unapproved_launches == 0, "SAFETY FAILURE: a process launched without approval"

        # ---- 5. approve, then exactly one launch -----------------------
        c.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        t0 = time.time()
        outcomes = c.post("/api/worker/drain", headers=USER).json()
        elapsed = time.time() - t0
        launches = launch_attempts(c, inv_id, mode)
        res = adapter_result(c, inv_id)
        evidence = res.get("evidence") or {}
        print(f"\n[5] after approval -> process launches = {launches}   (must be 1)")
        print(f"      drain outcomes: {json.dumps(outcomes)}")
        print(f"      elapsed       : {human_seconds(elapsed)}")
        if mode == "real":
            # A record proves a launch was ATTEMPTED; the events prove a process
            # really ran and spoke. Both are reported so neither is inferred.
            print(f"      process evidence: exit_code={evidence.get('exit_code')} "
                  f"timed_out={evidence.get('timed_out')} verdict={evidence.get('verdict')} "
                  f"jsonl_events={evidence.get('event_count')}")
        launch_ok = launches == 1
        if not launch_ok:
            print(f"      !! expected exactly one launch, got {launches}"
                  f" (detail: {res.get('detail') or 'n/a'})")
            print("      the run is reported below with its actual outcome")

        # ---- 6. the adapter's parsed result ----------------------------
        s = res.get("summary") or {}
        print(f"\n[6] adapter result")
        print(f"      status       = {res.get('status')}")
        print(f"      detail       = {res.get('detail')}")
        print(f"      thread_id    = {s.get('thread_id')}")
        print(f"      final reply  = {s.get('final_message')!r}")
        print(f"      usage        = {json.dumps(s.get('usage'), ensure_ascii=False)}")
        print(f"      event_types  = {json.dumps(s.get('event_types'), ensure_ascii=False)}")
        print(f"      workdir after= {s.get('workdir_entries_after')}")
        print(f"      driver errors= {evidence.get('error_count')}   "
              f"unparsed lines= {evidence.get('unparsed_count')}")

        # ---- 7. kernel state and quota ---------------------------------
        view2 = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        inv_state = view2["invocations"][0]["status"]
        quota = view2["quota"]
        print(f"\n[7] kernel state")
        print(f"      invocation status = {inv_state}")
        print(f"      quota             = {json.dumps(quota, ensure_ascii=False)}")
        print(f"      stopped={view2['stopped']}  paused={view2['paused']}")

        # ---- 8. what counts as a PASS or FAIL ---------------------------
        # Deliberately strict: "a process was launched and something happened" is
        # NOT a pass. A pass requires a completed turn, a clean exit, a delivered
        # reply, real usage, a settled invocation and no leaked reservation.
        checks = [
            ("未批准时启动数 = 0", unapproved_launches == 0),
            ("批准后启动尝试 = 恰好 1 次", launches == 1),
            ("进程确实运行（JSONL 事件 > 0）", bool(evidence.get("event_count"))),
            ("适配器判定 verdict = finished", evidence.get("verdict") == "finished"),
            ("进程退出码 = 0", evidence.get("exit_code") == 0),
            ("未发生本地超时", evidence.get("timed_out") is False),
            ("thread_id 非空", bool(s.get("thread_id"))),
            ("最终回复非空", bool((s.get("final_message") or "").strip())),
            ("返回真实 usage", bool(s.get("usage"))),
            ("内核状态 = succeeded", inv_state == "succeeded"),
            ("未释放的配额预留 = 0", quota.get("open_reservations") == 0),
            ("并发名额已释放 = 0", quota.get("concurrent") == 0),
        ]
        print("\n" + "=" * 78)
        print("CHECKS")
        print("=" * 78)
        for label, passed in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        failed = [label for label, passed in checks if not passed]
        ok = not failed

        print("\n" + "=" * 78)
        print("DIFF (before -> after)")
        print("=" * 78)
        print(f"  task trees        : 0 -> {len(tasks1)}")
        print(f"  invocations       : 0 -> 1")
        print(f"  process launches  : 0 (unapproved) -> {launches} (approved)"
              f"{'' if launches == 1 else '   <-- expected 1'}")
        print(f"  adapter record    : none -> {res.get('status')}")
        print(f"  thread_id         : none -> {s.get('thread_id') or 'none'}")
        print(f"  usage             : none -> {json.dumps(s.get('usage') or {}, ensure_ascii=False)}")
        print(f"  open reservations : 0 -> {quota.get('open_reservations')}")
        if mode == "real":
            print(f"  exit code         : none -> {evidence.get('exit_code')}")
            print(f"  jsonl events      : none -> {evidence.get('event_count')}")
            print(f"  verdict           : none -> {evidence.get('verdict')}")
        print("=" * 78)
        if ok:
            print("VERIFICATION PASSED")
            return 0
        print("VERIFICATION FAILED - unmet conditions:")
        for label in failed:
            print(f"  - {label}")
        return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except Exception:  # noqa: BLE001
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
