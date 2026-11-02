"""Do not invite the user to prove a mode that the selected runtime cannot admit."""
from agenthub_adapters.codex import CodexAdapter
from agenthub_adapters.routing import RoutingAdapter


def test_unsupported_reviewer_is_rejected_before_research_creation(hub,tmp_path):
    hub.prove();before=hub.model_calls()
    blocked=CodexAdapter(codex_path=__file__,codex_home=str(tmp_path/'never-open-home'),workdir_root=str(tmp_path/'runs'))
    agents=hub.client.app.state.worker.adapter.agents();agents['codex']=blocked
    router=RoutingAdapter(agents)
    hub.client.app.state.worker.adapter=router;hub.client.app.state.adapter=router
    body=hub.body(review='codex')
    pre=hub.api('POST','/api/papers/preflight',json=body).json()
    assert not pre['ok']
    assert pre['mode_admissions']['codex']['code']=='native_subagents_uncontrolled'
    assert any(b['code']=='execution_mode_unavailable' and b['agent']=='codex' for b in pre['blockers'])
    assert not any(b['code']=='stage_not_proven' and b['agent']=='codex' for b in pre['blockers'])
    response=hub.api('POST','/api/papers',json=body)
    assert response.status_code==409 and response.json()['detail']['code']=='execution_mode_unavailable'
    assert blocked.start_count==0 and hub.model_calls()==before


def test_fixture_is_marked_simulation_and_budgeted_strict_flow_still_runs(hub):
    hub.prove();body=hub.body(target=1,token_budget_total=50000,token_budget_per_call=10000)
    pre=hub.api('POST','/api/papers/preflight',json=body).json()
    assert pre['ok']
    assert pre['mode_admissions']['claude-code']['mode']=='simulation'
    assert pre['mode_admissions']['deepseek']['allowed']
    response=hub.api('POST','/api/papers',json=body)
    assert response.status_code==200
    assert hub.run(response.json()['task_id'])['status']=='completed'
