"""Acceptance must reject empty exports and unavailable/unknown execution."""
import pytest
from tools.probes.project_online import acceptance


def valid():
    return dict(status='completed', phase='done', quality_status='passed', proof_passed=True,
        fulltext_documents=1, study_units=12, references=20, quotes_matched=20,
        candidate_count=2, candidate_novelty_unverified=True, all_launched_approved=True,
        all_stage_calls_granted=True, usage_complete=True, active_or_unknown=0,
        tokens=50000, model_calls=8, bundle_verified=True, project_saved=True,
        experiments_saved=True, restart_no_extra_calls=True, pause_no_new_calls=True)


def test_complete_bounded_project_passes():
    assert acceptance(valid())


@pytest.mark.parametrize('change', [
    dict(status='blocked'), dict(phase='read'), dict(quality_status='needs_review'),
    dict(fulltext_documents=0), dict(study_units=0), dict(references=0, quotes_matched=0),
    dict(quotes_matched=19), dict(candidate_count=0), dict(candidate_novelty_unverified=False),
    dict(all_launched_approved=False), dict(all_stage_calls_granted=False), dict(usage_complete=False),
    dict(active_or_unknown=1), dict(tokens=100001), dict(model_calls=17),
    dict(bundle_verified=False), dict(project_saved=False), dict(experiments_saved=False),
    dict(restart_no_extra_calls=False), dict(pause_no_new_calls=False), dict(proof_passed=False),
])
def test_partial_or_unapproved_run_is_not_a_pass(change):
    assert not acceptance(valid() | change)
