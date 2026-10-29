"""Offline harness for P6: simulated model replies, fixture arXiv, injectable clock.

No provider, network or real Agent is contacted. ``claude-code`` runs in-process
(FakeClaudeRunner) and ``deepseek`` uses an offline transport; both answer
research templates from ``tools/demo/paper_stub_model.py``.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agenthub_adapters.claude_code import (
    ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher, result_object, scripted,
)
from agenthub_adapters.deepseek import DeepSeekAdapter
from agenthub_adapters.routing import RoutingAdapter
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import Database, InvocationRow
from agenthub_kernel.papers.net import FakeFetcher
from agenthub_rt.api import create_app
from tools.demo.paper_fixtures import FixtureWeb
from tools.demo.paper_stub_model import DeepSeekPaperTransport, respond

USER = {"Authorization": "Bearer dev-user-token"}
LAUNCHED = ("starting", "running", "succeeded", "failed", "unknown", "canceled", "timed_out")


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += dt.timedelta(**kw)


class Hub:
    def __init__(self, tmp_path, *, web=None, runner_script=None) -> None:
        self.tmp = tmp_path
        self.web = web or FixtureWeb()
        self.fetcher = FakeFetcher(self.web)
        self.cc_runner = FakeClaudeRunner(runner_script or (lambda argv, env, prompt: scripted(
            result_object(reply=respond(prompt)))(argv, env, prompt)))
        self.ds_transport = DeepSeekPaperTransport()
        self.clock = Clock()
        self.client = None

    def open(self):
        router = RoutingAdapter({
            "claude-code": ClaudeCodeAdapter(
                launcher=StubClaudeLauncher("success-usage"), workdir_root=str(self.tmp / "cc-runs"),
                store=ExecutionStore(str(self.tmp / "cc.db"), table="claude_code_executions"), runner=self.cc_runner),
            "deepseek": DeepSeekAdapter("https://api.deepseek.com", "deepseek-flash",
                                        credentials_path=str(self.tmp / "no-credentials.json"),
                                        store=ExecutionStore(str(self.tmp / "ds.db"), table="deepseek_executions"),
                                        transport=self.ds_transport, stub="paper-sim"),
        })
        app = create_app(db=Database(f"sqlite:///{(self.tmp / 'hub.db').as_posix()}"), adapter=router,
                         paper_fetcher=self.fetcher, paper_root=str(self.tmp / "papers"))
        app.state.autopilot.clock = self.clock
        self.client = TestClient(app)
        self.client.__enter__()
        return self

    def close(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client.app.state.db.engine.dispose()
            self.client = None

    def restart(self):
        """A new process on the same data: database, adapter stores and paper files."""
        self.close()
        return self.open()

    # -- helpers ---------------------------------------------------------------

    def api(self, method, path, **kw):
        return self.client.request(method, path, headers=USER, **kw)

    def prove(self, agents=("claude-code", "deepseek"), mode="strict"):
        cid = self.api("POST", "/api/conversations", json={}).json()["conversation_id"]
        for n, agent in enumerate(agents):
            turn = self.api("POST", f"/api/conversations/{cid}/invocations", json={
                "text": "hello", "agent_id": agent, "client_request_id": f"prove-{agent}-{n}",
                "execution_mode": mode}).json()
            assert self.api("POST", f"/api/approvals/{turn['approval_id']}/approve").status_code == 200
            r = self.api("POST", f"/api/invocations/{turn['invocation_id']}/run", params={"wait": "true"})
            assert r.json()["status"] == "succeeded", r.text

    def body(self, topic="retrieval augmented generation evaluation", *, policy="continue_allowed", target=5,
             review="deepseek", key="rt-1", sections=3, **extra):
        body = {"client_request_id": key,
                "request": {"topic": topic, "year_from": 2024, "year_to": 2026, "target_papers": target,
                            "language": "zh", "fulltext_policy": policy, "sections": sections},
                "roles": {"main_agent": "claude-code", "review_agent": review},
                "resources": {"local_roots": [str(self.tmp / "local")]} if (self.tmp / "local").is_dir() else {},
                "execution_mode": "strict", "max_calls": 40}
        body.update(extra)
        return body

    def create(self, **kw):
        r = self.api("POST", "/api/papers", json=self.body(**kw))
        assert r.status_code == 200, r.text
        return r.json()["task_id"]

    def view(self, task_id):
        r = self.api("GET", f"/api/papers/{task_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def run(self, task_id, passes=80, until=None):
        autopilot, worker = self.client.app.state.autopilot, self.client.app.state.worker
        view = self.view(task_id)
        for _ in range(passes):
            autopilot.tick()
            assert worker.wait_idle(15)
            view = self.view(task_id)
            if until is not None and until(view):
                return view
            if view["status"] not in ("running",):
                return view
        return view

    def launched(self):
        with self.client.app.state.db.session() as s:
            return s.scalar(select(func.count()).select_from(InvocationRow).where(
                InvocationRow.status.in_(LAUNCHED))) or 0

    def model_calls(self):
        return len(self.cc_runner.calls) + self.ds_transport.calls

    def choose(self, task_id, choice):
        req = self.view(task_id)["supplement"]
        return self.api("POST", f"/api/papers/{task_id}/supplement/choice",
                        json={"request_id": req["request_id"], "choice": choice})

    def upload(self, task_id, files, targets=None):
        req = self.view(task_id)["supplement"]
        data = {"request_id": req["request_id"]}
        if targets:
            data["targets"] = targets
        return self.client.post(f"/api/papers/{task_id}/supplement/files", headers=USER, data=data,
                                files=[("files", (name, content, "application/pdf")) for name, content in files])

    def submit(self, task_id, batch_id=None):
        req = self.view(task_id)["supplement"]
        return self.api("POST", f"/api/papers/{task_id}/supplement/submit",
                        json={"request_id": req["request_id"], "batch_id": batch_id or req["open_batch_id"]})

    def paper_id(self, task_id, source_id):
        return next(p["paper_id"] for p in self.view(task_id)["papers"] if p["source_id"] == source_id)


@pytest.fixture
def hub(tmp_path):
    h = Hub(tmp_path).open()
    yield h
    h.close()


def waiting(view):
    return view["status"] == "waiting_input"
