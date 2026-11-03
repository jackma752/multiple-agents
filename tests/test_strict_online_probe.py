"""Offline checks for the opt-in online driver's authorization scope."""
import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / 'tools/probes/strict_online.py'
spec = importlib.util.spec_from_file_location('strict_online_probe', path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_one_shot_intent_survives_and_cannot_be_replaced(tmp_path):
    probe.once(tmp_path, 'files', {'authorized': True})
    with pytest.raises(FileExistsError):
        probe.once(tmp_path, 'files', {'authorized': False})
    assert 'true' in (tmp_path / 'files-started.json').read_text()


def test_fixture_approver_denies_unscoped_files_tools_and_callers(tmp_path):
    def event(name='Write', filename='allowed.txt', content='ONLINE_ACCEPTANCE_ALLOWED\n', **extra):
        return {'tool_name': name, 'tool_input': {'file_path': str(tmp_path / filename),
                'content': content}, **extra}
    assert probe.allow_file(event(), tmp_path)
    assert not probe.allow_file(event(filename='denied.txt'), tmp_path)
    assert not probe.allow_file(event(filename='../allowed.txt'), tmp_path)
    assert not probe.allow_file(event(content='unapproved data'), tmp_path)
    assert not probe.allow_file(event(name='Edit'), tmp_path)
    assert not probe.allow_file(event(agent_id='child'), tmp_path)


def test_child_file_approval_requires_child_origin_and_fixed_content(tmp_path):
    event = {'tool_name': 'Write', 'tool_input': {'file_path': str(tmp_path / 'child.txt'),
             'content': 'ONLINE_ACCEPTANCE_CHILD\n'}}
    assert not probe.allow_file(event, tmp_path, child=True)
    event['agent_id'] = 'approved-child'
    assert probe.allow_file(event, tmp_path, child=True)
    event['tool_input']['extra'] = 'unapproved option'
    assert not probe.allow_file(event, tmp_path, child=True)


def test_file_verdict_requires_approved_and_delivered_cards():
    case = {'status': 'succeeded', 'approval': 'consumed', 'responder_errors': [],
        'expected_file_exact': True, 'denied_file_absent': True, 'native': [],
        'cards': [{'tool': 'Read', 'status': 'approved', 'delivery': 'sent'},
                  {'tool': 'Write', 'status': 'approved', 'delivery': 'sent'},
                  {'tool': 'Write', 'status': 'declined', 'delivery': 'sent'}]}
    assert probe.claude_case_passed(case, child=False)
    case['cards'][1]['delivery'] = 'pending'
    assert not probe.claude_case_passed(case, child=False)


def test_child_verdict_requires_terminal_child_and_released_slot():
    case = {'status': 'succeeded', 'approval': 'consumed', 'responder_errors': [],
        'expected_file_exact': True, 'denied_file_absent': True,
        'native': [{'state': 'finished', 'identified': True, 'depth': 1}],
        'quota': {'launched': 1, 'concurrent': 0},
        'cards': [{'tool': 'Agent', 'status': 'approved', 'delivery': 'sent', 'child': False},
                  {'tool': 'Read', 'status': 'approved', 'delivery': 'sent', 'child': True},
                  {'tool': 'Write', 'status': 'approved', 'delivery': 'sent', 'child': True}]}
    assert probe.claude_case_passed(case, child=True)
    case['quota']['concurrent'] = 1
    assert not probe.claude_case_passed(case, child=True)
    case['quota']['concurrent'] = 0
    case['native'][0]['state'] = 'unknown'
    assert not probe.claude_case_passed(case, child=True)
