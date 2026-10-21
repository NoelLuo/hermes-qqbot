"""Real guest lifecycle/storage contracts, exclusively on isolated temporary data."""
import json
import os
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from tools import guest_workspace_store as store
from tools import guest_workspace_lifecycle as lifecycle


@pytest.fixture
def env(tmp_path, monkeypatch):
    import hermes_constants
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    base = tmp_path / 'guests'
    policy = RequestSecurityPolicy('visitor', 'qqbot', 'group:one')

    def call(action, **args):
        return store.execute_workspace({'action': action, **args}, policy, base_dir=base)

    def root():
        return next(p for p in base.iterdir() if p.is_dir())

    return call, base, policy, root


def okay(result):
    assert result['success'], result
    return result


def denied(result):
    assert result['success'] is False, result
    return result


def import_file(env, tmp_path, name='temporary.txt', *, temporary=True, data=b'payload'):
    call, _, policy, _ = env
    source = tmp_path / (uuid.uuid4().hex + '.bin')
    source.write_bytes(data)
    assert policy.grant_artifact(str(source))
    with policy_scope(policy):
        return okay(call('import_attachment', path=name, source=str(source), temporary=temporary))


def test_large_attachment_export_and_independently_bounded_python(env, tmp_path, monkeypatch):
    call, _, _, root = env
    data = b'x' * (32 * 1024 * 1024)
    import_file(env, tmp_path, name='large.txt', temporary=False, data=data)
    assert (root() / 'large.txt').stat().st_size == len(data)
    result = okay(call('export', path='large.txt'))
    assert Path(result['file_path']).read_bytes() == data
    limits = okay(call('info'))['limits']
    assert limits['workspace_bytes'] >= 256 * 1024 * 1024
    assert limits['global_bytes'] >= 2 * 1024 * 1024 * 1024
    assert limits['files'] >= 2048 and limits['entries'] >= 4096
    denied(call('write', path='text.txt', text='x' * (128 * 1024 + 1)))
    denied(call('read', path='large.txt'))
    original = store._read
    reads = []

    def bounded(path, maximum=store.MAX_FILE_BYTES):
        reads.append((path.name, maximum))
        assert maximum <= 256 * 1024
        return original(path, maximum)

    monkeypatch.setattr(store, '_read', bounded)
    # Call snapshot directly to prove it rejects by stat BEFORE _read is called.
    with pytest.raises(store.WorkspaceError):
        store._public_snapshot(root(), ['large.txt'])
    assert reads == []
    assert store._public_snapshot(root(), []) == {}


def test_attachment_above_file_limit_has_no_partial_file(env, tmp_path):
    call, _, policy, root = env
    source = tmp_path / 'too-big'
    with source.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    policy.grant_artifact(str(source))
    with policy_scope(policy):
        denied(call('import_attachment', path='oversize', source=str(source)))
    assert not (root() / 'oversize').exists()


def test_python_total_and_count_are_checked_before_unbounded_reads(env, monkeypatch):
    call, _, _, root = env
    okay(call('info'))
    for i in range(5):
        (root() / f'{i}.txt').write_bytes(b'x' * (256 * 1024))
    reads = []
    original = store._read

    def tracked(path, maximum):
        reads.append(path.name)
        return original(path, maximum)

    monkeypatch.setattr(store, '_read', tracked)
    with pytest.raises(store.WorkspaceError):
        store._public_snapshot(root(), [f'{i}.txt' for i in range(5)])
    assert reads == ['0.txt', '1.txt', '2.txt', '3.txt']
    reads.clear()
    with pytest.raises(store.WorkspaceError):
        store._public_snapshot(root(), ['0.txt'] * 33)
    assert not reads


def test_delete_confirm_empty_directory_reserved_and_overquota(env, monkeypatch):
    call, _, _, root = env
    okay(call('write', path='a', text='persistent'))
    okay(call('mkdir', path='empty'))
    okay(call('mkdir', path='nonempty'))
    okay(call('write', path='nonempty/a', text='keep'))
    snapshot = Path(okay(call('export', path='a'))['file_path'])
    for confirm in (None, False, 1, 'true'):
        denied(call('delete', path='a', confirm=confirm))
    for name in ('', '.', '..', '*', 'nonempty', '.lifecycle.json',
                 '.exports/' + snapshot.name, '.lock', '.txn-anything'):
        denied(call('delete', path=name, confirm=True))
    assert snapshot.exists() and (root() / 'nonempty/a').exists()
    monkeypatch.setattr(store, 'MAX_FILES', 1)
    monkeypatch.setattr(store, 'MAX_ENTRIES', 1)
    monkeypatch.setattr(store, 'GLOBAL_BYTES', 1)
    denied(call('info'))
    okay(call('delete', path='a', confirm=True))
    okay(call('delete', path='empty', confirm=True))
    assert not (root() / 'a').exists()
    assert not (root() / 'empty').exists()
    assert snapshot.exists()


def test_temporary_expiry_persistent_survives_and_exports_live_seven_days(env, tmp_path, monkeypatch):
    call, _, _, root = env
    started = time.time()
    import_file(env, tmp_path)
    import_file(env, tmp_path, name='persistent.txt', temporary=False)
    okay(call('write', path='work.txt', text='never auto-delete'))
    export = Path(okay(call('export', path='work.txt'))['file_path'])
    # Old ordinary mtimes are NOT classification.
    os.utime(root() / 'persistent.txt', (1, 1))
    os.utime(root() / 'work.txt', (1, 1))
    monkeypatch.setattr(lifecycle.time, 'time', lambda: started + lifecycle.TEMPORARY_TTL + 10)
    result = okay(call('cleanup'))
    assert result['deleted_files'] == 1
    assert not (root() / 'temporary.txt').exists()
    assert export.exists() and (root() / 'persistent.txt').exists() and (root() / 'work.txt').exists()
    monkeypatch.setattr(lifecycle.time, 'time', lambda: started + lifecycle.EXPORT_TTL + 10)
    assert okay(call('cleanup'))['deleted_files'] == 1
    assert not export.exists()
    assert (root() / 'persistent.txt').exists() and (root() / 'work.txt').exists()


def test_overwritten_or_replaced_temporary_and_export_identity_survive(env, tmp_path):
    call, _, _, root = env
    import_file(env, tmp_path, name='changed.txt')
    import_file(env, tmp_path, name='replaced.txt')
    okay(call('write', path='changed.txt', text='edited', overwrite=True))
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'payload')
    os.replace(replacement, root() / 'replaced.txt')
    export = Path(okay(call('export', path='changed.txt'))['file_path'])
    replacement.write_bytes(b'new export')
    os.replace(replacement, export)
    now = time.time() + lifecycle.EXPORT_TTL + 100
    report = lifecycle.sweep_expired_workspaces(base_dir=env[1], now=now)
    assert report['deleted_files'] == 0
    assert (root() / 'changed.txt').read_text() == 'edited'
    assert (root() / 'replaced.txt').exists() and export.exists()


def test_opportunistic_cleanup_frees_quota_and_explicit_cleanup_ignores_quota(env, tmp_path, monkeypatch):
    call, _, _, root = env
    import_file(env, tmp_path)
    monkeypatch.setattr(store, 'MAX_FILES', 1)
    monkeypatch.setattr(store, 'MAX_ENTRIES', 1)
    monkeypatch.setattr(store, 'WORKSPACE_BYTES', 1)
    monkeypatch.setattr(lifecycle.time, 'time', lambda: float(4102444800))
    assert okay(call('cleanup'))['deleted_files'] == 1
    assert not (root() / 'temporary.txt').exists()
    monkeypatch.setattr(store, 'MAX_ENTRIES', 4096)
    monkeypatch.setattr(store, 'MAX_FILES', 2048)
    monkeypatch.setattr(store, 'WORKSPACE_BYTES', 256 * 1024 * 1024)
    import_file(env, tmp_path, name='again.txt')
    monkeypatch.setattr(lifecycle.time, 'time', lambda: float(4102444800 + lifecycle.TEMPORARY_TTL + 10))
    # Existing metadata + one file fits exactly. Without opportunistic prune a
    # third file would exceed MAX_FILES during write's transaction.
    monkeypatch.setattr(store, 'MAX_FILES', 2)
    okay(call('write', path='new.txt', text='new'))
    assert not (root() / 'again.txt').exists()


@pytest.mark.parametrize('metadata', [b'{broken', b'{}', b'{"version":1,"records":{"../escape":{}}}',
                                    b'{"version":1,"records":{},"records":{}}',
                                    b'[' * 2000 + b'0' + b']' * 2000])
def test_corrupt_metadata_deletes_nothing_and_is_not_rewritten(env, tmp_path, metadata):
    call, base, _, root = env
    import_file(env, tmp_path)
    snapshot = Path(okay(call('export', path='temporary.txt'))['file_path'])
    (root() / lifecycle.METADATA).write_bytes(metadata)
    report = lifecycle.sweep_expired_workspaces(base_dir=base, now=time.time() + lifecycle.EXPORT_TTL + 10)
    assert report['deleted_files'] == 0 and report['skipped'] >= 1
    assert (root() / 'temporary.txt').exists() and snapshot.exists()
    assert (root() / lifecycle.METADATA).read_bytes() == metadata
    denied(call('export', path='temporary.txt'))


def test_legacy_exports_only_generated_regular_names(env):
    call, base, _, root = env
    okay(call('write', path='ordinary', text='keep'))
    exports = root() / '.exports'
    exports.mkdir()
    eligible = exports / (uuid.uuid4().hex + '-old.txt')
    recent = exports / (uuid.uuid4().hex + '-recent.txt')
    unknown = exports / 'old.txt'
    for path in (eligible, recent, unknown, root() / 'ordinary'):
        path.write_bytes(b'keep')
        os.utime(path, (1, 1))
    os.utime(recent, None)
    report = lifecycle.sweep_expired_workspaces(base_dir=base)
    assert report['deleted_files'] == 1
    assert not eligible.exists()
    assert recent.exists() and unknown.exists() and (root() / 'ordinary').exists()


def test_metadata_is_private_and_quota_accounted(env, tmp_path):
    call, _, _, root = env
    import_file(env, tmp_path)
    usage = okay(call('info'))['usage']
    assert usage['files'] == 2
    assert usage['bytes'] == sum(p.stat().st_size for p in root().iterdir())
    for action in ('read', 'list', 'export', 'delete'):
        denied(call(action, path=lifecycle.METADATA, **({'confirm': True} if action == 'delete' else {})))
    assert [item['path'] for item in okay(call('list'))['entries']] == ['temporary.txt']
    assert not okay(call('search', query='identity'))['matches']
    assert store._public_snapshot(root(), ['temporary.txt']) == {'temporary.txt': b'payload'}


def test_metadata_commit_failure_never_installs_temporary_file(env, tmp_path, monkeypatch):
    call, _, _, root = env
    okay(call('info'))
    original = os.replace

    def fail_metadata(source, target):
        if Path(target).name == lifecycle.METADATA:
            raise OSError('injected failure')
        return original(source, target)

    monkeypatch.setattr(store.os, 'replace', fail_metadata)
    with pytest.raises(AssertionError):
        import_file(env, tmp_path)
    assert not (root() / 'temporary.txt').exists()
    assert not list(root().glob('.txn-*'))


def test_hardlinked_temporary_and_export_never_pruned_or_deleted(env, tmp_path):
    call, base, _, root = env
    import_file(env, tmp_path)
    export = Path(okay(call('export', path='temporary.txt'))['file_path'])
    os.link(root() / 'temporary.txt', tmp_path / 'outside-temp')
    os.link(export, tmp_path / 'outside-export')
    denied(call('delete', path='temporary.txt', confirm=True))
    report = lifecycle.sweep_expired_workspaces(base_dir=base, now=time.time() + lifecycle.EXPORT_TTL + 10)
    assert report['deleted_files'] == 0 and report['skipped'] >= 1
    assert export.exists() and (root() / 'temporary.txt').exists()


def test_sweep_requires_explicit_base_missing_noop_bounded_and_continues(env, tmp_path):
    _call, base, policy, root = env
    missing = tmp_path / 'missing'
    assert lifecycle.sweep_expired_workspaces(base_dir=missing)['tenants'] == 0
    assert not missing.exists()
    with pytest.raises(TypeError):
        lifecycle.sweep_expired_workspaces()
    import_file(env, tmp_path)
    (root() / lifecycle.METADATA).write_bytes(b'corrupt')
    other = replace(policy, subject_id='other')
    okay(store.execute_workspace({'action': 'write', 'path': 'a', 'text': 'keep'}, other, base_dir=base))
    exported = okay(store.execute_workspace({'action': 'export', 'path': 'a'}, other, base_dir=base))
    report = lifecycle.sweep_expired_workspaces(base_dir=base, now=time.time() + lifecycle.EXPORT_TTL + 10)
    assert report['tenants'] == 2 and report['skipped'] >= 1 and report['deleted_files'] == 1
    assert not Path(exported['file_path']).exists()
    assert str(base) not in json.dumps(report) and policy.subject_id not in json.dumps(report)
    assert lifecycle.sweep_expired_workspaces(base_dir=base, max_entries=1)['truncated']


@pytest.mark.platforms('windows')
def test_actual_junction_sweep_never_enters_other_directory(env, tmp_path):
    call, base, _, root = env
    okay(call('info'))
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / (uuid.uuid4().hex + '-secret.txt')
    target.write_bytes(b'private')
    os.utime(target, (1, 1))
    junction = root() / '.exports'
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        report = lifecycle.sweep_expired_workspaces(base_dir=base)
        assert report['deleted_files'] == 0 and report['skipped'] >= 1
        assert target.read_bytes() == b'private'
    finally:
        junction.rmdir()


def test_list_large_names_is_bounded_and_reports_truncation(env):
    call, _, _, root = env
    okay(call('info'))
    for i in range(2048):
        (root() / (str(i).zfill(4) + 'x' * 96)).write_bytes(b'')
    listing = okay(call('list'))
    assert listing['truncated'] and 0 < len(listing['entries']) < 2048
    assert len(json.dumps(listing, ensure_ascii=False).encode('utf-8')) < store.MAX_OUTPUT_BYTES


def test_browser_persistence_policy_batch_namespace_no_grants_and_survival(env):
    call, base, policy, root = env
    browser_policy = replace(policy, allowed_tools=policy.allowed_tools | {'guest_browser'})
    files = {'screen.png': b'png bytes', 'data.csv': b'a,b\n1,2\n'}
    denied(store.save_browser_artifacts(files, browser_policy, base_dir=base))
    assert not base.exists()
    with policy_scope(policy):
        denied(store.save_browser_artifacts(files, browser_policy, base_dir=base))
    denied_policy = replace(policy, allowed_tools=policy.allowed_tools - {'guest_browser'})
    with policy_scope(denied_policy):
        denied(store.save_browser_artifacts(files, denied_policy, base_dir=base))
    with policy_scope(browser_policy):
        result = okay(store.save_browser_artifacts(files, browser_policy, base_dir=base))
        again = okay(store.save_browser_artifacts(files, browser_policy, base_dir=base))
        denied(store.save_browser_artifacts({'../a.png': b'no'}, browser_policy, base_dir=base))
        denied(store.save_browser_artifacts({'a.exe': b'no'}, browser_policy, base_dir=base))
        denied(store.save_browser_artifacts({f'{i}.png': b'x' for i in range(9)}, browser_policy, base_dir=base))
    paths = [item['path'] for item in result['files'] + again['files']]
    assert len(set(paths)) == 4
    for item in result['files']:
        path = root() / item['path']
        assert item['path'].startswith('browser/') and path.stat().st_size == item['bytes']
        assert not browser_policy._artifacts.allows(str(path))
    lifecycle.sweep_expired_workspaces(base_dir=base, now=time.time() + 10 * lifecycle.EXPORT_TTL)
    assert all((root() / name).exists() for name in paths)
    okay(call('delete', path=paths[0], confirm=True))


def test_browser_batch_quota_failure_leaves_no_partial_artifacts(env, monkeypatch):
    _, base, policy, root = env
    policy = replace(policy, allowed_tools=policy.allowed_tools | {'guest_browser'})
    monkeypatch.setattr(store, 'WORKSPACE_BYTES', 3)
    with policy_scope(policy):
        denied(store.save_browser_artifacts({'a.png': b'12', 'b.pdf': b'34'}, policy, base_dir=base))
    assert not (root() / 'browser').exists()
    assert not list(root().glob('.txn-*'))


def test_sweep_and_workspace_operations_share_serialization(env, tmp_path):
    call, base, _, _ = env
    import_file(env, tmp_path)
    now = time.time() + lifecycle.EXPORT_TTL + 10
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(lifecycle.sweep_expired_workspaces, base_dir=base, now=now)
                   for _ in range(4)]
        futures += [executor.submit(call, 'write', path=f'{i}.txt', text='persistent') for i in range(4)]
        results = [future.result() for future in futures]
    assert sum(result.get('deleted_files', 0) for result in results) == 1
    assert all(result.get('success', True) for result in results)


def test_browser_real_large_file_and_batch_bound(env):
    _, base, policy, root = env
    policy = replace(policy, allowed_tools=policy.allowed_tools | {'guest_browser'})
    data = b'p' * (32 * 1024 * 1024)
    with policy_scope(policy):
        denied(store.save_browser_artifacts({'a.pdf': data, 'b.pdf': data, 'c.txt': b'x'},
                                            policy, base_dir=base))
        assert not base.exists()
        result = okay(store.save_browser_artifacts({'paper.pdf': data}, policy, base_dir=base))
    assert (root() / result['files'][0]['path']).read_bytes() == data


def test_browser_commit_failure_rolls_back_full_batch(env, monkeypatch):
    _, base, policy, root = env
    policy = replace(policy, allowed_tools=policy.allowed_tools | {'guest_browser'})
    original = os.replace
    installed = []

    def fail_second(source, target):
        if Path(target).parent.name == 'browser':
            installed.append(str(target))
            if len(installed) == 2:
                raise OSError('injected browser commit failure')
        return original(source, target)

    monkeypatch.setattr(store.os, 'replace', fail_second)
    with policy_scope(policy):
        denied(store.save_browser_artifacts({'a.png': b'a', 'b.pdf': b'b'}, policy, base_dir=base))
    assert len(installed) == 2
    assert not (root() / 'browser').exists()
    assert not list(root().glob('.txn-*'))


@pytest.mark.platforms('windows')
def test_tenant_junction_is_skipped_while_other_tenant_is_cleaned(env, tmp_path):
    call, base, _, _ = env
    okay(call('info'))
    outside = tmp_path / 'outside-tenant'
    outside.mkdir()
    sentinel = outside / 'keep'
    sentinel.write_bytes(b'private')
    junction = base / ('f' * 64)
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(outside)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    try:
        report = lifecycle.sweep_expired_workspaces(base_dir=base)
        assert report['skipped'] >= 1 and report['deleted_files'] == 0
        assert sentinel.read_bytes() == b'private'
    finally:
        junction.rmdir()
