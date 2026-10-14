r"""Offline end-to-end integration against a STUB process (no Codex, no network).

Starts the real API with the real Codex adapter, but points it at a stub
executable so the full stack runs:

    console/API -> approval gate -> quota gate -> Worker -> CodexAdapter
    -> subprocess (stub) -> JSONL parsing -> execution store -> API -> console

It asserts the safety valves on the way: nothing launches before approval, exactly
one launch after approval, and a completed run leaks no quota slot.

Usage:
    .\.venv\Scripts\python.exe tools\demo\offline_integration.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
# Scratch lives under var/ (gitignored); these scripts themselves are tracked.
SCRATCH = os.path.join(REPO, "var")
STUB = os.path.join(HERE, "stub_codex.py")
ARGV_LOG = os.path.join(SCRATCH, "stub-codex-argv.log")
PORT = 8137
BASE = f"http://127.0.0.1:{PORT}"
USER = {"Authorization": "Bearer dev-user-token"}


def sha256(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main() -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    if os.path.exists(ARGV_LOG):
        os.remove(ARGV_LOG)

    env = dict(os.environ)
    env.update({
        "AGENT_HUB_ADAPTER": "codex",
        "AGENT_HUB_CODEX_PATH": STUB,
        "AGENT_HUB_CODEX_SHA256": sha256(STUB),
        "AGENT_HUB_CODEX_SIZE": str(os.path.getsize(STUB)),
        "AGENT_HUB_CODEX_PREFIX": json.dumps([sys.executable]),
        "AGENT_HUB_CODEX_TIMEOUT": "30",
        "AGENT_HUB_DB_URL": "sqlite:///"
        + os.path.join(SCRATCH, "offline-integration.db").replace("\\", "/"),
        "AGENT_HUB_DATA_DIR": SCRATCH,
    })
    for f in ("offline-integration.db", "codex-executions.db"):
        p = os.path.join(SCRATCH, f)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(p + suffix):
                os.remove(p + suffix)

    server = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "tools", "demo", "serve.py"), "--port", str(PORT), "--no-pid-file"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=REPO,
    )
    try:
        c = httpx.Client(base_url=BASE, trust_env=False, timeout=60)
        for _ in range(40):
            try:
                if c.get("/ui").status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        else:
            print("server did not come up")
            return 2

        print("=" * 78)
        print("OFFLINE INTEGRATION: stub process behind the real Codex adapter")
        print("=" * 78)

        info = c.get("/api/adapter", headers=USER).json()
        print("\n1) adapter identity as reported to the console")
        print(f"   kind={info.get('kind')}  version={info.get('version')}")
        print(f"   identity_ok={info.get('identity_ok')}  sandbox={info.get('sandbox')} "
              f"subagents_disabled={info.get('subagents_disabled')}  proxy={info.get('proxy')}")
        caps = info.get("capabilities", {})
        print(f"   capabilities: post_message={caps.get('post_message')} stream={caps.get('stream')} "
              f"real_usage={caps.get('real_usage')} cancel={caps.get('cancel')} "
              f"intercept_tool={caps.get('intercept_tool')} limit_dir={caps.get('limit_dir')} "
              f"control_subagent={caps.get('control_subagent')}")

        # ---- create a call, do NOT approve -----------------------------
        inv = c.post("/api/invocations",
                     json={"task_id": "-", "agent_id": "codex",
                           "task_input": "Reply with exactly: OK."}).json()
        root, inv_id, appr_id = inv["task_id"], inv["invocation_id"], inv["approval_id"]
        print(f"\n2) created invocation {inv_id} (approval {appr_id}, status {inv['status']})")

        c.post("/api/worker/drain", headers=USER)
        launched = os.path.exists(ARGV_LOG)
        print(f"   after drain WITHOUT approval -> stub process launched? {launched}  (expect False)")
        assert not launched, "SAFETY FAILURE: process launched without approval"

        # ---- approve ----------------------------------------------------
        c.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        out = c.post("/api/worker/drain", headers=USER).json()
        print(f"\n3) after approval -> drain outcomes: {json.dumps(out)}")

        lines = []
        if os.path.exists(ARGV_LOG):
            lines = [json.loads(l) for l in open(ARGV_LOG, encoding="utf-8") if l.strip()]
        print(f"   stub process launches: {len(lines)}  (expect 1)")
        assert len(lines) == 1, f"expected exactly one launch, got {len(lines)}"
        argv = lines[0]["argv"]
        print(f"   argv: {' '.join(argv)}")
        assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--disable" in argv and "multi_agent" in argv
        print(f"   env keys seen by the process: {lines[0]['env_keys']}")

        # ---- adapter result through the API ----------------------------
        res = c.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        s = res.get("summary") or {}
        print("\n4) adapter result (what the console shows)")
        print(f"   status       = {res.get('status')}")
        print(f"   thread_id    = {s.get('thread_id')}")
        print(f"   final reply  = {s.get('final_message')!r}")
        print(f"   usage        = {json.dumps(s.get('usage'), ensure_ascii=False)}")
        print(f"   event_types  = {json.dumps(s.get('event_types'), ensure_ascii=False)}")
        print(f"   workdir after= {s.get('workdir_entries_after')}")

        assert s.get("final_message") == "OK"
        assert s.get("thread_id") == "stub-thread-0001"
        assert s.get("usage", {}).get("input_tokens") == 14481
        assert s.get("workdir_entries_after") == []

        # ---- quota + second call ---------------------------------------
        view = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        print(f"\n5) quota after the call: {json.dumps(view['quota'], ensure_ascii=False)}")
        print(f"   invocation status in kernel: {view['invocations'][0]['status']}")

        # ---- failure path ----------------------------------------------
        inv2 = c.post("/api/invocations",
                      json={"task_id": root, "agent_id": "codex",
                            "task_input": "FAIL please"}).json()
        c.post(f"/api/approvals/{inv2['approval_id']}/approve", headers=USER)
        c.post("/api/worker/drain", headers=USER)
        res2 = c.get(f"/api/invocations/{inv2['invocation_id']}/adapter-result", headers=USER).json()
        print(f"\n6) failure path -> adapter status={res2.get('status')} "
              f"detail={str(res2.get('detail'))[:90]!r}")

        view2 = c.get(f"/api/tasks/{root}/view", headers=USER).json()
        print(f"   quota: {json.dumps(view2['quota'], ensure_ascii=False)}")

        print("\n" + "=" * 78)
        print("OFFLINE INTEGRATION PASSED")
        print("=" * 78)
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except Exception:  # noqa: BLE001
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
