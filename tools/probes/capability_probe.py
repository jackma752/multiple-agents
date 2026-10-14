r"""Capability probes: turn "the flag exists" into observed behaviour.

Four Codex capabilities are currently UNVERIFIED and must not be described as
supported: read-only write refusal, native sub-agent blocking, pre-execution tool
interception, and cancellation. This tool runs ONE probe per invocation of the
real HTTP stack (API -> Worker -> approval gate -> quota gate -> Codex adapter),
then judges what it observed with the pure rules in
``agenthub_adapters.codex_probes``.

Design rules, in order of importance:

1. **The prompt is identical in stub and real mode.** In stub mode the outcome is
   chosen by ``STUB_CODEX_PROBE_OUTCOME`` (an environment variable), never by
   changing the text, so what a reviewer approves is exactly what real mode sends.
2. **No evidence means INCONCLUSIVE, not PASS.** "The file is not there" is not
   proof that a write was refused; the run may never have tried.
3. **One probe = one real call, at most.** Each probe prints its own cost, its
   permissions and its cleanup before doing anything.
4. **The sandbox is never relaxed.** Probes run read-only, with sub-agents
   disabled, and the only probe that asks for a tool call uses a harmless `echo`
   and needs its own explicit flag (`--i-understand-this-runs-a-shell-command`).

Usage (nothing real happens without the flags):

    # review the four plans, no side effects
    .\.venv\Scripts\python.exe tools\probes\capability_probe.py --list

    # offline: exercises the machinery and the judging logic with the stub
    .\.venv\Scripts\python.exe tools\probes\capability_probe.py --probe readonly-write --mode stub

    # real: one call, needs an explicit acknowledgement
    .\.venv\Scripts\python.exe tools\probes\capability_probe.py --probe readonly-write --mode real \
        --i-understand-this-spends-quota --codex-home "$env:LOCALAPPDATA\AgentHub\codex-home"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import httpx

# The Windows console is not UTF-8 by default; without this the Chinese verdict
# text is mojibake exactly where the operator has to read it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path[:0] = [
    os.path.join(REPO, "packages", "contracts"),
    os.path.join(REPO, "packages", "adapters"),
    os.path.join(REPO, "apps", "worker"),
]

from agenthub_adapters import codex_probes as probes  # noqa: E402

STUB = os.path.join(REPO, "tools", "demo", "stub_codex.py")
SERVE = os.path.join(REPO, "tools", "demo", "serve.py")
RUN_ROOT = os.path.join(REPO, "var", "probes")
PORT = 8150
USER = {"Authorization": "Bearer " + os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token")}

#: Which stub scripted outcome exercises which probe. Chosen so that the offline
#: run proves the judge can tell the good case from the bad one.
STUB_OUTCOMES = {
    "readonly-write": "refuse",          # attempt + driver refusal  -> PASS
    "subagent-block": "spawn-refused",   # attempt + driver refusal  -> PASS
    "tool-intercept": "command",         # ran with no approval       -> FAIL
    "cancel": "slow",
}

#: The read-only probe's paired control must actually write, or it proves nothing.
CONTROL_OUTCOME = "write"
#: Everything except an approved control runs with THIS sandbox, explicitly.
READ_ONLY = "read-only"
CONTROL_SANDBOX = "workspace-write"

#: Adapter timeout per probe (seconds). The cancel probe needs a window in which
#: the call is in flight, then a local timeout to end it.
TIMEOUTS = {"cancel": 25.0}
DEFAULT_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# printing the plan (reviewable without running anything)
# --------------------------------------------------------------------------- #


def print_plan(spec: probes.ProbeSpec) -> None:
    print("=" * 78)
    print(f"PROBE {spec.name}: {spec.title}")
    print("=" * 78)
    print(f"  验证什么     : {spec.claim}")
    print(f"  提示词       : {spec.prompt}")
    print(f"  权限范围     : {spec.permissions}")
    print(f"  沙箱         : {spec.sandbox}（由本工具显式设定并在启动前回读核对）")
    print(f"  额外审批     : {spec.extra_approval}")
    print(f"  真实调用次数 : {spec.real_calls}（stub 模式为 0）")
    print(f"  判定=PASS    : {spec.pass_criteria}")
    print(f"  判定=FAIL    : {spec.fail_criteria}")
    print(f"  判定=无法判断: {spec.inconclusive_criteria}")
    print(f"  清理步骤     : {spec.cleanup}")
    print(f"  局限         : {spec.limitations}")
    if spec.expected_outcome:
        print(f"  预期结果     : {spec.expected_outcome}")
    if not spec.verifiable_now:
        print("  ⚠ 现状       : 本项在当前调用模式下**不具备完全验证条件**（见局限）")
    print()


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_real_home(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    override = os.environ.get("AGENT_HUB_CODEX_HOME")
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AgentHub", "codex-home")


def build_env(mode: str, probe: str, db_path: str, run_dir: str, home: str,
              outcome: str = "", sandbox: str = READ_ONLY) -> dict:
    from agenthub_kernel import config  # imported here: path is set up above

    env = dict(os.environ)
    if sandbox not in (READ_ONLY, CONTROL_SANDBOX):
        raise SystemExit(f"refusing an unsupported sandbox: {sandbox!r}")
    env.update({
        "AGENT_HUB_ADAPTER": "codex",
        "AGENT_HUB_DB_URL": "sqlite:///" + db_path.replace("\\", "/"),
        "AGENT_HUB_DATA_DIR": run_dir,
        "AGENT_HUB_CODEX_HOME": home,
        "AGENT_HUB_CODEX_TIMEOUT": str(TIMEOUTS.get(probe, DEFAULT_TIMEOUT)),
        # ALWAYS set explicitly, so an inherited AGENT_HUB_CODEX_SANDBOX (or a typo)
        # can never quietly widen a probe: the two allowed values are named here and
        # nothing else is accepted. danger-full-access is not reachable from this tool.
        "AGENT_HUB_CODEX_SANDBOX": sandbox,
    })
    if mode == "stub":
        env.update({
            "AGENT_HUB_CODEX_PATH": STUB,
            "AGENT_HUB_CODEX_SHA256": sha256(STUB),
            "AGENT_HUB_CODEX_SIZE": str(os.path.getsize(STUB)),
            "AGENT_HUB_CODEX_PREFIX": json.dumps([sys.executable]),
            "STUB_CODEX_ARGV_LOG": os.path.join(run_dir, "stub-argv.log"),
        })
        if outcome:
            env["STUB_CODEX_PROBE_OUTCOME"] = outcome
    else:
        env["AGENT_HUB_CODEX_PATH"] = os.environ.get(
            "AGENT_HUB_CODEX_PATH", config.PINNED_CODEX_PATH
        )
    return env


class Server:
    """A console server for one probe run, stopped by its own handle."""

    def __init__(self, mode: str, probe: str, run_dir: str, home: str,
                 outcome: str = "", port: int = PORT, sandbox: str = READ_ONLY) -> None:
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.db_path = os.path.join(run_dir, f"probe-{probe}-{mode}-{int(time.time())}.db")
        self.env = build_env(mode, probe, self.db_path, run_dir, home, outcome, sandbox)
        self.expected_sandbox = sandbox
        self.proc: subprocess.Popen | None = None
        self.client = httpx.Client(base_url=self.base, trust_env=False, timeout=180)

    def __enter__(self) -> "Server":
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        assert not os.path.exists(self.db_path), "probe database must start absent"
        self.proc = subprocess.Popen(
            [sys.executable, SERVE, "--port", str(self.port), "--no-pid-file"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=self.env, cwd=REPO,
        )
        try:
            for _ in range(60):
                try:
                    if self.client.get("/ui").status_code == 200:
                        self.verify_sandbox()
                        return self
                except SystemExit:
                    raise
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.5)
            raise SystemExit("server did not come up")
        except BaseException:
            # __exit__ does not run when __enter__ raises, so stop the server here:
            # a refused probe must not leave a console process behind.
            self._terminate()
            self.client.close()
            raise

    def verify_sandbox(self) -> None:
        """Fail closed unless the running adapter has the sandbox we intended.

        The point of a probe is to describe the configuration it ran under, so the
        configuration is read back from the server instead of assumed from the
        environment we passed.
        """
        info = self.client.get("/api/adapter", headers=USER).json()
        actual = str(info.get("sandbox") or "")
        if actual != self.expected_sandbox:
            raise SystemExit(
                f"REFUSING: adapter reports sandbox={actual!r}, probe expected "
                f"{self.expected_sandbox!r}"
            )
        if info.get("kind") != "codex":
            raise SystemExit(f"REFUSING: adapter kind is {info.get('kind')!r}, not codex")
        if not info.get("subagents_disabled"):
            raise SystemExit("REFUSING: the adapter does not report sub-agents disabled")
        print(f"  已核对实际适配器配置：sandbox={actual} subagents_disabled=True "
              f"identity_ok={info.get('identity_ok')}")

    def _terminate(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    def __exit__(self, *_exc) -> None:
        self._terminate()
        self.client.close()


def read_driver_channel(run_dir: str, invocation_id: str) -> dict:
    """Read the DRIVER channel for one invocation from the adapter's own store.

    The API deliberately returns only derived, content-free fields, so the refusal
    evidence a judge needs (JSONL ``error`` messages and the CLI's non-JSON output)
    is read here, from the store this probe created. The assistant's own words are
    never used as proof, which is exactly why this is a separate step.
    """
    import sqlite3

    path = os.path.join(run_dir, "codex-executions.db")
    if not os.path.exists(path):
        return {"errors": [], "unparsed_lines": [], "note": "no execution store to read"}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload FROM codex_executions WHERE invocation_id = ?", (invocation_id,)
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return {"errors": [], "unparsed_lines": [], "note": f"store unreadable: {exc}"}
    if row is None:
        return {"errors": [], "unparsed_lines": [], "note": "no record for this invocation"}
    payload = json.loads(row["payload"] or "{}")
    return {
        "errors": [str(e) for e in (payload.get("errors") or [])],
        "unparsed_lines": [str(line) for line in (payload.get("unparsed_lines") or [])],
        "note": "read from the adapter's own record (uses file:...?mode=ro)",
    }


def create_call(client: httpx.Client, prompt: str) -> dict:
    created = client.post("/api/invocations", json={
        "task_id": "-", "agent_id": "codex", "task_input": prompt,
    }).json()
    tasks = client.get("/api/tasks", headers=USER).json()
    assert len(tasks) == 1, f"the probe database must hold exactly one tree, got {len(tasks)}"
    view = client.get(f"/api/tasks/{created['task_id']}/view", headers=USER).json()
    assert len(view["invocations"]) == 1, "the probe must schedule exactly one invocation"
    return created


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #


def run_simple_probe(probe: str, mode: str, home: str, verbose: bool = True,
                     outcome: str = "", sandbox: str = READ_ONLY) -> dict:
    """readonly-write / subagent-block / tool-intercept: one approved call."""
    spec = probes.PROBE_SPECS[probe]
    os.makedirs(RUN_ROOT, exist_ok=True)
    tag = f"{probe}-{mode}" + ("-control" if sandbox != READ_ONLY else "")
    run_dir = os.path.join(RUN_ROOT, tag)
    os.makedirs(run_dir, exist_ok=True)

    with Server(mode, probe, run_dir, home, outcome or STUB_OUTCOMES.get(probe, ""),
                sandbox=sandbox) as server:
        client = server.client
        assert client.get("/api/tasks", headers=USER).json() == [], "database not empty"
        created = create_call(client, spec.prompt)
        inv_id, appr_id = created["invocation_id"], created["approval_id"]
        root = created["task_id"]
        if verbose:
            print(f"  invocation {inv_id}  approval {appr_id}  agent_id=codex")

        # Unapproved first: the safety valve, re-checked on every probe run.
        client.post("/api/worker/drain", headers=USER)
        before = client.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        if verbose:
            print(f"  未批准 drain -> adapter record available = {before.get('available')} (must be False)")
        assert before.get("available") is False, "SAFETY FAILURE: launched without approval"

        client.post(f"/api/approvals/{appr_id}/approve", headers=USER)
        outcomes = client.post("/api/worker/drain", headers=USER).json()
        res = client.get(f"/api/invocations/{inv_id}/adapter-result", headers=USER).json()
        view = client.get(f"/api/tasks/{root}/view", headers=USER).json()
        workdir = os.path.join(run_dir, "codex-runs", inv_id)
        disk_entries = sorted(os.listdir(workdir)) if os.path.isdir(workdir) else None
        driver = read_driver_channel(run_dir, inv_id)

        payload = {
            "probe": probe, "mode": mode, "invocation_id": inv_id,
            "drain_outcomes": outcomes, "adapter_result": res, "view": view,
            "workdir": workdir, "workdir_entries_on_disk": disk_entries,
            "driver_channel": driver,
            "errors": driver.get("errors") or [],
            "unparsed_lines": driver.get("unparsed_lines") or [],
        }
        if verbose:
            preview = json.dumps({k: v for k, v in res.get("evidence", {}).items()
                                  if k != "event_types"}, ensure_ascii=False)
            print(f"  drain outcomes: {json.dumps(outcomes, ensure_ascii=False)}")
            print(f"  adapter status: {res.get('status')}  evidence: {preview}")
            print(f"  workdir on disk: {disk_entries}")
            print(f"  驱动通道（错误/非 JSON 行）: errors={len(payload['errors'])} "
                  f"unparsed={len(payload['unparsed_lines'])}  [{driver.get('note')}]")

    return payload


def run_cancel_probe(mode: str, home: str, verbose: bool = True) -> dict:
    """A long call, a stop request while it runs, then an honest outcome.

    The call is started in a background thread because `codex exec` is
    synchronous: the drain request does not return until the process ends.
    """
    spec = probes.PROBE_SPECS["cancel"]
    run_dir = os.path.join(RUN_ROOT, f"cancel-{mode}")
    os.makedirs(run_dir, exist_ok=True)

    with Server(mode, "cancel", run_dir, home, STUB_OUTCOMES["cancel"]) as server:
        client = server.client
        assert client.get("/api/tasks", headers=USER).json() == []
        created = create_call(client, spec.prompt)
        inv_id, appr_id = created["invocation_id"], created["approval_id"]
        root = created["task_id"]
        client.post(f"/api/approvals/{appr_id}/approve", headers=USER)

        # Start the (blocking) call.
        holder: dict = {}

        def drain() -> None:
            try:
                holder["drain"] = client.post("/api/worker/drain", headers=USER).json()
            except Exception as exc:  # noqa: BLE001
                holder["drain_error"] = f"{type(exc).__name__}: {exc}"

        worker = threading.Thread(target=drain, daemon=True)
        worker.start()

        # Wait until the call is really in flight before asking to stop it.
        live = ""
        for _ in range(60):
            view = client.get(f"/api/tasks/{root}/view", headers=USER).json()
            live = view["invocations"][0]["status"]
            if live in ("starting", "running"):
                break
            time.sleep(0.5)
        if verbose:
            print(f"  调用状态（请求停止前）= {live}")

        stop = client.post(f"/api/tasks/{root}/stop", headers=USER).json()
        if verbose:
            print(f"  stop: attempted={stop.get('cancelled')} confirmed={stop.get('confirmed')} "
                  f"unconfirmed={stop.get('unconfirmed')}")
            print(f"  outcomes: {json.dumps(stop.get('outcomes'), ensure_ascii=False)}")

        after = client.get(f"/api/tasks/{root}/view", headers=USER).json()
        status_after_stop = after["invocations"][0]["status"]
        quota_after_stop = dict(after["quota"])

        worker.join(timeout=TIMEOUTS["cancel"] + 40)
        final = client.get(f"/api/tasks/{root}/view", headers=USER).json()
        cancel_result = (stop.get("outcomes") or [{}])[0].get("cancel", {})
        payload = {
            "probe": "cancel", "mode": mode, "invocation_id": inv_id,
            "status_when_stopped": live,
            "adapter_cancel_result": cancel_result,
            "kernel_status_after_stop": status_after_stop,
            "slot_kept_after_stop": quota_after_stop.get("open_reservations", 0) > 0,
            "quota_after_stop": quota_after_stop,
            "final_status": final["invocations"][0]["status"],
            "final_quota": dict(final["quota"]),
            "drain_outcomes": holder.get("drain"),
            "drain_error": holder.get("drain_error"),
            "adapter_result": client.get(
                f"/api/invocations/{inv_id}/adapter-result", headers=USER
            ).json(),
        }
        if verbose:
            print(f"  停止后状态 = {status_after_stop}  名额保留 = {payload['slot_kept_after_stop']}")
            print(f"  最终状态   = {payload['final_status']}  配额 = {json.dumps(final['quota'])}")
    return payload


# --------------------------------------------------------------------------- #
# judging
# --------------------------------------------------------------------------- #


def evidence_events(evidence: dict) -> list[dict]:
    """Rebuild the event shape the judges need, from the adapter's own record.

    `/api/invocations/{id}/adapter-result` deliberately returns derived fields
    (thread ids, item-type counts, top-level event counts) rather than the raw
    JSONL, so this reconstructs the three things the judges look at: which sessions
    were announced, which item types the run produced, and which top-level event
    types occurred (that is where an `approval_request` would show up). It carries
    no message text.
    """
    events: list[dict] = []
    for tid in evidence.get("thread_ids") or []:
        events.append({"type": "thread.started", "thread_id": tid})
    for item_type, count in (evidence.get("item_types") or {}).items():
        for _ in range(int(count or 0)):
            events.append({"type": "item.completed", "item": {"type": item_type}})
    handled = ("thread.started", "item.completed")
    for event_type, count in (evidence.get("event_types") or {}).items():
        if event_type in handled:
            continue
        for _ in range(int(count or 0)):
            events.append({"type": event_type})
    return events


def judge(probe: str, payload: dict, scripted: bool) -> probes.ProbeVerdict:
    res = payload.get("adapter_result") or {}
    summary = res.get("summary") or {}
    evidence = res.get("evidence") or {}
    events = evidence_events(evidence)
    # `launched` is the adapter's own statement that it got past its pre-launch
    # refusals; without it, no observation in this run is attributable to Codex.
    launched = bool(res.get("available")) and bool(evidence.get("event_count"))
    unparsed = list(payload.get("unparsed_lines") or [])
    driver_errors = list(payload.get("errors") or [])

    if probe == "readonly-write":
        # Two independent sources: the adapter's own post-run listing and the disk.
        on_disk = payload.get("workdir_entries_on_disk")
        entries = on_disk if on_disk is not None else summary.get("workdir_entries_after")
        return probes.judge_readonly_write(
            workdir_entries=entries,
            final_message=summary.get("final_message", ""),
            events=events,
            errors=[res.get("detail", "")] + driver_errors,
            unparsed=unparsed,
            exit_code=evidence.get("exit_code"),
            timed_out=bool(evidence.get("timed_out")),
            launched=launched,
            target_name="PROBE_WRITE.txt",
        )
    if probe == "subagent-block":
        return probes.judge_subagent_block(
            events=events,
            final_message=summary.get("final_message", ""),
            errors=[res.get("detail", "")] + driver_errors,
            unparsed=unparsed,
            exit_code=evidence.get("exit_code"),
            timed_out=bool(evidence.get("timed_out")),
            launched=launched,
            disable_flag_passed=True,
        )
    if probe == "tool-intercept":
        return probes.judge_tool_interception(
            events=events,
            final_message=summary.get("final_message", ""),
            errors=[res.get("detail", "")],
            exit_code=evidence.get("exit_code"),
            timed_out=bool(evidence.get("timed_out")),
            launched=launched,
        )
    if probe == "cancel":
        return probes.judge_cancel(
            adapter_cancel_result=payload.get("adapter_cancel_result"),
            kernel_status=payload.get("kernel_status_after_stop", ""),
            slot_kept=bool(payload.get("slot_kept_after_stop")),
            adapter_claims_cancel=False,
        )
    raise SystemExit(f"unknown probe {probe}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one Codex capability probe.")
    ap.add_argument("--probe", choices=sorted(probes.PROBE_SPECS), default=None)
    ap.add_argument("--mode", choices=["stub", "real"], default="stub")
    ap.add_argument("--list", action="store_true", help="print the four plans and exit")
    ap.add_argument("--codex-home", default=None,
                    help="CODEX_HOME for real mode (must be outside the repo and logged in)")
    ap.add_argument("--i-understand-this-spends-quota", action="store_true",
                    help="required for --mode real: this probe performs one real call")
    ap.add_argument("--i-understand-this-runs-a-shell-command", action="store_true",
                    help="required for the tool-intercept probe in real mode")
    ap.add_argument("--with-write-control", action="store_true",
                    help="readonly-write only: also run the same prompt against "
                         "--sandbox workspace-write as a positive control (2nd real call)")
    ap.add_argument("--i-understand-this-widens-the-sandbox", action="store_true",
                    help="required for --with-write-control in real mode")
    ap.add_argument("--stub-outcome", default=None,
                    help="STUB MODE ONLY: force one scripted stub outcome "
                         "(refuse|write|lie|spawn|no-spawn|command|approval|slow). "
                         "Used for negative controls: the judge must not pass these")
    ap.add_argument("--quiet", action="store_true",
                    help="print the verdict and observations only, not the full plan "
                         "(the plan is what a reviewer approves; use --list to read it)")
    ap.add_argument("--fail-on-fail", action="store_true",
                    help="exit 1 when the verdict is FAIL (default: a FAIL is a reportable "
                         "capability gap, not a tool error, so the exit code stays 0)")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.list or not args.probe:
        print("CAPABILITY PROBES - review these before approving anything\n")
        for spec in probes.PROBE_SPECS.values():
            print_plan(spec)
        print("Nothing was executed. Real mode additionally requires")
        print("--i-understand-this-spends-quota (and, for tool-intercept,")
        print("--i-understand-this-runs-a-shell-command).")
        print()
        print("Sandbox policy: every probe except an APPROVED paired control runs with")
        print("--sandbox read-only, set explicitly by this tool and read back from the")
        print("running adapter before any call. danger-full-access is not reachable here.")
        return 0

    spec = probes.PROBE_SPECS[args.probe]
    if args.quiet:
        print(f"PROBE {spec.name}: {spec.title}  (mode={args.mode})")
    else:
        print_plan(spec)

    home = resolve_real_home(args.codex_home) if args.mode == "real" else \
        os.path.join(tempfile.gettempdir(), "agenthub-probe-home")
    if args.mode == "real":
        if not args.i_understand_this_spends_quota:
            print("REFUSING: --mode real needs --i-understand-this-spends-quota")
            return 2
        if args.probe == "tool-intercept" and not args.i_understand_this_runs_a_shell_command:
            print("REFUSING: this probe asks the agent to run a shell command (echo).")
            print("Approving a run is NOT approving its tool calls; pass")
            print("--i-understand-this-runs-a-shell-command if you approve that separately.")
            return 2
        if args.with_write_control and not args.i_understand_this_widens_the_sandbox:
            print("REFUSING: --with-write-control runs the same prompt against")
            print("--sandbox workspace-write, which makes the sandbox writable. That is a")
            print("separate, larger permission than the read-only probe; pass")
            print("--i-understand-this-widens-the-sandbox if you approve it.")
            return 2
        repo = os.path.abspath(REPO).lower()
        if home.lower().startswith(repo + os.sep):
            print(f"REFUSING: CODEX_HOME is inside the repository: {home}")
            return 2
        credential = os.path.join(home, "auth.json")
        if not os.path.isfile(credential):
            print(f"REFUSING: no login credential in {home}")
            print("A real probe from an unauthenticated home would only waste the call.")
            return 2
        # Codex needs a WRITABLE home (it previously failed with `os error 5` when
        # it could not write $CODEX_HOME/tmp). A sandbox policy can deny writes to a
        # directory that exists, so ask instead of assuming - and say which of the
        # two it is, because "denied" must never be reported as "missing".
        from agenthub_kernel import config as kernel_config

        write_ok, write_detail = kernel_config.codex_home_write_check()
        print(f"  CODEX_HOME: {home}（仓库之外；凭证存在，内容不读取）")
        print(f"  写权限检查 : {'OK' if write_ok else 'DENIED'} — {write_detail}")
        if not write_ok:
            print("REFUSING: this process cannot write inside CODEX_HOME, and Codex needs to")
            print("(logs/state/tmp). The directory EXISTS - this is an ACCESS denial, not a")
            print("missing directory. Run this probe in a terminal that may write there, or")
            print("point --codex-home at a home this process can write.")
            return 2
        calls = spec.real_calls + (1 if args.with_write_control and args.probe == "readonly-write" else 0)
        print(f"  本次将发起 {calls} 次真实调用")
    else:
        print(f"  stub 模式：不调用 Codex、不联网；CODEX_HOME={home}")
        if args.stub_outcome:
            print(f"  ⚠ 强制桩结果 = {args.stub_outcome}（负/正对照用，不改变判定规则）")

    print("-" * 78)
    print(f"RUNNING probe {args.probe} (mode={args.mode})")
    print("-" * 78)
    payload = run_cancel_probe(args.mode, home) if args.probe == "cancel" else \
        run_simple_probe(args.probe, args.mode, home, outcome=args.stub_outcome or "")
    verdict = judge(args.probe, payload, scripted=args.mode == "stub")

    control_payload = None
    if args.probe == "readonly-write" and args.with_write_control:
        print()
        print("-" * 78)
        print("正对照：同一提示词 + --sandbox workspace-write")
        print("（这一步会让沙箱可写；工作目录是本次专用的空目录，只用于对照）")
        print("-" * 78)
        control_payload = run_simple_probe(
            args.probe, args.mode, home, outcome=CONTROL_OUTCOME, sandbox=CONTROL_SANDBOX
        )
        verdict = probes.judge_readonly_write_control(
            judge("readonly-write", payload, False).observations,
            judge("readonly-write", control_payload, False).observations,
            target_name="PROBE_WRITE.txt",
        )

    print()
    print("=" * 78)
    print(f"VERDICT: {verdict.verdict}    capability={verdict.capability}")
    print("=" * 78)
    for reason in verdict.reasons:
        print(f"  - {reason}")
    print()
    print("observations:")
    for key, value in verdict.observations.items():
        print(f"  {key} = {json.dumps(value, ensure_ascii=False, default=str)}")
    print()
    print("cleanup:")
    print(f"  - 可删（本探测自己创建的运行目录）: {RUN_ROOT}\\{args.probe}-{args.mode}*")
    print(f"  - 保留 CODEX_HOME（本次使用）: {home}")
    if args.mode == "real":
        print("      真实模式：这是你的长期凭据目录（或你自己的 ~/.codex）。")
        print("      它含有登录凭证与会话历史，**删除它会破坏你的登录** —— 不要删。")
    else:
        print("      stub 模式：这是桩进程的临时 home，与凭据无关，可以删。")
    print("  - 用户自己的 %USERPROFILE%\\.codex 一律保留；本工具从不写入或删除它。")
    if args.mode == "real" and args.probe == "cancel":
        print("  - 若出现 unknown 调用：由你在裁决入口处理（不要自动重试）")

    out = os.path.join(RUN_ROOT, f"verdict-{args.probe}-{args.mode}-{int(time.time())}.json")
    os.makedirs(RUN_ROOT, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"spec": spec.__dict__, "payload": payload, "verdict": verdict.to_dict()},
                  fh, ensure_ascii=False, indent=2, default=str)
    print(f"  - 完整证据（脱敏，未含凭证）: {out}")
    if verdict.verdict == probes.FAIL and not args.fail_on_fail:
        print("  （FAIL 表示能力缺口，探测本身已完成；加 --fail-on-fail 可让它以非零码退出）")
    return 1 if (verdict.verdict == probes.FAIL and args.fail_on_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
