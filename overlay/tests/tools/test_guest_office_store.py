"""Real store/transaction tests; the container seam is stubbed, NOT Docker proof."""
import io
import json
import os
import zipfile
from dataclasses import replace

import pytest
import hermes_yaml as yaml

from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from tools import guest_office_docker, guest_workspace_store as store


@pytest.fixture
def office(tmp_path, monkeypatch):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(yaml.safe_dump({'plugins': {'entries': {
        'guest-workspace': {'office_image': 'sha256:' + 'a' * 64}}}}), encoding='utf-8')
    policy = RequestSecurityPolicy('visitor', 'qqbot', 'group')
    base = tmp_path / 'storage'
    seen = []
    reply = {'success': True, 'stdout': '', 'stderr': '', 'errors': [], 'files': {}}

    def runner(command, files, *, image, timeout=60):
        seen.append((command, files, image, timeout))
        return reply.copy()

    monkeypatch.setattr(guest_office_docker, 'run_office_sandbox', runner)

    def call(action='run_office', *, as_policy=None, **args):
        p = as_policy or policy
        with policy_scope(p):
            return store.execute_workspace({'action': action, **args}, p, base_dir=base)

    return call, reply, seen, base, policy


def test_binary_zip_24mib_import_roundtrip_repeat_and_export_ledger(office, tmp_path):
    call, reply, seen, base, policy = office
    source = tmp_path / 'source.zip'
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('payload.bin', b'\x00\xff\x80\x01' * (6 * 1024 * 1024))
    payload = source.read_bytes()
    assert 24 * 1024 * 1024 < len(payload) < store.MAX_FILE_BYTES
    assert policy.grant_artifact(source)
    assert call('import_attachment', source=str(source), path='input.zip')['success']
    exported = call('export', path='input.zip')
    assert exported['success'] and policy.allows_media(exported['file_path'])
    reply['files'] = {'input.zip': payload, 'output/result.zip': payload}
    result = call(command='cp input.zip output/result.zip')
    assert result['success'] and result['output_files'] == ['output/result.zip']
    assert seen[-1][1] == {'input.zip': payload}  # excludes export and ledger
    assert seen[-1][2:] == ('sha256:' + 'a' * 64, 60)
    assert call(command='same')['output_files'] == []
    assert not policy.allows_media(next(base.glob('*/output/result.zip')))
    assert next(base.glob('*/output/result.zip')).read_bytes() == payload
    assert call(command='no inputs', inputs=[])['success']
    assert seen[-1][1] == {}
    with zipfile.ZipFile(io.BytesIO(next(base.glob('*/output/result.zip')).read_bytes())) as archive:
        assert archive.testzip() is None


def test_changed_existing_requires_overwrite_no_sync_delete(office):
    call, reply, _, base, _ = office
    assert call('write', path='keep.txt', text='old')['success']
    reply['files'] = {'keep.txt': b'new', 'new.bin': b'\x00\xff'}
    assert not call(command='change')['success']
    assert not list(base.glob('*/new.bin'))
    assert call('read', path='keep.txt')['text'] == 'old'
    assert call(command='change', overwrite=True)['output_files'] == ['keep.txt', 'new.bin']
    reply['files'] = {}
    assert call(command='rm keep.txt', overwrite=True)['success']
    assert call('read', path='keep.txt')['text'] == 'new'


def test_failure_diagnostics_bounded_no_partial_save(office):
    call, reply, _, base, _ = office
    reply.update(success=False, stdout='x' * 100000, stderr='C:/Users/example/secret.py bad command',
                 errors=['missing executable'], files={'oops.txt': b'oops'}, error_code='command_failed')
    result = call(command='not-an-app')
    assert not result['success'] and result['error_code'] == 'command_failed'
    assert 'C:/Users/example' not in json.dumps(result) and '[host-path]' in result['stderr']
    assert len(result['stdout'].encode()) < 17000
    assert result['errors'] == ['missing executable']
    assert not list(base.glob('*/oops.txt'))


@pytest.mark.parametrize('args', [
    {'command': ''}, {'command': 'a\x00b'}, {'command': 'x' * 32769},
    {'command': 'ok', 'timeout': True}, {'command': 'ok', 'timeout': 0},
    {'command': 'ok', 'timeout': 91}, {'command': 'ok', 'timeout': 1.0},
    {'command': 'ok', 'image': 'sha256:' + 'a' * 64},
    {'command': 'ok', 'overwrite': 1}, {'command': 'ok', 'inputs': ['a', 'A']},
    {'command': 'ok', 'inputs': ['x'] * 257}, {'command': 'ok', 'inputs': ['../a']},
    {'command': 'ok', 'inputs': ['.exports/x']}, {'command': 'ok', 'inputs': 'a'},
])
def test_invalid_arguments_fail_before_runner(office, args):
    call, _, seen, _, _ = office
    assert not call(**args)['success']
    assert seen == []


@pytest.mark.parametrize('outputs', [
    {'fine.txt': b'ok', '../escape': b'no'}, {'fine.txt': b'ok', '.lifecycle.json': b'no'},
    {'a': b'x', 'a/b': b'y'}, {'a/x': b'x', 'A/y': b'y'}, {'a': b'x', 'A': b'y'},
    {'fine.txt': b'ok', 'wrong.bin': 'not bytes'}, {str(n): b'x' for n in range(257)},
    {'a/' * 8 + 'x': b'x'}, {'x' * 101: b'x'},
])
def test_output_prevalidation_atomic_reject(office, outputs):
    call, reply, _, base, _ = office
    reply['files'] = outputs
    assert not call(command='produce')['success']
    root = next(p for p in base.iterdir() if p.is_dir())
    assert list(root.iterdir()) == []


def test_cross_person_chat_scope_and_current_policy_isolation(office):
    call, _, seen, base, policy = office
    assert call('write', path='mine.txt', text='mine')['success']
    for changes in ({'subject_id': 'other'}, {'chat_id': 'other'}, {'scope_id': 'other'}):
        assert call(command='list', as_policy=replace(policy, **changes))['success']
        assert seen[-1][1] == {}
    restricted = replace(policy, allowed_tools=frozenset({'web_search'}))
    assert call(command='id', as_policy=restricted)['error_code'] == 'workspace_forbidden'
    with policy_scope(replace(policy, subject_id='other')):
        assert not store.execute_workspace({'action': 'run_office', 'command': 'id'}, policy, base_dir=base)['success']
    assert seen[0][1] == {}


@pytest.mark.parametrize('image', [None, 'latest', 'sha256:short'])
def test_pinned_config_required_no_fallback(office, image):
    call, _, seen, _, _ = office
    config = {'plugins': {'entries': {'guest-workspace': {'office_image': image}}}}
    (get_hermes_home() / 'config.yaml').write_text(yaml.safe_dump(config), encoding='utf-8')
    assert call(command='true')['error_code'] == 'office_not_configured'
    assert not seen


def test_quota_preflight_leaves_no_new_or_changed_files(office, monkeypatch):
    call, reply, _, base, _ = office
    assert call('write', path='keep.txt', text='old')['success']
    monkeypatch.setattr(store, 'WORKSPACE_BYTES', 12)
    reply['files'] = {'keep.txt': b'replacement', 'next': b'new'}
    assert not call(command='produce', overwrite=True)['success']
    assert call('read', path='keep.txt')['text'] == 'old'
    assert not list(base.glob('*/next'))


def test_transaction_rollback_after_first_install(office, monkeypatch):
    call, reply, _, base, _ = office
    assert call('write', path='keep.txt', text='old')['success']
    original = store.os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if str(destination).endswith('new.bin') and not failed:
            failed = True
            raise OSError('injected C:/host/private/path')
        return original(source, destination)

    monkeypatch.setattr(store.os, 'replace', fail_once)
    reply['files'] = {'keep.txt': b'new', 'new.bin': b'bytes'}
    result = call(command='produce', overwrite=True)
    assert not result['success'] and 'private' not in json.dumps(result)
    assert failed and call('read', path='keep.txt')['text'] == 'old'
    root = next(p for p in base.iterdir() if p.is_dir())
    assert sorted(p.name for p in root.iterdir()) == ['keep.txt']


def test_binary_per_file_and_total_output_caps(office):
    call, reply, _, base, _ = office
    reply['files'] = {'too.bin': bytes(store.MAX_FILE_BYTES + 1)}
    assert not call(command='large')['success']
    chunk = bytes(store.MAX_FILE_BYTES)
    reply['files'] = {f'{i}.bin': chunk for i in range(5)}
    assert not call(command='large total')['success']
    assert not list(base.glob('*/*.bin'))


def test_input_total_cap_and_hardlink_rejected_before_runner(office):
    call, _, seen, base, _ = office
    assert call('info')['success']
    root = next(p for p in base.iterdir() if p.is_dir())
    for i in range(5):
        (root / f'{i}.bin').write_bytes(bytes(store.MAX_FILE_BYTES))
    assert call(command='large')['error_code'] == 'office_input_limit'
    assert not seen
    for item in root.iterdir():
        item.unlink()
    (root / 'a').write_bytes(b'x')
    os.link(root / 'a', root / 'alias')
    assert not call(command='linked')['success'] and not seen


def test_large_document_character_slicing(office):
    call, _, _, base, _ = office
    assert call('info')['success']
    root = next(p for p in base.iterdir() if p.is_dir())
    text = '中文abc\n' * 100000
    (root / 'large.txt').write_bytes(text.encode('utf-8'))
    result = call('read', path='large.txt', offset=100000, limit=10000)
    assert result['success'] and result['text'] == text[100000:110000]
    assert result['next_offset'] == 110000 and result['truncated']
    assert not call('read', path='large.txt', limit=16385)['success']
    assert not call('read', path='large.txt', offset=True)['success']
    assert not call('info', offset=1)['success']
