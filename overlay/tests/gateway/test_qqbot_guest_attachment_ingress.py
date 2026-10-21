"""Real QQ download/intake -> native snapshot -> fresh policy -> workspace I/O."""

import os
import json
import base64
import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
import model_tools

from agent.request_security import policy_scope
from agent.image_routing import build_native_content_parts
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore
from gateway.session_authority import initialize_session_authority
from gateway.session_envelope import prepare_native, restore_native
from gateway.inbound_attachment_proofs import eligible_paths
from gateway.request_security import grant_verified_event_attachments, policy_for_source
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.group_history import prepare_turn_history
from tools.guest_workspace_interface import register
from tools.registry import registry

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    home = Path(os.environ['HERMES_HOME'])
    monkeypatch.setenv('QQ_ALLOW_ALL_USERS', 'true')
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    scope = registry.current_scope_key()
    previous = registry.snapshot_registration('guest_workspace', scope=scope)
    class Context:
        def register_tool(self, **kwargs):
            registry.register(**kwargs, scope=scope)
    register(Context())
    registered = registry.snapshot_registration('guest_workspace', scope=scope)
    config = PlatformConfig(enabled=True, extra={
        'group_policy': 'open', 'group_reply_without_mention': ['group'],
        'group_allow_admin_from': ['admin'], 'allow_admin_from': ['admin'],
        'role_policy': {'enabled': True}})
    runner = GatewayRunner(GatewayConfig(sessions_dir=home / 'sessions',
                                        group_sessions_per_user=False,
                                        platforms={Platform.QQBOT: config}))
    adapter = QQAdapter(config)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.QQBOT: adapter}
    adapter.set_session_store(runner.session_store)
    adapter.set_authorization_check(lambda *_: True)
    adapter._group_reply_gate._judge = AsyncMock(return_value=False)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='attachments')
    body = {'bytes': b'fixture data\n'}
    def transport(request):
        assert request.url.host == '1.1.1.1'
        return httpx.Response(200, content=body['bytes'])
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    events = []
    async def handle(event):
        events.append(event)
    monkeypatch.setattr(adapter, 'handle_message', handle)
    yield runner, adapter, body, events
    await adapter._http_client.aclose()
    authority.db.close()
    registry.restore_registration('guest_workspace', registered, previous, scope=scope)


def attachment(mime='file', name='data.txt'):
    return {'content_type': mime, 'filename': name, 'url': 'https://1.1.1.1/fixture'}


async def ingest(adapter, attachments, actor='guest', mid='current'):
    await adapter._ingest({}, mid, 'question', attachments, '', chat_id='group',
                          qq_chat_type='group', user_id=actor, chat_type='group')


def import_export(runner, event, tmp_path):
    paths = grant_verified_event_attachments(runner, event)
    assert len(paths) == 1
    policy = policy_for_source(runner, event.source)
    with policy_scope(policy):
        result = json.loads(model_tools.handle_function_call('guest_workspace', {
            'action': 'import_attachment', 'source': paths[0], 'path': 'input.bin'}))
        assert result['success'], result
        exported = json.loads(model_tools.handle_function_call('guest_workspace', {
            'action': 'export', 'path': 'input.bin'}))
        assert exported['success'], exported
        assert Path(exported['file_path']).read_bytes() == Path(paths[0]).read_bytes()
    return paths


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', ['guest', 'admin'])
@pytest.mark.parametrize('mime,name', [('file', 'data.txt'), ('image/png', 'picture.png')])
async def test_current_media_native_restore_import_export(rig, tmp_path, actor, mime, name):
    runner, adapter, body, events = rig
    if mime == 'image/png':
        body['bytes'] = PNG
    await ingest(adapter, [attachment(mime, name)], actor)
    original = events[-1]
    old_path = original.media_urls[0]
    payload = await prepare_native(runner, original)
    restored = restore_native(payload, runner)
    assert restored.media_urls[0] != old_path
    assert not hasattr(restored.source, '_request_security_policy')
    paths = import_export(runner, restored, tmp_path)
    assert paths == restored.media_urls
    if mime == 'image/png':
        parts, skipped = build_native_content_parts(restored.text, restored.media_urls)
        assert not skipped and parts[1]['image_url']['url'].startswith('data:image/png;base64,')
        assert restored.media_urls[0] in parts[0]['text']
    assert old_path not in restored.text
    assert 'request_security_policy' not in str(payload)
    private = adapter.build_source(chat_id=actor, user_id='admin', chat_type='dm')
    assert policy_for_source(runner, private).public_context is False


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [8 * 1024 * 1024, 32 * 1024 * 1024])
async def test_quiet_file_followup_survives_intake_and_restore(rig, tmp_path, size):
    runner, adapter, body, events = rig
    body['bytes'] = b'x' * size
    data = {'group_openid': 'group', 'attachments': [attachment()]}
    await adapter._handle_plain_group_message(data, 'quiet', '', {'member_openid': 'guest'}, '')
    assert not events
    await ingest(adapter, [], mid='followup')
    event = events[-1]
    sid = runner.session_store.get_or_create_session(event.source).session_id
    # A new codec/source and fresh transcript load, not hand-built policy grants.
    event = restore_native(await prepare_native(runner, event), runner)
    await prepare_turn_history(AsyncSessionStore(runner.session_store), sid, event)
    assert len(event.media_urls) == 1
    import_export(runner, event, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['file', 'image/png'])
async def test_dispatcher_retains_own_attachment_with_autoreply_disabled(rig, tmp_path, monkeypatch, kind):
    runner, adapter, body, events = rig
    if kind.startswith('image/'):
        body['bytes'] = PNG
    monkeypatch.setattr(adapter._group_reply_gate, 'enabled', lambda _: False)
    adapter._group_reply_gate.should_reply = AsyncMock(return_value=True)
    data = {'id': 'silent-file', 'group_openid': 'group', 'author': {'member_openid': 'guest'},
            'content': 'file caption', 'attachments': [attachment(kind)]}
    await adapter._group_events.dispatch(adapter, 'GROUP_MESSAGE_CREATE', data)
    assert not events
    adapter._group_reply_gate.should_reply.assert_not_awaited()
    await adapter._group_events.dispatch(adapter, 'GROUP_AT_MESSAGE_CREATE', {
        'id': 'followup-at', 'group_openid': 'group', 'author': {'member_openid': 'guest'},
        'content': 'inspect the attachment'})
    event = events[-1]
    sid = runner.session_store.get_or_create_session(event.source).session_id
    event = restore_native(await prepare_native(runner, event), runner)
    await adapter.prepare_group_turn_history(AsyncSessionStore(runner.session_store), sid, event)
    import_export(runner, event, tmp_path)
    adapter._group_reply_gate.should_reply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', ['guest', 'other'])
async def test_identical_native_recapture_keeps_queued_receipt_and_detects_tampering(rig, tmp_path, monkeypatch, actor):
    runner, adapter, body, events = rig
    await ingest(adapter, [attachment()], mid='queued-A')
    original = events[-1]
    waiting = restore_native(await prepare_native(runner, original), runner)
    snapshot = Path(waiting.media_urls[0])
    info = snapshot.stat()
    # The real cache may deduplicate an identical download; keep its transport
    # path to exercise the shared native alias, not a manually granted policy.
    monkeypatch.setattr(adapter, '_download_and_cache', AsyncMock(return_value=original.media_urls[0]))
    await ingest(adapter, [attachment()], actor=actor, mid='queued-B')
    await prepare_native(runner, events[-1])
    os.utime(snapshot, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    import_export(runner, waiting, tmp_path)
    assert grant_verified_event_attachments(runner, waiting) == [str(snapshot)]
    final = snapshot.stat()
    snapshot.write_bytes(b'Z' * len(body['bytes']))
    os.utime(snapshot, ns=(final.st_atime_ns, final.st_mtime_ns))
    assert grant_verified_event_attachments(runner, waiting) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('attack', ['sender', 'group', 'path', 'bytes', 'identity', 'expired', 'profile',
                                  'source_profile', 'scope', 'request', 'receipt'])
async def test_forged_or_stale_reference_cannot_import(rig, tmp_path, monkeypatch, attack):
    runner, adapter, body, events = rig
    await ingest(adapter, [attachment()])
    event = restore_native(await prepare_native(runner, events[-1]), runner)
    if attack == 'sender':
        event.source.user_id = 'other'
    elif attack == 'group':
        event.source.chat_id = 'other'
    elif attack == 'path':
        event.media_urls[0] = str(tmp_path / 'forged.txt')
        Path(event.media_urls[0]).write_bytes(body['bytes'])
    elif attack == 'bytes':
        Path(event.media_urls[0]).write_bytes(b'changed')
    elif attack == 'identity':
        path = Path(event.media_urls[0])
        data = path.read_bytes()
        path.unlink()
        path.write_bytes(data)
    elif attack == 'expired':
        expired = time.time() + 1000
        monkeypatch.setattr('gateway.inbound_attachment_proofs.time.time', lambda: expired)
    elif attack == 'profile':
        monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'other'))
    elif attack == 'source_profile':
        event.source.profile = 'other'
    elif attack == 'scope':
        event.source.scope_id = 'other'
    elif attack == 'request':
        event.message_id = 'another-request'
    elif attack == 'receipt':
        event._inbound_attachment_receipts = [('f' * 32, 0)]
    assert grant_verified_event_attachments(runner, event) == []


@pytest.mark.asyncio
async def test_other_author_nearby_image_visual_only_and_forged_transcript_file(rig):
    runner, adapter, body, events = rig
    body['bytes'] = PNG
    data = {'group_openid': 'group', 'attachments': [attachment('image/png', 'p.png')]}
    await adapter._handle_plain_group_message(data, 'quiet', '', {'member_openid': 'other'}, '')
    await ingest(adapter, [], mid='followup')
    event = events[-1]
    sid = runner.session_store.get_or_create_session(event.source).session_id
    await prepare_turn_history(AsyncSessionStore(runner.session_store), sid, event)
    assert event.media_urls  # Shared visual context remains intact.
    assert eligible_paths(event) == []
    assert grant_verified_event_attachments(runner, event) == []


@pytest.mark.asyncio
async def test_no_url_and_oversize_are_unavailable(rig):
    _runner, adapter, body, events = rig
    result = await adapter._process_attachments([{'content_type': 'file', 'filename': 'missing.txt'}])
    assert not result['image_urls']
    assert '未提供下载 URL' in result['attachment_info']
    body['bytes'] = b'x' * (32 * 1024 * 1024 + 1)
    await ingest(adapter, [attachment()])
    assert events[-1].media_urls == []


@pytest.mark.asyncio
async def test_hardlink_denied_after_native_restore(rig, tmp_path):
    runner, adapter, _body, events = rig
    await ingest(adapter, [attachment()])
    event = restore_native(await prepare_native(runner, events[-1]), runner)
    os.link(event.media_urls[0], tmp_path / 'alias')
    assert grant_verified_event_attachments(runner, event) == []

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['file', 'image/png'])
@pytest.mark.parametrize('actor', ['guest', 'admin'])
async def test_same_author_quiet_attachment_reconstructed_twice(rig, tmp_path, kind, actor):
    runner, adapter, body, events = rig
    if kind.startswith('image/'):
        body['bytes'] = PNG
    data = {'group_openid': 'group', 'attachments': [attachment(kind, 'fixture.png' if kind.startswith('image/') else 'fixture.txt')]}
    await adapter._handle_plain_group_message(data, 'quiet', '', {'member_openid': actor}, '')
    await ingest(adapter, [], actor=actor, mid='followup')
    event = events[-1]
    sid = runner.session_store.get_or_create_session(event.source).session_id
    await prepare_turn_history(AsyncSessionStore(runner.session_store), sid, event)
    original = eligible_paths(event)
    assert len(original) == 1
    event = restore_native(await prepare_native(runner, event), runner)
    final = eligible_paths(event)
    assert len(final) == 1 and final != original
    import_export(runner, event, tmp_path)


@pytest.mark.asyncio
async def test_forged_sender_metadata_does_not_promote_other_author_file(rig):
    runner, adapter, _body, events = rig
    data = {'group_openid': 'group', 'attachments': [attachment()]}
    await adapter._handle_plain_group_message(data, 'quiet', '', {'member_openid': 'other'}, '')
    await ingest(adapter, [], mid='followup')
    event = events[-1]
    sid = runner.session_store.get_or_create_session(event.source).session_id
    rows = runner.session_store.load_transcript(sid, repair_alternation=False)
    rows[0]['display_metadata']['qq_sender'] = {'user_id': 'guest', 'message_id': 'quiet'}
    rows[0]['display_metadata']['qq_context_attachments'] = [{'path': 'C:/Windows/win.ini',
                                                            'sha256': 'a' * 64, 'status': 'cached'}]
    from plugins.platforms.qqbot.group_attachments import attach_own_pending
    await attach_own_pending(rows, event)
    assert not event.media_urls
    assert grant_verified_event_attachments(runner, event) == []


@pytest.mark.asyncio
async def test_real_queued_ingress_restores_and_imports_before_agent(rig, tmp_path, monkeypatch):
    runner, adapter, _body, _events = rig
    runner._wire_adapter_handlers(adapter)
    assert Path(runner.session_authority.db.db_path).resolve().parent == Path(os.environ['HERMES_HOME']).resolve(), (
        runner.session_authority.db.db_path, runner._native_transport_homes)
    # Restore the actual framework intake method that the capture fixture replaced.
    monkeypatch.setattr(adapter, 'handle_message', type(adapter).handle_message.__get__(adapter))
    completed = []
    from gateway.session_results import execution_result
    from tests.waiting import async_wait_until
    async def turn(event, *_args, **_kwargs):
        assert 'native-inputs' in event.media_urls[0]
        completed.append(import_export(runner, event, tmp_path))
        execution_result.get()['result'] = {'final_response': 'ok', 'messages': []}
        return 'ok'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    async def send(*args, **kwargs):
        from gateway.platforms.base import SendResult
        return SendResult(success=True, message_id='mock-sent')
    monkeypatch.setattr(adapter, 'send', send)
    await ingest(adapter, [attachment()])
    await async_wait_until(lambda: bool(completed) and not adapter._background_tasks, timeout=20,
                           what='mock QQ native attachment turn')
    assert len(completed) == 1


@pytest.mark.asyncio
@pytest.mark.platforms('windows')
async def test_junction_parent_rejected_even_with_same_bytes_and_identity(rig):
    runner, adapter, _body, events = rig
    await ingest(adapter, [attachment()])
    event = restore_native(await prepare_native(runner, events[-1]), runner)
    parent = Path(event.media_urls[0]).parent
    moved = parent.with_name(parent.name + '-original')
    parent.rename(moved)
    result = await asyncio.to_thread(
        subprocess.run, ['cmd.exe', '/c', 'mklink', '/J', str(parent), str(moved)],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    try:
        assert grant_verified_event_attachments(runner, event) == []
    finally:
        parent.rmdir()


def test_unc_rejected_without_any_filesystem_probe(monkeypatch):
    from gateway.inbound_attachment_proofs import _identity
    def no_probe(*args, **kwargs):
        raise AssertionError('UNC must not be probed')
    monkeypatch.setattr(Path, 'lstat', no_probe)
    with pytest.raises(ValueError, match='local'):
        _identity('\\\\server\\share\\forged.txt')
