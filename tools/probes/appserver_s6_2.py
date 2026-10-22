"""S6-2: one minimal supervised call through the Hub. Offline by default.

The whole product path runs in this process: the FastAPI app (API + Worker +
kernel) built by ``create_app`` with the adapter from ``build_adapter()``, driven
over its HTTP interface. One conversation, one turn, one approval with the
supervised-boundary consent, one ``run``. Then the result is judged from what
the Hub stored: invocation status, event order, the reply, the adapter record
and the quota slot.

``--mode stub`` uses the bundled stdio stub (simulation admission). ``--mode
real`` needs ``--approve-one-model-turn`` and ``--acknowledge-supervised-
boundary``; before the database or any process is created it checks the pinned
executable (size and SHA-256), the CA bundle hash, CODEX_HOME (outside the
repository, login file present - never read - and writable) and that no
identity override is set, and that supervised admission is granted. The data directory (database, work
directories, adapter store) is created OUTSIDE the repository so the model's cwd
does not inherit repository instructions (as S2-S5). The driver never answers a
tool request itself, never retries and never sends a second turn.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for sub in ("packages/contracts", "packages/adapters", "apps/worker", "apps/api"):
    path = str(REPO / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

RUN_ROOT = REPO / "var" / "appserver-s6-2"
WORK_ROOT = Path(tempfile.gettempdir())
PROMPT = "Reply with exactly: OK. Do not use tools, create subagents, or modify any files."
EXPECTED_REPLY = "OK"
TIMEOUT_SECONDS = 300
#: certifi 2026.10.19 cacert.pem, recorded in docs/codex-proxy-setup.md §7.1.
CA_BUNDLE_SHA256 = "50f79f98dac192b20dbf73f35c56f0b0c557ab267ff5199d1117cc1180b70785"
IDENTITY_ENV = ("AGENT_HUB_CODEX_PATH", "AGENT_HUB_CODEX_SHA256", "AGENT_HUB_CODEX_SIZE",
                "AGENT_HUB_CODEX_VERSION")
#: Offline scenarios and the reply each one gives (None: it is expected to fail).
STUB_REPLIES = {"happy": "OK", "turn-failed": None, "tool-unfinished": "OK",
                "native-spawn": "OK", "settings-mismatch": None}


class Refused(Exception):
    """A precondition failed; nothing was created or launched."""


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
    """Set (or with None, remove) variables for the run; restore them afterwards."""
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


def preflight_real(adapter, *, codex_home: str) -> dict:
    """Passive checks before anything is created. Raises Refused on the first failure."""
    from agenthub.contracts import execution_admission
    from agenthub_adapters.appserver_adapter import CodexAppServerFactory, PINNED_APPSERVER

    checks = {}
    overrides = [name for name in IDENTITY_ENV if os.environ.get(name)]
    checks["no_identity_override"] = not overrides
    factory = adapter.transport_factory
    checks["factory_is_pinned_real"] = type(factory) is CodexAppServerFactory
    ok, _why = factory.identity_matches() if checks["factory_is_pinned_real"] else (False, "")
    checks["pinned_identity_matches"] = bool(ok) and factory.codex_path == PINNED_APPSERVER["path"]
    ca = getattr(factory, "ca_bundle", None)
    checks["ca_bundle_hash_matches"] = bool(ca and os.path.isfile(ca) and _sha256(ca) == CA_BUNDLE_SHA256)
    checks["codex_home_outside_repository"] = not _inside(codex_home, REPO)
    checks["codex_home_has_login"] = os.path.isfile(os.path.join(codex_home, "auth.json"))  # presence only
    from agenthub_kernel import config

    checks["codex_home_writable"] = config.codex_home_write_check(codex_home)[0]
    admission = execution_admission(adapter, "supervised")
    checks["supervised_admission"] = admission.allowed and admission.mode == "supervised"
    checks["strict_still_blocked"] = not execution_admission(adapter, "strict").allowed
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise Refused("preflight failed: " + ", ".join(failed))
    return checks


def _page_events(client, cid, headers) -> list:
    events, after = [], 0
    while True:
        page = client.get(f"/api/conversations/{cid}/events", params={"after_seq": after, "limit": 500},
                          headers=headers).json()
        events.extend(page["events"])
        if not page.get("has_more"):
            return events
        after = page["next_after_seq"]


def judge(evidence: dict, *, expected_reply: str) -> list:
    """Failure codes; empty means PASS. Structural facts only."""
    codes = []
    if evidence["status"] != "succeeded":
        codes.append("status_not_succeeded")
    if evidence["start_count"] != 1:
        codes.append("start_count_not_one")
    if evidence["held_slots"] != 0:
        codes.append("quota_slot_held")
    order = evidence["order"]
    if not (order.get("starting") is not None and order.get("turn_started") is not None
            and order.get("first_delta") is not None and order.get("turn_ended") is not None
            and order.get("succeeded") is not None
            and order["starting"] < order["turn_started"] < order["first_delta"]
            < order["turn_ended"] < order["succeeded"]):
        codes.append("event_order")
    if not evidence["reply_exact"]:
        codes.append("reply_not_exact")
    record = evidence["record"]
    if record.get("tool_process_state") != "none" or record.get("tool_approvals"):
        codes.append("tool_activity")
    if record.get("native_agents"):
        codes.append("native_agent_observed")
    if record.get("declined_requests") or record.get("unanswered_requests"):
        codes.append("server_request")
    if record.get("foreign_events") or record.get("late_events") or record.get("usage_anomalies"):
        codes.append("unbound_or_late_events")
    if record.get("replayed_usage") is not None or record.get("resumed"):
        codes.append("unexpected_resume")
    if not record.get("usage"):
        codes.append("no_usage")
    if not record.get("local_process_exit_confirmed"):
        codes.append("local_exit_unconfirmed")
    survivors = (record.get("descendants") or {}).get("after_grace")
    if survivors:
        codes.append("descendants_survived")
    if not evidence["binding_matches"]:
        codes.append("binding_mismatch")
    if evidence["workdir_empty"] is not True:
        codes.append("workdir_not_empty")
    return codes


def run_probe(*, real: bool = False, approved: bool = False, acknowledged: bool = False,
              scenario: str = "happy", codex_home: str = "", run_root: Path = RUN_ROOT,
              work_root: Path = WORK_ROOT) -> int:
    if real and not (approved and acknowledged):
        print("REFUSED: S6-2 needs separate one-turn approval and acknowledgement of the supervised boundary.")
        return 2  # before any file, credential check or process
    if real and scenario != "happy":
        raise ValueError("real mode has no scenarios")
    if not real and scenario not in STUB_REPLIES:
        raise ValueError(f"unknown stub scenario {scenario!r}")
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="real-" if real else f"stub-{scenario}-", dir=run_root))
    data_dir = Path(tempfile.mkdtemp(prefix="agenthub-s6-2-data-", dir=work_root))
    result = {"mode": "real" if real else "stub", "scenario": scenario, "passed": False,
              "data_dir": str(data_dir), "reason_codes": [], "refused": None,
              "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest()}
    env = {
        "AGENT_HUB_ADAPTER": "codex", "AGENT_HUB_CODEX_TRANSPORT": "app-server",
        # Real mode must never fall back to a stub; stub mode names its scenario.
        "AGENT_HUB_APPSERVER_STUB": None if real else scenario,
        "AGENT_HUB_CODEX_TIMEOUT": str(TIMEOUT_SECONDS),
        # A per-run token for the in-process app only; never stored.
        "AGENT_HUB_USER_TOKEN": secrets.token_hex(16),
    }
    if real and codex_home:
        env["AGENT_HUB_CODEX_HOME"] = codex_home
    from agenthub_kernel import config

    saved_data_dir = config.DATA_DIR
    try:
        with _environment(env):
            config.DATA_DIR = str(data_dir)  # work dirs and the adapter store outside the repo
            from fastapi.testclient import TestClient

            from agenthub_kernel.adapters import adapter_binding, build_adapter
            from agenthub_kernel.db import Database, InvocationRow, QuotaReservationRow
            from agenthub_rt.api import create_app
            from sqlalchemy import func, select

            adapter = build_adapter("codex")
            result["binding_identity"] = adapter_binding(adapter)
            if real:
                result["preflight"] = preflight_real(adapter, codex_home=config.codex_home())
            headers = {"Authorization": f"Bearer {os.environ['AGENT_HUB_USER_TOKEN']}"}
            db = Database(f"sqlite:///{(data_dir / 'agent-hub.db').as_posix()}")
            app = create_app(db=db, adapter=adapter)
            with TestClient(app) as client:
                created = client.post("/api/conversations", headers=headers, json={"title": "S6-2"})
                if created.status_code != 200:
                    raise Refused(f"conversation refused: {created.status_code}")
                cid = created.json()["conversation_id"]
                turn = client.post(f"/api/conversations/{cid}/invocations", headers=headers, json={
                    "text": PROMPT, "agent_id": "codex", "client_request_id": "s6-2",
                    "execution_mode": "supervised"})
                if turn.status_code != 200:
                    raise Refused(f"submit refused: {turn.status_code}")
                turn = turn.json()
                consent = client.post(f"/api/approvals/{turn['approval_id']}/approve", headers=headers,
                                      json={"accept_unmanaged_native_agents": True})
                if consent.status_code != 200:
                    raise Refused(f"approval refused: {consent.status_code}")
                ran = client.post(f"/api/invocations/{turn['invocation_id']}/run",
                                  params={"wait": "true"}, headers=headers)
                result["run_http_status"] = ran.status_code
                events = _page_events(client, cid, headers)
                messages = client.get(f"/api/conversations/{cid}/messages", headers=headers).json()["messages"]
            inv_id = turn["invocation_id"]
            with db.session() as s:
                row = s.get(InvocationRow, inv_id)
                status, snapshot = row.status, dict(row.snapshot)
                held = s.scalar(select(func.count()).select_from(QuotaReservationRow)
                                .where(QuotaReservationRow.released_at.is_(None)))
            db.engine.dispose()
            record = (adapter.store.get(inv_id) or {}).get("payload") or {}
            order = {}
            for index, event in enumerate(events):
                kind, payload = event["type"], event.get("payload") or {}
                if kind == "invocation.status" and payload.get("to") in ("starting", "succeeded"):
                    order.setdefault(payload["to"], index)
                elif kind == "stream.turn_started":
                    order.setdefault("turn_started", index)
                elif kind == "stream.delta":
                    order.setdefault("first_delta", index)
                elif kind == "stream.turn_ended":
                    order.setdefault("turn_ended", index)
            replies = [m["text"] for m in messages if m["role"] == "assistant"]
            expected = EXPECTED_REPLY if real else STUB_REPLIES[scenario]
            workdir = record.get("workdir")
            evidence = {
                "status": status, "start_count": adapter.start_count, "held_slots": held,
                "order": order, "event_types": [e["type"] for e in events],
                "reply_exact": bool(expected) and len(replies) == 1 and replies[0].strip() == expected,
                "binding_matches": snapshot.get("adapter_version") == result["binding_identity"],
                "workdir_empty": (not any(Path(workdir).iterdir())) if workdir and Path(workdir).is_dir() else None,
                "record": {k: record.get(k) for k in (
                    "thread_id", "turn_id", "turn_status", "usage", "replayed_usage", "usage_anomalies",
                    "tool_process_state", "tool_activity", "tool_approvals", "declined_requests",
                    "unanswered_requests", "native_agents", "foreign_events", "late_events", "resumed",
                    "local_process_exit_confirmed", "descendants", "verdict", "terminal", "detail")},
            }
            result.update(invocation_id=inv_id, **evidence)
            result["reason_codes"] = judge(evidence, expected_reply=expected or "")
            result["passed"] = not result["reason_codes"]
    except Refused as exc:
        result["refused"] = str(exc)
        result["reason_codes"] = ["refused"]
    finally:
        config.DATA_DIR = saved_data_dir
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("S6-2 PASSED" if result["passed"] else f"S6-2 FAILED {result['reason_codes']}")
    print(f"Evidence: {run_dir / 'result.json'}")
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    parser.add_argument("--scenario", choices=sorted(STUB_REPLIES), default="happy")
    parser.add_argument("--approve-one-model-turn", action="store_true")
    parser.add_argument("--acknowledge-supervised-boundary", action="store_true")
    parser.add_argument("--codex-home", default="")
    args = parser.parse_args()
    return run_probe(real=args.mode == "real", approved=args.approve_one_model_turn,
                     acknowledged=args.acknowledge_supervised_boundary, scenario=args.scenario,
                     codex_home=os.path.abspath(args.codex_home) if args.codex_home else "")


if __name__ == "__main__":
    raise SystemExit(main())
