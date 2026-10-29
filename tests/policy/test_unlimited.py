from agenthub.contracts import InvocationStatus, Policy
from agenthub.stage import make_rules
from agenthub_kernel.service import PolicyService
from tests.helpers import KernelHarness


def test_unlimited_defaults_still_require_approval_and_bound_concurrency():
    policy=Policy(collaboration_enabled=True,max_subagents_concurrent=1)
    assert policy.max_agent_invocations==policy.max_subagents_total==policy.run_time_limit_seconds==0
    h=KernelHarness(policy=policy); root=h.create_root()
    # Cross the old 3-child and 10-call defaults, releasing each completed slot.
    for i in range(12):
        task=h.create_subtask(root.id,str(i));inv=h.create_invocation(task.id)
        h.drain();assert h.adapter.start_count==i
        approval=h.create_approval(inv.id);h.approve(approval.id);h.drain()
        assert h.adapter.start_count==i+1
        with h.open_session() as s: PolicyService(s,h.policy).release(inv.id,InvocationStatus.SUCCEEDED);s.commit()
    first=h.create_subtask(root.id,'running');inv=h.create_invocation(first.id)
    approval=h.create_approval(inv.id);h.approve(approval.id);h.drain()
    second=h.create_subtask(root.id,'blocked');other=h.create_invocation(second.id)
    approval=h.create_approval(other.id);h.approve(approval.id);h.drain()
    assert h.adapter.start_count==13  # unlimited total is still one concurrent slot


def test_stage_zero_is_unlimited_and_explicit_limit_changes_the_grant():
    args={'agents':['codex'],'main_agent':'codex'}
    unlimited=make_rules(args)
    bounded=make_rules({**args,'max_calls':2})
    assert unlimited.max_calls==0 and bounded.max_calls==2
    assert unlimited.rules_hash()!=bounded.rules_hash()
