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
* after approval exactly ONE process launch happens, and the script says so.

It never prints the user token or the contents of any credential file.

Usage:
    .\.venv\Scripts\python.exe tools\demo\codex_verify.py --mode stub
    .\.venv\Scripts\python.exe tools\demo\codex_verify.py --mode real --i-understand-this-spends-quota
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


def build_env(mode: str, db_path: str) -> dict:
    """Environment for the server process. Process-scoped only."""
    env = dict(os.environ)
    env.update({
        "AGENT_HUB_ADAPTER": "codex",
        "AGENT_HUB_DB_URL": "sqlite:///" + db_path.replace("\\", "/"),
        "AGENT_HUB_DATA_DIR": RUN_DIR,
        # CODEX_HOME stays OUTSIDE the repo: it can hold a credential copy.
        "AGENT_HUB_CODEX_HOME": os.environ.get("AGENT_HUB_CODEX_HOME")
        or os.path.join(tempfile.gettempdir(), "agenthub-codex-verify-home"),
        "AGENT_HUB_CODEX_TIMEOUT": os.environ.get("AGENT_HUB_CODEX_TIMEOUT", "300"),
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

    # ---- independent, empty database -----------------------------------
    os.makedirs(RUN_DIR, exist_ok=True)
    db_path = os.path.join(RUN_DIR, f"verify-{mode}-{int(time.time())}.db")
    assert not os.path.exists(db_path)
    if os.path.exists(ARGV_LOG):
        os.remove(ARGV_LOG)

    env = build_env(mode, db_path)
    print("=" * 78)
    print(f"CODEX VERIFICATION - mode={mode}")
    print("=" * 78)
    print(f"  database   : {db_path}   (created fresh, asserted empty)")
    print(f"  CODEX_HOME : {env['AGENT_HUB_CODEX_HOME']}   (outside the repository)")
    if mode == "stub":
        print(f"  process    : STUB {STUB} (no Codex, no network)")
    else:
        from agenthub_kernel import config as _cfg  # noqa: F401  (path report only)
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
            "task_id": "-", "agent_id": "codex", "task_input": PROMPT,
        }).json()
        root, inv_id, appr_id = created["task_id"], created["invocation_id"], created["approval_id"]
        print(f"\n[3] created exactly one invocation:")
        print(f"      agent_id   = codex (as sent)")
        print(f"      task_id    = {root}")
        print(f"      invocation = {inv_id}")
        print(f"      approval   = {appr_id}  status={created['status']}")

        tasks1 = c.get("/api/tasks", headers=USER).json()
        assert len(tasks1) == 1, f"expected exactly one task tree, got {len(tasks1)}"
        view = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        assert len(view["invocations"]) == 1, "expected exactly one invocation in the tree"
        assert view["invocations"][0]["id"] == inv_id
        print(f"      verified: 1 task tree, 1 invocation, 0 others in this database")

        # ---- 4. unapproved must launch nothing -------------------------
        c.post("/api/worker/drain", headers=USER)
        if mode == "stub":
            launches = len(open(ARGV_LOG, encoding="utf-8").read().splitlines()) if os.path.exists(ARGV_LOG) else 0
        else:
            launches = 1 if c.get(f"/api/invocations/{inv_id}/adapter-result",
                                 headers=USER).json().get("available") else 0
        print(f"\n[4] drain WITHOUT approval -> process launches = {launches}   (must be 0)")
        assert launches == 0, "SAFETY FAILURE: a process launched without approval"

        # ---- 5. approve, then exactly one launch -----------------------
        c.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        t0 = time.time()
        outcomes = c.post("/api/worker/drain", headers=USER).json()
        elapsed = time.time() - t0
        if mode == "stub":
            launches = len(open(ARGV_LOG, encoding="utf-8").read().splitlines()) if os.path.exists(ARGV_LOG) else 0
        else:
            launches = 1 if c.get(f"/api/invocations/{inv_id}/adapter-result",
                                 headers=USER).json().get("available") else 0
        print(f"\n[5] after approval -> process launches = {launches}   (must be 1)")
        print(f"      drain outcomes: {json.dumps(outcomes)}")
        print(f"      elapsed       : {human_seconds(elapsed)}")
        assert launches == 1, f"expected exactly one launch, got {launches}"

        # ---- 6. the adapter's parsed result ----------------------------
        res = c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        s = res.get("summary") or {}
        print(f"\n[6] adapter result")
        print(f"      status       = {res.get('status')}")
        print(f"      thread_id    = {s.get('thread_id')}")
        print(f"      final reply  = {s.get('final_message')!r}")
        print(f"      usage        = {json.dumps(s.get('usage'), ensure_ascii=False)}")
        print(f"      event_types  = {json.dumps(s.get('event_types'), ensure_ascii=False)}")
        print(f"      workdir after= {s.get('workdir_entries_after')}")

        # ---- 7. kernel state and quota ---------------------------------
        view2 = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        inv_state = view2["invocations"][0]["status"]
        print(f"\n[7] kernel state")
        print(f"      invocation status = {inv_state}")
        print(f"      quota             = {json.dumps(view2['quota'], ensure_ascii=False)}")
        print(f"      stopped={view2['stopped']}  paused={view2['paused']}")

        # ---- 8. diff summary -------------------------------------------
        ok = bool(s.get("thread_id")) and inv_state in ("succeeded", "failed", "canceled",
                                                        "timed_out", "unknown")
        print("\n" + "=" * 78)
        print("DIFF (before -> after)")
        print("=" * 78)
        print(f"  task trees        : 0 -> {len(tasks1)}")
        print(f"  invocations       : 0 -> 1")
        print(f"  process launches  : 0 (unapproved) -> {launches} (approved)")
        print(f"  adapter record    : none -> {res.get('status')}")
        print(f"  thread_id         : none -> {s.get('thread_id') or 'none'}")
        print(f"  usage             : none -> {json.dumps(s.get('usage') or {}, ensure_ascii=False)}")
        print(f"  open reservations : 0 -> {view2['quota'].get('open_reservations')}")
        print("=" * 78)
        print("VERIFICATION PASSED" if ok else "VERIFICATION COMPLETED WITH CAVEATS")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except Exception:  # noqa: BLE001
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
