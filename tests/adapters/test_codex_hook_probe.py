"""Guard review must match the exact fixed script before a synthetic turn."""
from pathlib import Path
import sys

import pytest

from tools.probes.codex_hooks_trust import verify_guard


def metadata(path):
    return {'eventName': 'preToolUse', 'currentHash': 'sha256:' + 'a'*64,
        'enabled': True, 'isManaged': False, 'trustStatus': 'trusted',
        'command': Path(sys.executable).as_posix() + ' ' + path.as_posix(),
        'async': False, 'matcher': '.*'}


def test_current_wire_names_and_hash_prefix_are_accepted(tmp_path):
    path = tmp_path / 'guard.py'; row = metadata(path)
    assert verify_guard([row], path=path, trusted=True) == row


@pytest.mark.parametrize('change', [
    {'trustStatus': 'untrusted'}, {'currentHash': 'not-a-hash'}, {'enabled': False},
    {'isManaged': True}, {'eventName': 'postToolUse'}, {'command': 'another script'},
    {'async': True}, {'matcher': 'only_one_tool'},
])
def test_unknown_or_changed_hook_cannot_qualify_the_guard(tmp_path, change):
    path = tmp_path / 'guard.py'
    with pytest.raises(ValueError):
        verify_guard([{**metadata(path), **change}], path=path, trusted=True)


def test_additional_hooks_are_not_silently_trusted(tmp_path):
    path = tmp_path / 'guard.py'; row = metadata(path)
    with pytest.raises(ValueError): verify_guard([row, row], path=path, trusted=True)
