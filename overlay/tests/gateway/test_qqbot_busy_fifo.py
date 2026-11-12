"""QQ busy inputs are independent FIFO turns, never another actor's live input.

Exercise the real adapter -> runner -> SQLite admission -> restored turn path;
only model execution and outbound network delivery are replaced.
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.request_security import policy_for_source
from gateway.run import GatewayRunner
from gateway.run import _AGENT_PENDING_SENTINEL
from gateway.session_authority import initialize_session_authority
from gateway.session_results import execution_result
from hermes_constants import get_hermes_home
from hermes_state_runtime import list_session_admissions
from plugins.platforms.qqbot.adapter import QQAdapter
from tests.waiting import async_wait_until


def make_pair(monkeypatch, *, role_policy=True):
    monkeypatch.setenv('QQ_ALLOW_ALL_USERS', 'true')
    config = PlatformConfig(enabled=True, extra={
        'app_id': 'synthetic-app', 'client_secret': 'synthetic-secret',
        'group_policy': 'open', 'dm_policy': 'open',
        'group_sessions_per_user': False,
        'role_policy': {'enabled': role_policy},
        'group_allow_admin_from': ['admin'], 'allow_admin_from': ['admin'],
    })
    adapter = QQAdapter(config)
    runner = GatewayRunner(GatewayConfig(platforms={Platform.QQBOT: config}, group_sessions_per_user=False))
    adapter.gateway_runner = runner
    runner.adapters = {Platform.QQBOT: adapter}
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='synthetic-sent'))
    adapter.send_typing = AsyncMock()
    return runner, adapter


def message(adapter, text, user='guest', *, kind=MessageType.TEXT, chat_type='group'):
    return MessageEvent(text=text, message_type=kind, message_id=text,
                        source=adapter.build_source(chat_id='room', chat_type=chat_type, user_id=user))


def rows(authority):
    return [row for sid in authority.sessions
            for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['queue', 'interrupt', 'steer'])
@pytest.mark.parametrize('chat_type', ['group', 'dm'])
async def test_real_busy_admission_drains_fifo_with_each_request_identity(monkeypatch, mode, chat_type):
    runner, adapter = make_pair(monkeypatch)
    runner._busy_input_mode = mode
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='qq-fifo')
    assert Path(authority.db.db_path).parent == get_hermes_home()
    runner._wire_adapter_handlers(adapter, busy_text_mode='queue')
    started, finish = asyncio.Event(), asyncio.Event()
    agent = Mock()
    executed = []

    async def turn(event, source, quick_key, run_generation):
        policy = policy_for_source(runner, source)
        executed.append((event.text, policy.subject_id, policy.is_admin, event.message_id))
        runner._session_state(quick_key).turn.agent = agent
        if event.text == 'first':
            started.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'reply-' + event.text, 'messages': []}
        return 'reply-' + event.text

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    first = message(adapter, 'first', 'admin', chat_type=chat_type)
    key = adapter._event_session_key(first)
    # Group members differ; a DM has one trusted sender. Same-author follow-ups
    # must also remain separate turns, not be debounced into one request.
    users = ['guest', 'other', 'other', 'admin'] if chat_type == 'group' else ['admin'] * 4
    followups = [message(adapter, text, user, chat_type=chat_type)
                 for text, user in zip(['second', 'third', 'fourth', 'fifth'], users)]
    assert all(adapter._event_session_key(event) == key for event in followups)
    try:
        await adapter.handle_message(first)
        await asyncio.wait_for(started.wait(), 10)
        for event in followups:
            await asyncio.wait_for(adapter.handle_message(event), 10)
            assert event._gateway_accepted is True
        assert [row['request_id'] for row in rows(authority)] == ['first', 'second', 'third', 'fourth', 'fifth']
        assert [row['status'] for row in rows(authority)] == ['started'] + ['queued'] * 4
        assert executed == [('first', 'admin', True, 'first')]
        assert not adapter._pending_messages, 'durable turns must not leak into the legacy slot'
        agent.interrupt.assert_not_called()
        agent.steer.assert_not_called()
        adapter.send.assert_not_awaited()
        finish.set()
        await async_wait_until(lambda: len(executed) == 5 and not adapter._background_tasks,
                               timeout=20, what='all QQ FIFO replies')
        assert executed == [('first', 'admin', True, 'first')] + [
            (event.text, user, user == 'admin', event.message_id) for event, user in zip(followups, users)]
        assert [row['status'] for row in rows(authority)] == ['terminal'] * 5
        assert [call.args[1] if len(call.args) > 1 else call.kwargs['content']
                for call in adapter.send.call_args_list] == ['reply-' + text for text in
                                                            ['first', 'second', 'third', 'fourth', 'fifth']]
    finally:
        finish.set()
        tasks = list(adapter._background_tasks) + [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['runner_busy', 'running_fast_path', 'message_handler'])
@pytest.mark.parametrize('role_policy', [True, False])
@pytest.mark.parametrize('starting', [False, True])
async def test_legacy_entrypoints_queue_without_intercepts_or_cross_sender_media_merge(monkeypatch, entry, role_policy, starting):
    runner, adapter = make_pair(monkeypatch, role_policy=role_policy)
    runner._wire_adapter_handlers(adapter)
    first = message(adapter, 'first', 'admin')
    key = runner._session_key_for_source(first.source)
    state = runner._session_state(key)
    state.turn.agent = _AGENT_PENDING_SENTINEL if starting else Mock()
    state.turn.event = first
    runner._hm_pending_reply_intercepts = AsyncMock(side_effect=AssertionError('must not consume live reply'))
    runner._route_plaintext_approval_while_busy = AsyncMock(side_effect=AssertionError('must not answer live approval'))
    incoming = [message(adapter, 'photo-a', 'guest', kind=MessageType.PHOTO),
                message(adapter, 'text-b', 'other'), message(adapter, 'photo-c', 'guest', kind=MessageType.PHOTO),
                message(adapter, 'text-d', 'admin')]
    for event in incoming:
        if entry == 'runner_busy':
            assert await runner._handle_active_session_busy_message(event, key)
        elif entry == 'running_fast_path':
            assert await runner._hm_handle_running_session_message(event, event.source, key) is None
        else:
            assert await runner._handle_message(event) is None
    actual = []
    while pending := adapter.get_pending_message(key):
        actual.append(runner._promote_queued_event(key, adapter, pending))
    assert actual == incoming
    assert [event.source.user_id for event in actual] == ['guest', 'other', 'guest', 'admin']
    if not starting:
        state.turn.agent.interrupt.assert_not_called()
        state.turn.agent.steer.assert_not_called()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['stop', 'new', 'reset', 'steer', 'approve', 'deny'])
async def test_guest_control_commands_still_cannot_mutate_busy_turn(monkeypatch, command):
    runner, adapter = make_pair(monkeypatch)
    runner._wire_adapter_handlers(adapter)
    event = message(adapter, '/' + command)
    key = adapter._event_session_key(event)
    guard = asyncio.Event()
    adapter._active_sessions[key] = guard
    adapter.cancel_session_processing = AsyncMock()
    adapter._drain_pending_after_session_command = AsyncMock()
    await adapter._handle_message_while_active(event, key)
    assert adapter._active_sessions[key] is guard
    assert not adapter._pending_messages
    adapter.cancel_session_processing.assert_not_awaited()
    adapter._drain_pending_after_session_command.assert_not_awaited()
    assert '仅限管理员' in adapter.send.call_args.args[1]


@pytest.mark.asyncio
async def test_unauthorized_sender_is_not_queued(monkeypatch):
    runner, adapter = make_pair(monkeypatch)
    runner._wire_adapter_handlers(adapter)
    runner._is_user_authorized_for_source = Mock(return_value=False)
    event = message(adapter, 'blocked')
    key = adapter._event_session_key(event)
    assert await runner._handle_active_session_busy_message(event, key)
    assert not adapter._pending_messages
    assert not runner._overflow_queue(key)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['admin', 'other'])
async def test_authorized_explicit_steer_does_not_depend_on_running_owner(monkeypatch, owner):
    runner, adapter = make_pair(monkeypatch)
    runner._wire_adapter_handlers(adapter)
    event = message(adapter, '/steer next-task', 'admin')
    key = adapter._event_session_key(event)
    runner._session_state(key).turn.event = message(adapter, 'owner-task', owner)
    agent = runner._session_state(key).turn.agent = Mock()
    agent.steer.return_value = True
    reply = await runner._busy_steer_command(event, key, event.source)
    assert reply is None or isinstance(reply, str), 'must not leak an unawaited queue coroutine'
    assert adapter.get_pending_message(key) is None
    agent.steer.assert_called_once()
    assert 'next-task' in agent.steer.call_args.args[0]


@pytest.mark.asyncio
async def test_qq_callbacks_keep_group_authors_and_do_not_consume_live_clarification(monkeypatch):
    runner, adapter = make_pair(monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='qq-callback-fifo')
    runner._wire_adapter_handlers(adapter)
    started, finish = asyncio.Event(), asyncio.Event()
    executed = []
    agent = Mock()

    async def turn(event, source, quick_key, run_generation):
        policy = policy_for_source(runner, source)
        executed.append((event.message_id, source.user_id, policy.is_admin))
        runner._session_state(quick_key).turn.agent = agent
        if event.message_id == 'first':
            started.set()
            await finish.wait()
        execution_result.get()['result'] = {'final_response': 'answer-' + event.message_id, 'messages': []}
        return 'answer-' + event.message_id

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    # Public transport callback, including group-event dedup, labels and native provenance.
    async def receive(mid, sender, text):
        await adapter._on_message('GROUP_AT_MESSAGE_CREATE', {
            'id': mid, 'group_openid': 'room', 'author': {'member_openid': sender}, 'content': text,
        })

    try:
        await receive('first', 'admin', 'start')
        await asyncio.wait_for(started.wait(), 10)
        # A shared pending clarify must not turn another sender's "yes" into
        # the owner's tool answer; queued admission execution also skips it.
        from tools import clarify_gateway
        lookup = Mock(return_value={'prompt': 'owner question'})
        monkeypatch.setattr(clarify_gateway, 'get_pending_for_session', lookup)
        await receive('second', 'guest', 'yes')
        await receive('third', 'other', 'next')
        await receive('second', 'guest', 'yes')  # duplicate transport event
        assert [row['request_id'] for row in rows(authority)] == ['first', 'second', 'third']
        assert executed == [('first', 'admin', True)]
        lookup.assert_not_called()
        agent.interrupt.assert_not_called()
        agent.steer.assert_not_called()
        finish.set()
        await async_wait_until(lambda: len(executed) == 3 and not adapter._background_tasks,
                               timeout=20, what='callback FIFO drain')
        assert executed == [('first', 'admin', True), ('second', 'guest', False), ('third', 'other', False)]
        assert [row['status'] for row in rows(authority)] == ['terminal'] * 3
    finally:
        finish.set()
        tasks = list(adapter._background_tasks) + [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
