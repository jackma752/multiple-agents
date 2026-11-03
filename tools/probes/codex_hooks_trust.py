"""Pinned real CLI, trusted fixed guard, loopback provider; zero online requests.

Own empty home only. No login files, global hook trust or bypass-trust flags.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
for sub in ('packages/contracts', 'packages/adapters', 'apps/worker', '.'):
    sys.path.insert(0, str(ROOT / sub))

from agenthub_adapters.appserver import AppServerClient, StdioTransport
from agenthub_adapters.appserver_adapter import PINNED_APPSERVER
from agenthub_adapters.codex import build_env
from tools.probes.codex_native_loopback import Provider


def hook_rows(result):
    return [hook for row in result.get('data', []) for hook in row.get('hooks', [])]


def verify_guard(rows, *, path, trusted):
    if len(rows) != 1:
        raise ValueError('unexpected_hooks')
    hook = rows[0]
    if hook.get('eventName') not in ('preToolUse', 'pre_tool_use', 'PreToolUse'):
        raise ValueError('unexpected_hook_event')
    digest = hook.get('currentHash')
    if not isinstance(digest, str) or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', digest):
        raise ValueError('invalid_hook_hash')
    if not hook.get('enabled') or hook.get('isManaged'):
        raise ValueError('unexpected_hook_policy')
    if trusted and hook.get('trustStatus') != 'trusted':
        raise ValueError('guard_not_trusted')
    expected = str(Path(sys.executable).as_posix()) + ' ' + path.as_posix()
    if hook.get('command') != expected or hook.get('async') is not False or hook.get('matcher') != '.*':
        raise ValueError('guard_command_changed')
    return hook


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--approve-local-runtime-test', action='store_true', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--fault', choices=['none', 'invalid-json', 'timeout'], default='none')
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if not output.is_relative_to(ROOT / 'var') or output.exists():
        parser.error('fresh output inside this worktree var required')
    output.parent.mkdir(parents=True, exist_ok=True)
    executable = Path(PINNED_APPSERVER['path'])
    if hashlib.sha256(executable.read_bytes()).hexdigest() != PINNED_APPSERVER['sha256']:
        raise SystemExit('unreviewed executable')
    scratch = Path('E:/plug/.codex-hooks-083') / uuid.uuid4().hex
    home, workspace = scratch / 'empty-home', scratch / 'workspace'
    home.mkdir(parents=True); workspace.mkdir()
    guard, marker = scratch / 'guard.py', scratch / 'guard-events.jsonl'
    extra = "print('not-json')" if args.fault == 'invalid-json' else 'import time; time.sleep(8)' if args.fault == 'timeout' else (
        "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny',"
        "'permissionDecisionReason':'User-authorized synthetic fixture denial'}}))")
    guard.write_text('import json,sys\nfrom pathlib import Path\n'
        'event=json.load(sys.stdin)\n'
        f"with Path({str(marker)!r}).open('a',encoding='utf-8') as f: f.write(json.dumps(event)+'\\n')\n"
        + extra + '\n', encoding='utf-8')
    guard_sha = hashlib.sha256(guard.read_bytes()).hexdigest()
    report = {'real_model_requests': 0, 'fault': args.fault, 'scratch': str(scratch),
        'binary_sha256': PINNED_APPSERVER['sha256'], 'guard_sha256': guard_sha, 'scenario_verified': False}
    base = {k: v for k, v in os.environ.items() if not k.upper().startswith(('OPENAI', 'CODEX', 'AGENT_HUB'))}
    env = build_env(codex_home=str(home), proxy='http://127.0.0.1:1', base_env=base)
    env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1'
    def connect():
        client = AppServerClient(StdioTransport([str(executable), 'app-server', '--stdio'], env=env,
            cwd=str(workspace), stderr_path=str(scratch / ('stderr-'+uuid.uuid4().hex+'.txt'))), request_timeout=10)
        client.start()
        client.request('initialize', {'clientInfo': {'name': 'hub-hook-fixture', 'version': '0.8.3'},
            'capabilities': {'experimentalApi': True}})
        client.notify('initialized')
        return client
    try:
        with Provider() as provider:
            command = str(Path(sys.executable).as_posix()) + ' ' + guard.as_posix()
            config = ('model="gpt-5.4"\nmodel_provider="loopback"\nweb_search="disabled"\n'
                '[model_providers.loopback]\nname="Synthetic localhost only"\nwire_api="responses"\nrequires_openai_auth=false\n'
                f'base_url="http://127.0.0.1:{provider.server.server_address[1]}/v1"\n'
                '[agents]\nenabled=true\n[features]\nhooks=true\nmulti_agent=true\n'
                '[[hooks.PreToolUse]]\nmatcher=".*"\n[[hooks.PreToolUse.hooks]]\ntype="command"\n'
                f'command={json.dumps(command)}\ncommand_windows={json.dumps(command)}\ntimeout=2\n')
            (home / 'config.toml').write_text(config, encoding='utf-8')
            client = connect()
            try:
                before = client.request('hooks/list', {'cwds': [str(workspace)]})
                report['hooks_before'] = before
                hook = verify_guard(hook_rows(before), path=guard, trusted=False)
            finally:
                client.close()
            # User authorized this reviewed fixed test script in this empty home.
            # The model cannot write trust, and the real user's profile is untouched.
            config += f"\n[hooks.state.{json.dumps(hook['key'])}]\ntrusted_hash={json.dumps(hook['currentHash'])}\n"
            (home / 'config.toml').write_text(config, encoding='utf-8')
            assert hashlib.sha256(guard.read_bytes()).hexdigest() == guard_sha
            client = connect()
            try:
                after = client.request('hooks/list', {'cwds': [str(workspace)]})
                report['hooks_after'] = after
                trusted = verify_guard(hook_rows(after), path=guard, trusted=True)
                assert trusted['currentHash'] == hook['currentHash']
                thread = client.request('thread/start', {'cwd': str(workspace), 'model': 'gpt-5.4',
                    'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'sandbox': 'read-only'})['thread']['id']
                client.request('turn/start', {'threadId': thread,
                    'input': [{'type': 'text', 'text': 'SYNTHETIC MAIN. Follow the synthetic task.'}],
                    'approvalPolicy': 'on-request', 'approvalsReviewer': 'user',
                    'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}})
                terminal = client.wait_for(lambda n: n.method == 'turn/completed' and n.params.get('threadId') == thread, timeout=30)
                notes = client.notifications()
                (scratch / 'notifications.json').write_text(json.dumps([{'method': n.method, 'params': n.params} for n in notes], indent=2), encoding='utf-8')
                spawned = {child for n in notes for child in (n.params.get('item', {}).get('receiverThreadIds') or [])
                    if n.method == 'item/completed' and n.params.get('item', {}).get('type') == 'collabAgentToolCall'
                    and n.params.get('item', {}).get('tool') == 'spawnAgent'}
                report.update(terminal_status=terminal.params['turn']['status'], native_spawned_children=len(spawned),
                    synthetic_provider_requests=provider.calls, guard_fired=marker.exists())
                report['scenario_verified'] = marker.exists() and not spawned
            finally:
                client.close()
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error_detail'] = str(exc)[:500]  # isolated synthetic metadata only
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['scenario_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
