"""Model selection, accounting and admission through offline Hub endpoints."""
import threading
from dataclasses import replace

import pytest
from sqlalchemy import select
from agenthub.contracts import InvocationSnapshot
from agenthub.models import budget_key
from agenthub.usage import MAPPING_CODEX_APPSERVER
from agenthub_adapters.appserver_stub import InMemoryStubFactory
from agenthub_adapters.claude_code import (ClaudeCodeAdapter, FakeClaudeRunner, StubClaudeLauncher,
                                         result_object, scripted)
from agenthub_adapters.store import ExecutionStore
from agenthub_kernel.db import ApprovalRow, InvocationRow, ModelBudgetReservationRow, TokenBudgetItemRow
from tests.http.test_conversation_stream import make_client, submit, turn_and_run, held_slots  # noqa: F401
from tests.http.test_conversations import USER, AGENT, approve, conversation, run
from tests.http.test_budget_admission import root_of


def policy(client, cid, models, **extra):
    response = client.patch(f'/api/tasks/{root_of(client, cid)}/policy', headers=USER,
                            json={'model_budgets': models, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def budgeted(client, model='stub-model', **extra):
    cid = conversation(client)
    submit(client, cid, key='opener')
    policy(client, cid, {budget_key('codex', 'codex', model):
                         {'total': 50000, 'per_call': 40000}}, **extra)
    return cid


def usage(client, cid):
    return client.get(f'/api/conversations/{cid}/usage', headers=USER).json()


def test_model_and_effort_reach_wire_and_are_bound_to_approval(make_client):
    factory = InMemoryStubFactory('usage-multi-request')
    client = make_client(factory)
    cid = conversation(client)
    turn, result = turn_and_run(client, cid, 'one', model='stub-model-2', reasoning_effort='high')
    assert result['status'] == 'succeeded'
    requests = factory.servers[0].received
    thread = next(m['params'] for m in requests if m.get('method') == 'thread/start')
    actual = next(m['params'] for m in requests if m.get('method') == 'turn/start')
    assert thread['model'] == actual['model'] == 'stub-model-2'
    assert actual['effort'] == 'high'
    with client.app.state.db.session() as session:
        inv = session.get(InvocationRow, turn['invocation_id'])
        snapshot = InvocationSnapshot(**inv.snapshot)
        assert snapshot.payload_hash() != replace(snapshot, model='stub-model').payload_hash()
    rows = usage(client, cid)['by_model']
    assert rows[0]['model'] == 'stub-model-2' and rows[0]['model_source'] == ['thread_configuration']


def test_metadata_get_is_passive_refresh_has_no_model_turn(make_client):
    factory = InMemoryStubFactory('stream')
    client = make_client(factory)
    assert client.get('/api/agents/codex/models').status_code == 401
    before = client.get('/api/agents/codex/models', headers=USER).json()
    assert before['default_model'] is None and not factory.servers
    assert client.post('/api/agents/codex/models/refresh', headers=AGENT).status_code == 403
    response = client.post('/api/agents/codex/models/refresh', headers=USER)
    assert response.status_code == 200, response.text
    assert response.json()['default_model'] == 'stub-model'
    assert {m['id'] for m in response.json()['models']} == {'stub-model', 'stub-model-2'}
    methods = [m.get('method') for m in factory.servers[0].received]
    assert 'model/list' in methods and 'thread/start' not in methods and 'turn/start' not in methods
    client.get('/api/agents/codex/models', headers=USER)
    assert len(factory.servers) == 1


def test_budget_blocks_only_its_model_and_agent_total_is_not_double_counted(make_client):
    factory = InMemoryStubFactory('usage-multi-request')
    client = make_client(factory)
    cid = budgeted(client)
    turn_and_run(client, cid, 'one', model='stub-model')
    refused = submit(client, cid, key='blocked', model='stub-model')
    approve(client, refused)
    response = run(client, refused)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'model_token_budget_exceeded'
    assert len(factory.servers) == 1 and held_slots(client) == 0
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, refused['approval_id']).status == 'approved'
        assert session.get(ModelBudgetReservationRow, refused['invocation_id']) is None
    turn_and_run(client, cid, 'other', model='stub-model-2')
    view = usage(client, cid)
    assert len(view['by_model']) == 2
    assert view['by_agent']['codex']['known']['input_total'] == 30539 * 2
    assert view['total']['known']['input_total'] == sum(r['total']['known']['input_total'] for r in view['by_model'])
    assert view['budget']['model_budgets'][0]['remaining'] == 50000 - 30618


def test_model_refusal_rolls_back_root_reservation_too(make_client):
    client = make_client(InMemoryStubFactory('stream'))
    cid = budgeted(client, token_budget_total=100000, token_budget_per_call=40000)
    # Explicit model is required if this agent has a strict model budget.
    turn = submit(client, cid, key='no-model')
    approve(client, turn)
    response = run(client, turn)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'model_budget_model_required'
    with client.app.state.db.session() as session:
        assert session.get(TokenBudgetItemRow, turn['invocation_id']) is None
        assert session.get(ApprovalRow, turn['approval_id']).status == 'approved'
    assert held_slots(client) == 0


def test_model_reservation_is_preserved_as_unsettled_when_usage_unknown(make_client, monkeypatch):
    client = make_client(InMemoryStubFactory('stream'))
    adapter = client.app.state.worker.adapter
    monkeypatch.setattr(adapter, 'usage_report', lambda _: {'mapping': MAPPING_CODEX_APPSERVER,
        'source': 'none', 'usage': {}, 'model': 'stub-model'})
    cid = budgeted(client)
    turn_and_run(client, cid, 'one', model='stub-model')
    model = usage(client, cid)['budget']['model_budgets'][0]
    assert model['unsettled'] == 40000 and model['reserved'] == 0
    turn = submit(client, cid, key='two', model='stub-model')
    approve(client, turn)
    assert run(client, turn).status_code == 409


def test_two_model_claims_cannot_reserve_the_same_balance(make_client):
    factory = InMemoryStubFactory('usage-multi-request')
    client = make_client(factory)
    cid = budgeted(client)
    turns = [submit(client, cid, key=f'call-{i}', model='stub-model') for i in range(2)]
    for turn in turns:
        approve(client, turn)
    barrier, results = threading.Barrier(2), []
    def claim(turn):
        barrier.wait()
        results.append(client.app.state.worker.submit(turn['invocation_id']))
    threads = [threading.Thread(target=claim, args=(t,)) for t in turns]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert len(results) == 2 and sum(bool(r.get('accepted')) for r in results) == 1
    assert client.app.state.worker.wait_idle(10)
    assert len(factory.servers) == 1


def test_claude_multimodel_components_and_selection_argv(make_client, tmp_path):
    obj = result_object(usage={'input_tokens': 2, 'cache_creation_input_tokens': 3,
                               'cache_read_input_tokens': 5, 'output_tokens': 4})
    obj['modelUsage'] = {
        'claude-a': {'inputTokens': 1, 'cacheCreationInputTokens': 1, 'cacheReadInputTokens': 2, 'outputTokens': 1},
        'claude-b': {'inputTokens': 1, 'cacheCreationInputTokens': 2, 'cacheReadInputTokens': 3, 'outputTokens': 3}}
    runner = FakeClaudeRunner(scripted(obj))
    adapter = ClaudeCodeAdapter(launcher=StubClaudeLauncher('success-usage'), runner=runner,
        workdir_root=str(tmp_path/'cc-runs'), store=ExecutionStore(str(tmp_path/'cc.db')))
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = client.post(f'/api/conversations/{cid}/invocations', headers=USER, json={
        'text':'task', 'agent_id':'claude-code', 'client_request_id':'one', 'model':'claude-a',
        'reasoning_effort':'high'}).json()
    approve(client, turn)
    assert run(client, turn).status_code == 200
    argv = runner.calls[0]['argv']
    assert argv[argv.index('--model')+1] == 'claude-a' and argv[argv.index('--effort')+1] == 'high'
    page = usage(client, cid)
    assert [(r['model'], r['total']['known']['input_total']) for r in page['by_model']] == [('claude-a',4),('claude-b',6)]
    assert page['by_agent']['claude-code']['calls'] == 1
    assert page['total']['known']['input_total'] == 10 and page['total']['known']['output_total'] == 4


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '100'])
def test_invalid_model_budget_writes_nothing(make_client, value):
    client = make_client()
    cid = conversation(client)
    turn = submit(client, cid)
    response = client.patch(f'/api/tasks/{root_of(client,cid)}/policy', headers=USER, json={
        'model_budgets': {budget_key('codex','codex','stub-model'): {'total':value}}})
    assert response.status_code == 422
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, turn['approval_id']).status == 'pending'


def test_stage_selection_survives_scheduler_and_is_bound_to_rules(make_hub):
    from tests.http.test_autopilot import conv, prove, start, run_stage, stage_of
    client, factory = make_hub()
    cid = conv(client)
    prove(client, cid)
    response = start(client, cid, agents=['codex'], main_agent='codex', models={
        'codex': {'model':'stub-model-2', 'reasoning_effort':'high'}})
    assert response.status_code == 200, response.text
    assert run_stage(client, cid) == 'completed'
    result = stage_of(client, cid)
    with client.app.state.db.session() as session:
        inv = session.get(InvocationRow, result['steps'][0]['invocation_id'])
        assert inv.snapshot['model'] == 'stub-model-2' and inv.snapshot['reasoning_effort'] == 'high'


from tests.http.test_autopilot import make_hub  # noqa: E402,F401


def test_later_usage_reconciles_model_reservation_without_rebooking(make_client, monkeypatch):
    from agenthub_kernel.usage_ledger import record_usage
    client = make_client(InMemoryStubFactory('stream'))
    adapter = client.app.state.worker.adapter
    monkeypatch.setattr(adapter, 'usage_report', lambda _: {'mapping': MAPPING_CODEX_APPSERVER,
        'source': 'none', 'usage': {}, 'model': 'stub-model'})
    cid = budgeted(client)
    turn, _ = turn_and_run(client, cid, 'one', model='stub-model')
    monkeypatch.setattr(adapter, 'usage_report', lambda _: {'mapping': MAPPING_CODEX_APPSERVER,
        'source':'thread_total', 'model':'stub-model', 'usage':{'inputTokens':100, 'cachedInputTokens':0,
            'cacheWriteInputTokens':0, 'outputTokens':5, 'reasoningOutputTokens':0, 'totalTokens':105}})
    for _ in range(2):
        with client.app.state.db.session() as session:
            record_usage(session, adapter, turn['invocation_id'])
    state = usage(client, cid)['budget']['model_budgets'][0]
    assert (state['known'], state['unsettled'], state['reserved'], state['remaining']) == (105,0,0,49895)


def test_model_counter_stale_read_still_refuses_at_conditional_update(make_client, monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.dml import Update
    from agenthub_kernel.db import ModelBudgetCounterRow
    factory = InMemoryStubFactory('stream')
    client = make_client(factory)
    cid = budgeted(client)
    original = Session.execute
    def race(session, statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == 'model_budget_counters':
            original(session, ModelBudgetCounterRow.__table__.update().values(known=50000))
        return original(session, statement, *args, **kwargs)
    monkeypatch.setattr(Session, 'execute', race)
    turn = submit(client, cid, key='stale', model='stub-model')
    approve(client, turn)
    assert run(client, turn).status_code == 409
    assert not factory.servers and held_slots(client) == 0


@pytest.mark.parametrize('model', ['--help', 'has spaces', 'x\n--other'])
def test_invalid_model_cannot_be_used_as_cli_options(make_client, model):
    client = make_client()
    cid = conversation(client)
    response = client.post(f'/api/conversations/{cid}/invocations', headers=USER,
        json={'text':'task','agent_id':'codex','client_request_id':'invalid','model':model})
    assert response.status_code == 422


def test_unsupported_adapter_does_not_silently_ignore_model(make_client):
    from agenthub_adapters.mock import MockAdapter
    adapter = MockAdapter()
    client = make_client(adapter=adapter)
    cid = conversation(client)
    turn = client.post(f'/api/conversations/{cid}/invocations', headers=USER, json={
        'text':'task','agent_id':'mock','client_request_id':'unsupported','model':'some-model'}).json()
    approve(client, turn)
    response = run(client, turn)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'model_selection_unsupported'
    with client.app.state.db.session() as session:
        assert session.get(ApprovalRow, turn['approval_id']).status == 'approved'
    assert held_slots(client) == 0


def test_claude_alias_cannot_bypass_full_model_budget(make_client, tmp_path):
    runner = FakeClaudeRunner(scripted(result_object()))
    adapter = ClaudeCodeAdapter(launcher=StubClaudeLauncher('success-usage'), runner=runner,
        workdir_root=str(tmp_path/'cc'), store=ExecutionStore(str(tmp_path/'cc.db')))
    client = make_client(adapter=adapter)
    cid = conversation(client)
    def create(key):
        return client.post(f'/api/conversations/{cid}/invocations', headers=USER, json={
            'text':'task','agent_id':'claude-code','client_request_id':key,'model':'opus'}).json()
    create('opener')
    policy(client, cid, {budget_key('claude-code','claude-code','claude-model'):
                        {'total':1000,'per_call':100}})
    turn = create('alias')
    approve(client, turn)
    response = run(client, turn)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'model_budget_needs_resolved_model'
    assert not runner.calls
