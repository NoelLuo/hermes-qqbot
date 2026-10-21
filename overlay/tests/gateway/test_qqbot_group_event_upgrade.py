"""Actual QQ dispatch entry: promotion, opposite order, concurrency and recovery."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.run import GatewayRunner  # bootstrap before real-home guard
from gateway.session import AsyncSessionStore, SessionStore
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.group_history import is_observed, prepare_turn_history


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig(group_sessions_per_user=False))
    admitted = []

    def make_adapter(active_store):
        bot = QQAdapter(PlatformConfig(enabled=True, extra={
            'group_policy': 'open', 'bot_member_openid': 'group-bot',
            'group_reply_without_mention': ['group'],
            'group_reply_gate': {'cooldown_seconds': 0, 'decision_interval_seconds': 0}}))
        bot.set_session_store(active_store)
        bot.set_authorization_check(lambda user, *_: user != 'blocked')
        bot._group_reply_gate.should_reply = AsyncMock(return_value=False)

        async def dispatch(event):
            sid = active_store.get_or_create_session(event.source).session_id
            history = await prepare_turn_history(AsyncSessionStore(active_store), sid, event)
            assert 'UNIQUE_EVENT_BODY' not in (event.channel_context or '')
            admitted.append((event, history))
            active_store.append_to_transcript(sid, {
                'role': 'user', 'content': event.text, 'message_id': event.message_id})
            active_store.append_to_transcript(sid, {'role': 'assistant', 'content': 'answer'})
        bot.handle_message = dispatch
        return bot

    bot = make_adapter(store)
    yield bot, store, admitted, make_adapter
    store._db.close()


def payload(user='member'):
    return {'id': 'opaque-platform-message', 'group_openid': 'group',
            'author': {'member_openid': user}, 'content': 'UNIQUE_EVENT_BODY',
            'message_reference': {'message_id': 'QUOTE-OTHER-ID'},
            'timestamp': '2026-10-18T05:00:00Z'}


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [
    ['GROUP_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE'],
    ['GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'GROUP_AT_MESSAGE_CREATE'],
])
async def test_one_addressed_request_for_both_delivery_orders(rig, order):
    bot, store, admitted, _ = rig
    for kind in order:
        await bot._on_message(kind, payload())
    assert len(admitted) == 1
    assert admitted[0][0].reply_to_message_id == 'QUOTE-OTHER-ID'
    source = admitted[0][0].source
    rows = store.load_transcript(store.get_or_create_session(source).session_id, repair_alternation=False)
    assert sum(row.get('role') == 'user' and not is_observed(row) for row in rows) == 1
    # Original passive record remains an audit row; it is not a second request.
    assert sum(is_observed(row) for row in rows) == (order[0] == 'GROUP_MESSAGE_CREATE')


@pytest.mark.asyncio
async def test_concurrent_delivery_and_restart_do_not_readmit(rig):
    bot, store, admitted, make_adapter = rig
    await asyncio.gather(bot._on_message('GROUP_MESSAGE_CREATE', payload()),
                         bot._on_message('GROUP_AT_MESSAGE_CREATE', payload()),
                         bot._on_message('GROUP_AT_MESSAGE_CREATE', payload()))
    assert len(admitted) == 1
    restarted = SessionStore(store.sessions_dir, store.config)
    try:
        other = make_adapter(restarted)
        await other._on_message('GROUP_AT_MESSAGE_CREATE', payload())
        await other._on_message('GROUP_MESSAGE_CREATE', payload())
        assert len(admitted) == 1
    finally:
        restarted._db.close()


@pytest.mark.asyncio
async def test_passive_before_restart_can_be_promoted(rig):
    bot, store, admitted, make_adapter = rig
    await bot._on_message('GROUP_MESSAGE_CREATE', payload())
    assert not admitted
    restarted = SessionStore(store.sessions_dir, store.config)
    try:
        await make_adapter(restarted)._on_message('GROUP_AT_MESSAGE_CREATE', payload())
        assert len(admitted) == 1
    finally:
        restarted._db.close()


@pytest.mark.asyncio
async def test_autonomous_admission_consumes_later_addressed_delivery(rig):
    bot, _, admitted, _ = rig
    bot._group_reply_gate.should_reply.return_value = True
    await bot._on_message('GROUP_MESSAGE_CREATE', payload())
    await bot._on_message('GROUP_AT_MESSAGE_CREATE', payload())
    assert len(admitted) == 1
    assert admitted[0][0].allow_gateway_control is False


@pytest.mark.asyncio
async def test_unauthorized_event_does_not_claim_id(rig):
    bot, store, admitted, _ = rig
    await bot._on_message('GROUP_AT_MESSAGE_CREATE', payload('blocked'))
    assert not admitted and not store._entries
    await bot._on_message('GROUP_AT_MESSAGE_CREATE', payload())
    assert len(admitted) == 1
