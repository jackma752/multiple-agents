"""Paper UI/API must carry the reservation required by strict token admission."""
from sqlalchemy import select
from agenthub_kernel.db import TaskRow
from agenthub_kernel.service import PolicyService
import pytest


def test_total_without_reservation_is_refused_before_task_or_model_creation(hub):
    hub.prove();before=hub.model_calls()
    body=hub.body(token_budget_total=50000)
    check=hub.api('POST','/api/papers/preflight',json=body).json()
    assert not check['ok'] and any(b['code']=='budget_needs_per_call' for b in check['blockers'])
    assert hub.api('POST','/api/papers',json=body).status_code==422
    assert hub.model_calls()==before


def test_invalid_reservation_does_not_silently_widen_total(hub):
    hub.prove();before=hub.model_calls()
    body=hub.body(token_budget_total=100,token_budget_per_call=101)
    assert not hub.api('POST','/api/papers/preflight',json=body).json()['ok']
    assert hub.api('POST','/api/papers',json=body).status_code==422
    body['token_budget_per_call']=True
    assert hub.api('POST','/api/papers',json=body).status_code==422
    assert hub.model_calls()==before


def test_budgeted_paper_can_complete_and_preserves_policy(hub):
    hub.prove()
    body=hub.body(target=1,token_budget_total=50000,token_budget_per_call=10000)
    check=hub.api('POST','/api/papers/preflight',json=body).json()
    assert check['ok'] and check['token_budget_per_call']==10000
    task=hub.api('POST','/api/papers',json=body).json()['task_id']
    view=hub.run(task)
    assert view['status']=='completed',view
    with hub.client.app.state.db.session() as s:
        policy=s.get(TaskRow,view['root_task_id']).policy
        assert policy['token_budget_per_call']==10000 and policy['token_budget_total']==50000


def test_continuation_keeps_reservation_and_remaining_budget(hub):
    hub.prove();task=hub.create(target=1,token_budget_total=50000,token_budget_per_call=10000)
    old=hub.view(task)
    with hub.client.app.state.db.session() as s:
        PolicyService(s).request_stop(old['root_task_id'],requested_by='local-user')
    response=hub.api('POST',f'/api/papers/{task}/continue',json={})
    assert response.status_code==200,response.text
    with hub.client.app.state.db.session() as s:
        policy=s.get(TaskRow,response.json()['root_task_id']).policy
        assert policy['token_budget_per_call']==10000 and policy['token_budget_total']==50000


def test_continuation_refuses_remaining_budget_smaller_than_next_reservation(hub):
    hub.prove();task=hub.create(target=1,token_budget_total=10000,token_budget_per_call=10000)
    view=hub.run(task)
    assert view['status']=='blocked'
    before=hub.model_calls()
    with hub.client.app.state.db.session() as s:
        PolicyService(s).request_stop(view['root_task_id'],requested_by='local-user')
    response=hub.api('POST',f'/api/papers/{task}/continue',json={})
    assert response.status_code==409 and response.json()['detail']['code']=='allowance_used_up'
    assert hub.model_calls()==before


def test_screen_repair_names_the_missing_id_without_accepting_incomplete_results():
    from agenthub_kernel.papers.validate import screen, FormatError
    with pytest.raises(FormatError,match='pap-missing'):
        screen({'decisions':[{'paper_id':'pap-given','decision':'include','reason':'relevant'}]},
               ['pap-given','pap-missing'])
