r"""Drive the P2 Mock demo end to end against the local API.

Mirrors AGENT_HUB_PLAN §10:
    user chat -> dispatch two Mock sub-agents -> not approved = no start
    -> approve = execute -> cap reached = refuse new -> stop button works

It talks to a RUNNING server over HTTP (start it first with tools/demo/serve.py),
and prints a readable transcript plus the Mock adapter's start count at each
step, so the safety valve is visible rather than asserted.

Usage (from the repo root, with the venv):

    .\.venv\Scripts\python.exe tools\demo\serve.py            # terminal 1
    .\.venv\Scripts\python.exe tools\demo\demo_flow.py        # terminal 2
    .\.venv\Scripts\python.exe tools\demo\demo_flow.py --base-url http://127.0.0.1:8080

Note: the client disables environment proxy trust, because HTTP_PROXY-style
interception in some sandboxes answers localhost requests with 502.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_BASE = "http://127.0.0.1:8080"


class Demo:
    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base,
            timeout=30,
            trust_env=False,  # see module docstring
            headers={"Authorization": f"Bearer {token}"},
        )
        self.step_no = 0

    # -- helpers ---------------------------------------------------------

    def step(self, title: str) -> None:
        self.step_no += 1
        print(f"\n[{self.step_no}] {title}")
        print("-" * 68)

    def starts(self) -> int:
        r = self.client.get("/api/dev/mock-activity")
        r.raise_for_status()
        return r.json()["start_count"]

    def show_starts(self, label: str) -> int:
        n = self.starts()
        print(f"    Mock adapter start_count = {n}   ({label})")
        return n

    def main_invocation(self, goal: str) -> dict:
        r = self.client.post(
            "/api/invocations",
            json={"task_id": "-", "agent_id": "mock", "task_input": goal},
        )
        r.raise_for_status()
        return r.json()

    def pending_approval(self, invocation_id: str) -> str:
        """Dev convenience: the delegation/invocation flow surfaces the id."""
        return invocation_id

    def approve(self, approval_id: str) -> dict:
        r = self.client.post(f"/api/approvals/{approval_id}/approve")
        r.raise_for_status()
        return r.json()

    def reject(self, approval_id: str) -> dict:
        r = self.client.post(f"/api/approvals/{approval_id}/reject")
        r.raise_for_status()
        return r.json()

    def drain(self, verbose: bool = False) -> list[dict]:
        r = self.client.post("/api/worker/drain")
        r.raise_for_status()
        outcomes = r.json()
        if verbose:
            started = [o for o in outcomes if o.get("ok")]
            refused = [o for o in outcomes if not o.get("ok")]
            for o in started:
                print(f"    started  {o['invocation_id']}")
            for o in refused:
                print(f"    REFUSED  {o['invocation_id']}  code={o.get('code')}")
        return outcomes

    def delegate(self, parent_task_id: str, goal: str) -> dict:
        r = self.client.post(
            f"/api/tasks/{parent_task_id}/delegations",
            json={"agent_id": "mock", "goal": goal, "parent_task_id": parent_task_id},
        )
        r.raise_for_status()
        return r.json()


def run(base_url: str, token: str) -> int:
    d = Demo(base_url, token)

    print("=" * 68)
    print("Agent Hub - Mock approval & quota demo")
    print(f"  server      : {d.base}")
    print("  real agents : NONE - the Mock adapter is the only adapter")
    print("=" * 68)

    # Check reachability early with a clear message.
    try:
        d.client.get("/api/agents").raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"\nCannot reach {d.base}: {exc}")
        print("Start the server first:  .\\.venv\\Scripts\\python.exe tools\\demo\\serve.py")
        return 2

    d.step("User chats")
    r = d.client.post("/api/conversations", json={})
    r.raise_for_status()
    conv = r.json()["conversation_id"]
    r = d.client.post(f"/api/conversations/{conv}/messages", json={"text": "build a login page"})
    r.raise_for_status()
    print(f"    conversation {conv} -> message acked: {r.json()['acked']}")

    d.step("Create the main agent invocation (a PENDING approval is raised with it)")
    main = d.main_invocation("build a login page")
    root = main["task_id"]
    print(f"    task_id       = {root}")
    print(f"    invocation_id = {main['invocation_id']}")
    print(f"    approval_id   = {main['approval_id']}  status={main['status']}")

    d.step("Drain the worker WITHOUT approving")
    d.drain(verbose=True)
    n = d.show_starts("expected 0 - nothing is approved")
    assert n == 0, "SAFETY VALVE BROKEN: something started without approval"
    print("    OK: unapproved work never reached the adapter")

    d.step("Approve the main call, then drain")
    print(f"    approve {main['approval_id']} -> {d.approve(main['approval_id'])['status']}")
    d.drain(verbose=True)
    d.show_starts("expected 1 - the approved main call ran")

    d.step("Propose two Mock sub-agents (they do NOT execute yet)")
    s1 = d.delegate(root, "write the HTML")
    s2 = d.delegate(root, "write the CSS")
    print(f"    sub 1: task={s1['task_id']} approval={s1['approval_id']} status={s1['status']}")
    print(f"    sub 2: task={s2['task_id']} approval={s2['approval_id']} status={s2['status']}")

    d.step("Drain again - sub-agents are still not approved")
    d.drain(verbose=True)
    d.show_starts("expected still 1 - pending approvals are not consent")

    d.step("Approve BOTH sub-agents, then drain")
    for s in (s1, s2):
        print(f"    approve {s['approval_id']} -> {d.approve(s['approval_id'])['status']}")
    d.drain(verbose=True)
    d.show_starts("expected 3 - main + both approved sub-agents")

    d.step("Try to add a THIRD sub-agent beyond the quota")
    s3 = d.delegate(root, "write the JS")
    d.approve(s3["approval_id"])
    d.drain(verbose=True)
    n = d.show_starts("expected still 3 - the tree is at its sub-agent cap")
    assert n == 3, "QUOTA BROKEN: the cap was exceeded"
    print("    OK: the approved call was refused (see the code above)")

    d.step("Reject a call: it must never start")
    s4 = d.delegate(root, "should be rejected")
    print(f"    reject {s4['approval_id']} -> {d.reject(s4['approval_id'])['status']}")
    d.drain(verbose=True)
    d.show_starts("expected still 3")

    d.step("Stop the whole tree (cancels running calls, keeps the cumulative count)")
    r = d.client.post(f"/api/tasks/{root}/stop")
    r.raise_for_status()
    body = r.json()
    print(f"    stopped={body['stopped']} cancelled={body['cancelled']} "
          f"already_stopped={body['already_stopped']}")

    d.step("Approve one more call AFTER the stop - it must not start")
    s5 = d.delegate(root, "after stop")
    d.approve(s5["approval_id"])
    d.drain(verbose=True)
    d.show_starts("expected still 3 - a stopped tree schedules nothing new")

    print("\n" + "=" * 68)
    print("Demo finished. Every safety valve held:")
    print("  unapproved -> 0 starts | approved -> 1 each | cap -> refused")
    print("  rejected -> 0 starts   | stopped  -> no new scheduling")
    print("No real Agent was contacted at any point.")
    print("=" * 68)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Hub Mock demo.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_HUB_BASE_URL", DEFAULT_BASE),
        help=f"base URL of the running API (default {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_HUB_USER_TOKEN", "dev-user-token"),
        help="user bearer token (default $AGENT_HUB_USER_TOKEN or dev-user-token)",
    )
    args = parser.parse_args()
    return run(args.base_url, args.token)


if __name__ == "__main__":
    sys.exit(main())
