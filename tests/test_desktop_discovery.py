"""Read-only discovery survives stale desktop-cache paths; no real CLI calls."""
from pathlib import Path

import pytest

from agenthub_kernel import desktop


def test_claude_desktop_updates_are_discovered_without_adopting(tmp_path, monkeypatch):
    local, roaming = tmp_path / 'local', tmp_path / 'roaming'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('APPDATA', str(roaming))
    monkeypatch.setenv('AGENT_HUB_DESKTOP_HOME', str(tmp_path / 'desktop'))
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: None)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'user'))
    def never_run(*args, **kwargs):
        pytest.fail('scan must not run or adopt a program')
    monkeypatch.setattr(desktop.subprocess, 'run', never_run)
    stale = local / 'Packages/Claude_example/LocalCache/Roaming/Claude/claude-code/2.1.308/old/claude.exe'
    desktop.atomic_json(desktop.home() / 'profile.json', {'schema':1, 'agents':{
        'claude-code':{'enabled':True,'path':str(stale),'sha256':'prior-approval'}}})
    profile_before = (desktop.home() / 'profile.json').read_bytes()
    installed = [stale.parent.parent.parent / '2.1.317/new/claude.exe',
                 roaming / 'Claude/claude-code/2.1.309/cache/claude.exe']
    for program in installed:
        program.parent.mkdir(parents=True)
        program.write_bytes(b'synthetic executable')
    candidates = desktop.discover()
    assert {item['path'] for item in candidates} == {str(p.resolve()) for p in installed}
    assert all(item['agent'] == 'claude-code' for item in candidates)
    assert (desktop.home() / 'profile.json').read_bytes() == profile_before
    assert not (desktop.home() / 'adoptions.jsonl').exists()


def test_one_disappearing_candidate_does_not_hide_other_versions(tmp_path, monkeypatch):
    import glob
    monkeypatch.setattr(desktop, 'load', lambda: {'agents':{}})
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: None)
    old, current = tmp_path / 'old.exe', tmp_path / 'current.exe'
    old.write_bytes(b'old')
    current.write_bytes(b'current')
    monkeypatch.setattr(glob, 'iglob', lambda pattern: iter([str(old),str(current)]) if 'claude' in pattern.lower() else iter([]))
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == old:
            raise PermissionError('synthetic unreadable candidate')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    assert [item['path'] for item in desktop.discover()] == [str(current.resolve())]
