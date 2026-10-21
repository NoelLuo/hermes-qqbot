"""Real registry/store -> native restore/admission -> base delivery, without QQ posts."""
import asyncio
import base64
import json
import os
from contextvars import copy_context
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from gateway.config import Platform
from gateway.guest_export_delivery import begin_export_turn, bridge_export_turn
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.request_security import policy_for_adapter
from gateway.session import SessionSource
from gateway.session_ingress import deliver_response, execute_admission, deliver_settled
from plugins.platforms.qqbot.adapter import QQAdapter
from tools.guest_workspace_interface import register
from tools.guest_workspace_store import save_browser_artifacts
from tools.registry import registry

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
EXTRA = {'role_policy': {'enabled': True}}
KEY = 'agent:main:qqbot:group:room'


def event(message_id='request-one', **changes):
    source = SessionSource(platform=Platform.QQBOT, chat_id='room', user_id='visitor', chat_type='group')
    return MessageEvent(text='make screenshot', message_id=message_id, source=replace(source, **changes))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: home)
    monkeypatch.setattr('gateway.guest_export_delivery.get_hermes_home', lambda: home)
    scope = registry.current_scope_key()
    previous = registry.snapshot_registration('guest_workspace', scope=scope)
    register(NS(register_tool=lambda **kw: registry.register(**kw, scope=scope)))
    current = registry.snapshot_registration('guest_workspace', scope=scope)
    adapter = QQAdapter.__new__(QQAdapter)
    BasePlatformAdapter.__init__(adapter, NS(extra=EXTRA, typing_indicator=False), Platform.QQBOT)
    adapter._get_human_delay = lambda: 0
    adapter._bounded_history_media_paths_for_session = AsyncMock(return_value=set())
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id='file-ack'))
    adapter._send_final_text = AsyncMock(side_effect=lambda *args: args[-1](SendResult(success=True)))
    adapter._notify_media_delivery_failure = AsyncMock()
    yield adapter, home
    registry.restore_registration('guest_workspace', current, previous, scope=scope)


def export_png(adapter, request):
    policy = policy_for_adapter(adapter, request.source)
    begin_export_turn(policy, request.source, request.message_id)
    with policy_scope(policy):
        saved = save_browser_artifacts({'shot.png': PNG}, policy)
        assert saved['success'], saved
        result = json.loads(registry.dispatch('guest_workspace', {'action': 'export', 'path': saved['files'][0]['path']}))
    assert result['success'] and result['delivery_status'] == 'pending_transport'
    return policy, result


@pytest.mark.asyncio
@pytest.mark.parametrize('model_media', [False, True])
@pytest.mark.parametrize('admin', [False, True])
async def test_real_registry_snapshot_is_actually_sent_once(setup, model_media, admin):
    adapter, _ = setup
    if admin:
        adapter.config.extra = {**EXTRA, 'group_allow_admin_from': ['owner']}
    request = event(user_id='owner') if admin else event()
    policy, exported = export_png(adapter, request)
    assert policy.is_admin is admin and policy.public_context
    incoming = Path(exported['file_path']).parents[2] / 'incoming.png'
    incoming.write_bytes(PNG)
    assert policy.grant_artifact(str(incoming))
    response = '页面摘要：公开资料的说明。\n\n- 重要内容仍应保留。\n已收到图片，文件已经发送。'
    if model_media:
        response += '\n' + exported['media'] + '\n' + exported['media']
    await deliver_response(adapter, request, KEY, response)
    adapter.send_document.assert_awaited_once()
    sent = adapter.send_document.call_args.kwargs['file_path']
    assert Path(sent).read_bytes() == PNG
    assert Path(sent) == Path(exported['file_path'])
    text = adapter._send_final_text.call_args.args[2]
    assert '已经发送' not in text and '已收到' not in text and '尝试' in text
    assert '页面摘要：公开资料的说明。' in text and '- 重要内容仍应保留。' in text
    await deliver_response(adapter, request, KEY, response)
    assert adapter.send_document.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'user_id': 'other'}, {'chat_id': 'elsewhere'}, {'profile': 'other'}])
async def test_other_identity_cannot_bridge_or_send(setup, change):
    adapter, _ = setup
    executed = event()
    _, result = export_png(adapter, executed)
    waiting = event(**change)
    policy_for_adapter(adapter, waiting.source)
    assert not bridge_export_turn(executed, waiting)
    await deliver_response(adapter, waiting, KEY, result['media'])
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_restored_source_handoff_and_durable_no_waiter_delivery(setup):
    adapter, _ = setup
    original = event()
    old_policy = policy_for_adapter(adapter, original.source)
    encoded = original.source.to_dict()
    encoded['is_bot'] = False
    payload = {'text': original.text, 'native_text_v1': {
        'source': encoded, 'timestamp': original.timestamp.isoformat(),
        'event': {'message_id': original.message_id}, 'route': KEY}}
    # JSON round-trip proves live privilege objects are not serialized.
    payload = json.loads(json.dumps(payload))
    row = {'payload': payload, 'admission_id': 'admission-1', 'request_id': original.message_id}
    ref = NS(session_id='session-1')
    runner = NS(config=NS(multiplex_profiles=False), _adapter_for_source=lambda source: adapter)
    exported = {}

    async def handle(restored):
        assert restored.source is not original.source
        policy, result = export_png(adapter, restored)
        assert policy is not old_policy
        exported.update(result)
        return '已发送，请查收。'

    runner._handle_message = handle
    authority = NS(runner=runner, sessions={ref.session_id: NS(source=original.source, route=KEY)},
                   native_waiters={'admission-1'}, pending_results={}, pending_deliveries={},
                   export_delivery_waiters={'admission-1': original})
    response = await execute_admission(authority, ref, row)
    assert original.source._request_security_policy is not old_policy
    await deliver_response(adapter, original, KEY, response)
    assert Path(adapter.send_document.call_args.kwargs['file_path']) == Path(exported['file_path'])
    adapter.send_document.reset_mock()
    # A recovered admission has no original source/waiter; its own executed policy owns delivery.
    authority.native_waiters.clear()
    authority.export_delivery_waiters.clear()
    await execute_admission(authority, ref, row)
    await deliver_settled(authority, 'admission-1')
    adapter.send_document.assert_awaited_once()
    assert Path(adapter.send_document.call_args.kwargs['file_path']).read_bytes() == PNG
    assert not authority.pending_deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['bytes', 'same-stat', 'replace', 'request', 'profile'])
async def test_changed_snapshot_or_request_is_never_sent(setup, monkeypatch, mutation):
    adapter, home = setup
    request = event()
    _, result = export_png(adapter, request)
    snapshot = Path(result['file_path'])
    if mutation in {'bytes', 'same-stat'}:
        info = snapshot.stat()
        snapshot.write_bytes(b'x' * info.st_size)
        if mutation == 'same-stat':
            os.utime(snapshot, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif mutation == 'replace':
        replacement = snapshot.with_name('replacement.png')
        replacement.write_bytes(PNG)
        os.replace(replacement, snapshot)
    elif mutation == 'request':
        request.message_id = 'another-request'
    else:
        monkeypatch.setattr('gateway.guest_export_delivery.get_hermes_home', lambda: home / 'another')
    if mutation in {'bytes', 'same-stat', 'replace'}:
        with pytest.raises(RuntimeError, match='file_attachment_delivery_failed'):
            await deliver_response(adapter, request, KEY, 'sent')
    else:
        await deliver_response(adapter, request, KEY, 'sent')
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_export_and_incoming_grants_do_not_auto_attach(setup):
    adapter, home = setup
    request = event()
    policy = policy_for_adapter(adapter, request.source)
    begin_export_turn(policy, request.source, request.message_id)
    incoming = home / 'input.png'
    incoming.write_bytes(PNG)
    assert policy.grant_artifact(str(incoming))
    with policy_scope(policy):
        result = json.loads(registry.dispatch('guest_workspace', {'action': 'export', 'path': 'absent.png'}))
    assert not result['success']
    await deliver_response(adapter, request, KEY, 'No export was produced.')
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_response_failure_cannot_leave_pending_delivery(setup, monkeypatch):
    from tools import guest_workspace_store
    adapter, _ = setup
    request = event()
    policy = policy_for_adapter(adapter, request.source)
    begin_export_turn(policy, request.source, request.message_id)
    with policy_scope(policy):
        saved = save_browser_artifacts({'shot.png': PNG}, policy)
        assert saved['success']
        monkeypatch.setattr(guest_workspace_store, 'MAX_OUTPUT_BYTES', 1)
        result = json.loads(registry.dispatch('guest_workspace', {
            'action': 'export', 'path': saved['files'][0]['path']}))
    assert not result['success'] and not policy._exports.records
    await deliver_response(adapter, request, KEY, 'Export failed.')
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_api_failure_is_not_masked_by_successful_text(setup):
    adapter, _ = setup
    request = event()
    export_png(adapter, request)
    adapter.send_document.return_value = SendResult(success=False, error='synthetic upload rejected')
    with pytest.raises(RuntimeError, match='file_attachment_delivery_failed'):
        await deliver_response(adapter, request, KEY, '已发送。')
    adapter._notify_media_delivery_failure.assert_awaited_once()
    adapter._send_final_text.assert_awaited_once()


def test_payload_restore_has_no_live_export_rights(setup):
    adapter, _ = setup
    request = event()
    policy, result = export_png(adapter, request)
    restored = RequestSecurityPolicy.from_payload(policy.to_payload())
    assert not restored._artifacts.allows(result['file_path'])
    assert not restored._exports.records


@pytest.mark.asyncio
async def test_reused_policy_does_not_replay_historical_export(setup):
    adapter, _ = setup
    request = event()
    policy, result = export_png(adapter, request)
    old_context = copy_context()
    request.message_id = 'next-request'
    begin_export_turn(policy, request.source, request.message_id)
    assert not policy._artifacts.allows(result['file_path'])
    await deliver_response(adapter, request, KEY, result['media'])
    adapter.send_document.assert_not_awaited()
    # An older tool thread must not add a grant to the newer request's generation.
    def late_export():
        with policy_scope(policy):
            return json.loads(registry.dispatch('guest_workspace', {'action': 'export', 'path': result['path']}))
    assert not old_context.run(late_export)['success']


@pytest.mark.asyncio
@pytest.mark.parametrize('model_response', ['已发送。', ''])
async def test_real_turn_runner_installs_current_request_export_context(setup, model_response):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from agent.request_security import current_policy
    adapter, _ = setup
    request = event()
    runner = NS(config=NS(platforms={Platform.QQBOT: NS(extra=EXTRA)}), session_authority=None)
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = TurnContext(source=request.source, event_message_id=request.message_id)
    exported = {}
    def execute():
        policy = current_policy()
        saved = save_browser_artifacts({'real-turn.png': PNG}, policy)
        assert saved['success']
        exported.update(json.loads(registry.dispatch('guest_workspace', {
            'action': 'export', 'path': saved['files'][0]['path']})))
        return {'final_response': model_response}
    turn._run_sync_scoped = execute
    result = turn.run_sync()
    assert exported['success']
    await deliver_response(adapter, request, KEY, result['final_response'])
    assert Path(adapter.send_document.call_args.kwargs['file_path']) == Path(exported['file_path'])


@pytest.mark.asyncio
async def test_settled_delivery_returns_file_error_not_text_ack(setup):
    adapter, _ = setup
    request = event()
    export_png(adapter, request)
    adapter.send_document.return_value = SendResult(success=False, error='synthetic failed upload')
    authority = NS(runner=NS(config=NS(multiplex_profiles=False)), native_waiters=set(),
                   pending_deliveries={'a': (adapter, request, KEY, '已发送', None)})
    result = await deliver_settled(authority, 'a')
    assert result.success is False and 'file_attachment_delivery_failed' in result.error
    assert not authority.pending_deliveries


@pytest.mark.asyncio
@pytest.mark.parametrize('admin', [False, True])
async def test_snapshot_changed_during_text_send_is_refused_before_file_api(setup, admin):
    adapter, _ = setup
    if admin:
        adapter.config.extra = {**EXTRA, 'group_allow_admin_from': ['owner']}
    request = event(user_id='owner') if admin else event()
    _, result = export_png(adapter, request)
    snapshot = Path(result['file_path'])
    def mutate(*args):
        info = snapshot.stat()
        snapshot.write_bytes(b'X' * info.st_size)
        os.utime(snapshot, ns=(info.st_atime_ns, info.st_mtime_ns))
        args[-1](SendResult(success=True))
    adapter._send_final_text.side_effect = mutate
    with pytest.raises(RuntimeError, match='file_attachment_delivery_failed'):
        await deliver_response(adapter, request, KEY, 'sent')
    adapter.send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_admission_waiter_receives_its_executed_export_policy(setup, monkeypatch):
    from gateway.session_ingress import admit_message
    adapter, _ = setup
    original = event()
    old_policy = policy_for_adapter(adapter, original.source)
    source = {**original.source.to_dict(), 'is_bot': False}
    row = {'admission_id': 'live', 'request_id': original.message_id, 'status': 'started',
           'payload': {'text': original.text, 'native_text_v1': {
               'source': source, 'timestamp': original.timestamp.isoformat(),
               'event': {'message_id': original.message_id}, 'route': KEY}}}
    ref = NS(session_id='session')
    ready = asyncio.Event()
    runner = NS(config=NS(multiplex_profiles=False), _adapter_for_source=lambda source: adapter)
    produced = {}
    async def handle(restored):
        _, result = export_png(adapter, restored)
        produced.update(result)
        return '已发送。'
    runner._handle_message = handle
    authority = NS(runner=runner, db=Mock(), sessions={'session': NS(source=original.source, route=KEY)},
                   native_waiters=set(), waiters={}, pending_results={}, pending_deliveries={})
    async def admit(request):
        assert request is original
        ready.set()
        return NS(status='queued', admission_id='live', ref=ref)
    authority.admit_native = admit
    monkeypatch.setattr('hermes_state_runtime.get_session_admission', lambda *a, **kw: row)
    waiting = asyncio.create_task(admit_message(authority, original))
    await asyncio.wait_for(ready.wait(), timeout=5)
    response = await execute_admission(authority, ref, row)
    authority.waiters['live'].set_result(response)
    response = await asyncio.wait_for(waiting, timeout=5)
    assert not authority.export_delivery_waiters
    assert original.source._request_security_policy is not old_policy
    delivered = await deliver_response(adapter, original, KEY, response)
    assert delivered.success is True
    assert Path(adapter.send_document.call_args.kwargs['file_path']) == Path(produced['file_path'])
