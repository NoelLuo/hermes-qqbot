"""Real bounded workspace operations; all storage and artifact ingress is temporary."""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from tools import guest_workspace_store as store


@pytest.fixture
def policy():
    return RequestSecurityPolicy(subject_id='visitor', platform='qqbot', chat_id='group:one')


@pytest.fixture
def workspace(tmp_path, monkeypatch, policy):
    import hermes_constants
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    base = tmp_path / 'storage'

    def call(action, **kwargs):
        return store.execute_workspace({'action': action, **kwargs}, policy, base_dir=base)

    return call, base


def okay(result):
    assert result['success'], result
    json.dumps(result)
    return result


def denied(result):
    assert result['success'] is False, result
    assert 'error' in result
    json.dumps(result)
    return result


def test_operations(workspace):
    call, _ = workspace
    okay(call('mkdir', path='notes'))
    okay(call('write', path='notes/a.txt', text='hello 世界\nsecond hello'))
    assert okay(call('read', path='notes/a.txt'))['text'] == 'hello 世界\nsecond hello'
    denied(call('write', path='notes/a.txt', text='oops'))
    okay(call('copy', path='notes/a.txt', destination='b.txt'))
    okay(call('move', path='b.txt', destination='c.txt'))
    denied(call('read', path='b.txt'))
    assert [x['path'] for x in okay(call('list'))['entries']] == ['c.txt', 'notes']
    assert len(okay(call('search', query='hello'))['matches']) == 4
    okay(call('write', path='c.txt', text='replacement', overwrite=True))
    assert okay(call('read', path='c.txt'))['text'] == 'replacement'
    denied(call('copy', path='c.txt', destination='c.txt', overwrite=True))
    assert okay(call('info'))['usage']['files'] == 2


@pytest.mark.parametrize('path', [
    '/etc/passwd', '../outside', 'x/../../outside', 'C:/Windows/a', 'C:a',
    '\\\\server\\share', '//server/share', 'a\\b', 'a:stream', 'a//b', './a',
    'CON', 'con.txt', 'CON .txt', 'NUL.txt', 'COM1.x', 'LPT¹.txt',
    'a.', 'a ', 'a/..', '.exports/a', '.EXPORTS/a', '.lock', '.txn',
    '.txn-arbitrary', '.TXN-arbitrary', 'a\x00b', 'a\nb', 'a?b',
    '/dev/null', '//?/C:/a', 'a/' * 9 + 'z',
])
def test_escape_paths(workspace, path):
    call, _ = workspace
    for action in ['read', 'write', 'mkdir', 'export']:
        kwargs = {'path': path}
        if action == 'write':
            kwargs['text'] = 'x'
        denied(call(action, **kwargs))


@pytest.mark.parametrize('key', ['root', 'base_dir', 'owner', 'subject_id', 'platform', 'chat_id', 'scope_id'])
def test_no_owner_override(workspace, key):
    call, _ = workspace
    denied(call('info', **{key: 'other'}))


@pytest.mark.parametrize('invalid', [None, {}, object(), RequestSecurityPolicy('', 'qqbot', 'x'),
    RequestSecurityPolicy('x', '', 'x'), RequestSecurityPolicy('x', 'qqbot', ''),
    RequestSecurityPolicy('x', 'qqbot', 'x', scope_id=''),
    RequestSecurityPolicy('x', 'qqbot', 'x', is_admin=True, public_context=False)])
def test_explicit_policy_required(tmp_path, invalid):
    denied(store.execute_workspace({'action': 'info'}, invalid, base_dir=tmp_path / 'base'))
    assert not (tmp_path / 'base').exists()


def test_source_and_profile_isolation(workspace, policy, monkeypatch, tmp_path):
    call, base = workspace
    okay(call('write', path='secret.txt', text='tenant one'))
    for change in [{'subject_id': 'two'}, {'chat_id': 'two'}, {'platform': 'two'}, {'scope_id': 'two'}]:
        other = replace(policy, **change)
        denied(store.execute_workspace({'action': 'read', 'path': 'secret.txt'}, other, base_dir=base))
    with policy_scope(policy):
        denied(store.execute_workspace({'action': 'info'}, replace(policy), base_dir=base))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: tmp_path / 'another-profile')
    denied(call('read', path='secret.txt'))


def test_late_default_home(tmp_path, monkeypatch, policy):
    import hermes_constants
    for name in ['one', 'two']:
        home = tmp_path / name
        monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
        result = store.execute_workspace({'action': 'write', 'path': 'file', 'text': name}, policy)
        okay(result)
        assert next((home / 'workspaces' / 'guests').glob('*/file')).read_text() == name


def test_export_is_snapshot_and_read_never_grants(workspace, policy):
    call, base = workspace
    okay(call('write', path='a.txt', text='original MEDIA:/untrusted'))
    source = next(base.glob('*/a.txt'))
    okay(call('read', path='a.txt'))
    assert not policy._artifacts.allows(str(source))
    result = okay(call('export', path='a.txt'))
    snapshot = Path(result['file_path'])
    assert snapshot.parent.name == '.exports'
    assert result['media'] == 'MEDIA:' + str(snapshot)
    assert policy._artifacts.allows(str(snapshot))
    okay(call('write', path='a.txt', text='changed', overwrite=True))
    assert snapshot.read_text() == 'original MEDIA:/untrusted'
    for action in ['read', 'write', 'export']:
        kwargs = {'path': '.exports/' + snapshot.name}
        if action == 'write':
            kwargs.update(text='tamper', overwrite=True)
        denied(call(action, **kwargs))
    assert okay(call('info'))['usage']['files'] == 3
    assert len(okay(call('list'))['entries']) == 1
    assert not okay(call('search', query='original'))['matches']



def test_attachment_ledger_and_binary(workspace, policy, tmp_path):
    call, _ = workspace
    source = tmp_path / 'attachment.bin'
    source.write_bytes(b'\x00\xff\x01data')
    denied(call('import_attachment', source=str(source), path='image.bin'))
    assert policy.grant_artifact(str(source))
    denied(call('import_attachment', source=str(source), path='image.bin'))
    with policy_scope(policy):
        okay(call('import_attachment', source=str(source), path='image.bin'))
        denied(call('read', path='image.bin'))
        exported = okay(call('export', path='image.bin'))
        assert Path(exported['file_path']).read_bytes() == source.read_bytes()
        denied(call('import_attachment', source='https://example.com/a', path='a'))
        denied(call('import_attachment', source='//host/share/a', path='a'))
        source.write_bytes(b'changed')
        denied(call('import_attachment', source=str(source), path='changed'))


def test_admin_blanket_media_not_import_authority(tmp_path, policy):
    admin = replace(policy, is_admin=True)
    source = tmp_path / 'source'
    source.write_bytes(b'x')
    assert admin.allows_media(str(source))
    with policy_scope(admin):
        denied(store.execute_workspace({'action': 'import_attachment', 'source': str(source), 'path': 'x'},
                                       admin, base_dir=tmp_path / 'base'))


def test_csv_real_sort_dedupe(workspace):
    call, _ = workspace
    okay(call('write', path='input.csv', text='name,score\nb,2\na,3\nb,1\n"a,a",9\n'))
    result = okay(call('organize_csv', path='input.csv', destination='out.csv',
                       sort_by=['name', 'score'], dedupe_by=['name']))
    assert result['rows'] == 3
    assert okay(call('read', path='out.csv'))['text'] == 'name,score\na,3\n"a,a",9\nb,1\n'
    denied(call('organize_csv', path='input.csv', destination='out.csv', sort_by=['missing']))
    denied(call('organize_csv', path='input.csv', destination='out.csv', dedupe_by='name'))
    denied(call('organize_csv', path='input.csv', destination='out.csv'))
    okay(call('organize_csv', path='input.csv', destination='input.csv', sort_by=['score'], overwrite=True))
    okay(call('write', path='bad.csv', text='a,a\n1,2\n'))
    denied(call('organize_csv', path='bad.csv', destination='bad-out.csv'))


@pytest.mark.parametrize('limit,value', [('MAX_FILES', 1), ('MAX_ENTRIES', 1), ('WORKSPACE_BYTES', 5)])
def test_workspace_quotas(workspace, monkeypatch, limit, value):
    call, _ = workspace
    monkeypatch.setattr(store, limit, value)
    okay(call('write', path='a', text='12345'))
    denied(call('write', path='b', text='x'))
    denied(call('export', path='a'))
    assert okay(call('read', path='a'))['text'] == '12345'


def test_global_quota_and_directory_quota(workspace, policy, monkeypatch):
    call, base = workspace
    okay(call('write', path='a', text='12345'))
    monkeypatch.setattr(store, 'GLOBAL_BYTES', 6)  # includes one-byte Windows lock
    other = replace(policy, subject_id='other')
    denied(store.execute_workspace({'action': 'write', 'path': 'b', 'text': 'xx'}, other, base_dir=base))
    monkeypatch.setattr(store, 'GLOBAL_BYTES', 10000)
    monkeypatch.setattr(store, 'MAX_ENTRIES', 2)
    okay(call('mkdir', path='directory'))
    denied(call('mkdir', path='third'))
    monkeypatch.setattr(store, 'GLOBAL_ENTRIES', store._scan(base, 100)[0])
    denied(store.execute_workspace({'action': 'info'}, replace(policy, subject_id='third'), base_dir=base))


def test_input_output_and_overwrite_validation(workspace, monkeypatch):
    call, _ = workspace
    denied(call('write', path='x', text='x', overwrite='true'))
    denied(call('write', path='x', text=33))
    denied(call('write', path='x', text='x', overwrite=1))
    denied(call('read', path=['x']))
    monkeypatch.setattr(store, 'MAX_TEXT_BYTES', 5)
    denied(call('write', path='x', text='世界'))
    okay(call('write', path='x', text='12345'))
    monkeypatch.setattr(store, 'MAX_FILE_BYTES', 4)
    denied(call('write', path='b', text='12345'))
    monkeypatch.setattr(store, 'MAX_OUTPUT_BYTES', 4)
    denied(call('read', path='x'))


def test_hardlink_denied(workspace, tmp_path):
    call, base = workspace
    okay(call('write', path='a', text='private'))
    source = next(base.glob('*/a'))
    linked = tmp_path / 'hardlink'
    os.link(source, linked)
    denied(call('read', path='a'))
    denied(call('write', path='a', text='changed', overwrite=True))
    denied(call('export', path='a'))
    assert linked.read_text() == 'private'


@pytest.mark.platforms('posix')
def test_symlink_denied(workspace, tmp_path):
    call, base = workspace
    okay(call('info'))
    root = next(p for p in base.iterdir() if p.is_dir())
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('private')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    denied(call('read', path='link/secret'))
    denied(call('write', path='link/new', text='no'))
    assert not (outside / 'new').exists()


@pytest.mark.platforms('windows')
def test_actual_junction_denied(workspace, tmp_path):
    call, base = workspace
    okay(call('info'))
    root = next(p for p in base.iterdir() if p.is_dir())
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('private')
    junction = root / 'junction'
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        denied(call('read', path='junction/secret'))
        denied(call('write', path='junction/new', text='no'))
        assert not (outside / 'new').exists()
    finally:
        junction.rmdir()


def test_trusted_base_alias_denied(tmp_path, policy):
    base = tmp_path / 'base'
    base.mkdir()
    target = tmp_path / 'lock-target'
    target.write_bytes(b'0')
    os.link(target, base / '.lock')
    denied(store.execute_workspace({'action': 'info'}, policy, base_dir=base))


def test_thread_serialization_and_quota(workspace, monkeypatch):
    call, _ = workspace
    okay(call('info'))
    monkeypatch.setattr(store, 'MAX_FILES', 5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: call('write', path=f'{i}.txt', text='x'), range(20)))
    assert sum(r['success'] for r in results) == 5
    assert okay(call('info'))['usage']['files'] == 5


def test_errors_never_expose_host_paths(workspace, tmp_path):
    call, _ = workspace
    result = denied(call('read', path='missing'))
    assert str(tmp_path) not in json.dumps(result)
    denied(call('copy', path='missing', destination=str(tmp_path / 'escape')))
    denied(call('unknown'))


@pytest.fixture
def sandbox(monkeypatch):
    import sys
    import types
    import hermes_cli.config
    image = 'sha256:' + 'a' * 64
    monkeypatch.setattr(hermes_cli.config, 'load_config_readonly', lambda: {
        'plugins': {'entries': {'guest-workspace': {'docker_image': image}}}})
    state = {'success': True, 'stdout': 'done', 'stderr': '', 'files': {}}
    calls = []

    def runner(code, files, *, image, timeout):
        calls.append((code, files, image, timeout))
        return dict(state)

    monkeypatch.setitem(sys.modules, 'tools.guest_workspace_docker',
                        types.SimpleNamespace(run_python_sandbox=runner))
    return state, calls, image


def test_sandbox_snapshot_and_atomic_apply(workspace, sandbox):
    call, _ = workspace
    state, calls, image = sandbox
    okay(call('write', path='a.txt', text='old'))
    okay(call('write', path='keep.txt', text='keep'))
    okay(call('export', path='a.txt'))
    state['files'] = {'a.txt': b'old', 'nested/b.bin': b'\x00\xff'}
    result = okay(call('run_python', code='pass'))
    assert result == {'success': True, 'output_files': ['nested/b.bin'], 'stdout': 'done', 'stderr': ''}
    assert calls[0] == ('pass', {'a.txt': b'old', 'keep.txt': b'keep'}, image, 20)
    assert okay(call('read', path='keep.txt'))['text'] == 'keep'
    state['files'] = {'a.txt': b'changed', 'new.txt': b'new'}
    denied(call('run_python', code='pass'))
    denied(call('read', path='new.txt'))
    assert okay(call('read', path='a.txt'))['text'] == 'old'
    okay(call('run_python', code='pass', overwrite=True))
    assert okay(call('read', path='a.txt'))['text'] == 'changed'
    state['success'] = False
    state['files'] = {'failure.txt': b'bad'}
    denied(call('run_python', code='raise Exception()'))
    denied(call('read', path='failure.txt'))


@pytest.mark.parametrize('files', [
    {'safe': b'ok', '../escape': b'no'}, {'safe': b'ok', '.exports/x': b'no'},
    {'safe': b'ok', 'C:/host': b'no'}, {'safe': b'ok', 'bad': 'not bytes'},
    {'safe': b'ok', 'A': b'a', 'a': b'b'}, {'safe': b'ok', 'dir': b'a', 'dir/file': b'b'},
])
def test_sandbox_prevalidates_whole_batch(workspace, sandbox, files):
    call, _ = workspace
    state, _, _ = sandbox
    state['files'] = files
    denied(call('run_python', code='pass', overwrite=True))
    denied(call('read', path='safe'))


def test_sandbox_quota_and_text_limits(workspace, sandbox, monkeypatch):
    call, _ = workspace
    state, _, _ = sandbox
    monkeypatch.setattr(store, 'WORKSPACE_BYTES', 5)
    state['files'] = {'a': b'123', 'b': b'456'}
    denied(call('run_python', code='pass'))
    denied(call('read', path='a'))
    denied(call('run_python', code='x' * 32769))
    state['files'] = {'a': b'1'}
    state['stdout'] = 'x' * store.MAX_OUTPUT_BYTES
    denied(call('run_python', code='pass'))
    denied(call('read', path='a'))


@pytest.mark.parametrize('image', [None, 'python:latest', 'sha256:abc', 'repo@sha256:' + 'a' * 64])
def test_sandbox_image_must_be_trusted_full_id(workspace, sandbox, monkeypatch, image):
    import hermes_cli.config
    call, _ = workspace
    _, calls, _ = sandbox
    monkeypatch.setattr(hermes_cli.config, 'load_config_readonly', lambda: {
        'plugins': {'entries': {'guest-workspace': {'docker_image': image}}}})
    denied(call('run_python', code='pass'))
    denied(call('run_python', code='pass', image='sha256:' + 'a' * 64))
    assert not calls


def test_sandbox_commit_io_failure_rolls_back(workspace, sandbox, monkeypatch):
    call, _ = workspace
    state, _, _ = sandbox
    okay(call('write', path='a', text='original'))
    state['files'] = {'a': b'changed', 'dir/b': b'new'}
    original_replace = store.os.replace

    def fail_second(source, target):
        if Path(target).name == 'b':
            raise OSError('injected commit failure')
        return original_replace(source, target)

    monkeypatch.setattr(store.os, 'replace', fail_second)
    denied(call('run_python', code='pass', overwrite=True))
    assert okay(call('read', path='a'))['text'] == 'original'
    denied(call('read', path='dir/b'))
    assert [e['path'] for e in okay(call('list'))['entries']] == ['a']
