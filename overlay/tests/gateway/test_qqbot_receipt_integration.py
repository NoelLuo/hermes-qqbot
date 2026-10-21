"""Send -> durable source lookup -> controlled recall, through real QQ adapter methods."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner  # bootstrap runtime before real-home protection
from plugins.platforms.qqbot.adapter import API_BASE, QQAdapter
from plugins.platforms.qqbot.delivery_binding import bind_delivery, turn_context
from plugins.platforms.qqbot.delivery_receipts import ReceiptScopeError
from plugins.platforms.qqbot.receipt_adapter import receipt_journal


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={'app_id': 'test-account'}))
    adapter._auth_headers = AsyncMock(return_value={})
    adapter._ensure_connected = AsyncMock(return_value=True)
    adapter._guess_chat_type = lambda _: 'group'
    return adapter


@pytest.mark.asyncio
async def test_final_text_persists_all_parts_and_recalls_after_adapter_restart(bot):
    calls = []

    async def request(req):
        calls.append(req)
        if req.method == 'DELETE':
            return httpx.Response(200, content=b'')
        return httpx.Response(200, json={'id': f'platform/{len(calls)}:opaque'})

    source = bot.build_source(chat_id='group', user_id='member', chat_type='group')
    event = MessageEvent(source=source, message_id='inbound-real-id', text='question',
                         reply_to_message_id='quoted-distinct-id')
    key = bot._source_session_key(source)
    bot.truncate_message = lambda *_: ['part-one', 'part-two']
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result, owner = await bot.send_final_ledgered(event, key, 'reply', {}, reply_to=event.message_id)
        assert result.success and owner is bot
        assert result.message_id == 'platform/2:opaque'
        assert result.continuation_message_ids == ('platform/1:opaque',)
        rows = receipt_journal(bot, API_BASE).lookup_source(chat_id='group', source_session_key=key,
                                                          source_message_id=event.message_id)
        assert [r.message_id for r in rows] == ['platform/1:opaque', 'platform/2:opaque']
        assert all(r.source_quote_id == 'quoted-distinct-id' for r in rows)
        restarted = QQAdapter(PlatformConfig(enabled=True, extra={'app_id': 'test-account'}))
        restarted._http_client = client
        restarted._auth_headers = AsyncMock(return_value={})
        recalled = await restarted.recall_own_group_message(logical_id=rows[0].logical_id,
            group_id='group', message_id=rows[0].message_id)
        assert recalled.recall_status == 'api_acknowledged'
        assert calls[-1].url.raw_path.endswith(b'/platform%2F1%3Aopaque')
        persisted = receipt_journal(bot, API_BASE).lookup(rows[0].logical_id, chat_id='group')
        assert persisted[0].recall_status == 'api_acknowledged'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected', [('timeout', 'unknown'), ('reject', 'error'), ('missing', 'unknown')])
async def test_partial_failure_or_missing_id_retains_first_part_without_resend(bot, mode, expected):
    posts = []

    async def request(req):
        posts.append(req)
        if len(posts) == 1:
            return httpx.Response(200, json={'id': 'real-first'})
        if mode == 'timeout':
            raise httpx.ReadTimeout('private payload must not enter journal')
        if mode == 'reject':
            return httpx.Response(403, json={'code': 403, 'message': 'private payload'})
        return httpx.Response(200, json={})

    bot.truncate_message = lambda *_: ['one', 'two']
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result = await bot.send('group', 'full reply')
    assert len(posts) == 2
    assert result.continuation_message_ids == ('real-first',)
    logical = result.raw_response.get('qq_logical_id') or result.raw_response['qq_receipt']['logical_id']
    rows = receipt_journal(bot, API_BASE).lookup(logical, chat_id='group')
    assert [r.delivery_status for r in rows] == ['known', expected]
    assert rows[0].message_id == 'real-first' and rows[1].message_id is None
    assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,expected', [
    (200, b'{}', 'api_acknowledged'), (200, b'{"code":42}', 'api_error'),
    (403, b'', 'api_error'), (200, b'not-json', 'transport_unknown'),
])
async def test_adapter_recall_normalization_and_scope(bot, status, body, expected):
    journal = receipt_journal(bot, API_BASE)
    logical = journal.begin_send(chat_id='group')
    journal.append_part(logical, chat_id='group', returned_message_id='real-owned')
    calls = []

    async def request(req):
        calls.append(req)
        return httpx.Response(status, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        with pytest.raises(ReceiptScopeError):
            await bot.recall_own_group_message(logical_id=logical, group_id='other', message_id='real-owned')
        with pytest.raises(ReceiptScopeError):
            await bot.recall_own_group_message(logical_id=logical, group_id='group', message_id='inbound-id')
        assert not calls
        row = await bot.recall_own_group_message(logical_id=logical, group_id='group', message_id='real-owned')
        assert row.recall_status == expected and len(calls) == 1


@pytest.mark.asyncio
async def test_text_and_media_posts_share_turn_link_without_overwriting(bot):
    source = bot.build_source(chat_id='group', user_id='member', chat_type='group')
    event = MessageEvent(source=source, message_id='source-id', text='question')
    key = bot._source_session_key(source)
    bot._api_request = AsyncMock(side_effect=[{'id': 'text'}, {'id': 'media'}])
    with bind_delivery('group', turn_context(event, key)):
        await bot._post_message('/v2/groups/group/messages', {'content': 'text'})
    with bind_delivery('group', turn_context(event, key)):
        await bot._post_message('/v2/groups/group/messages', {'msg_type': 7, 'media': {'file_info': 'private'}})
    rows = receipt_journal(bot, API_BASE).lookup_source(chat_id='group', source_session_key=key,
                                                      source_message_id='source-id')
    assert [r.message_id for r in rows] == ['text', 'media']
    assert [r.part_index for r in rows] == [0, 1]
    assert len({r.logical_id for r in rows}) == 1


@pytest.mark.asyncio
async def test_cancelled_post_leaves_unknown_before_network(bot):
    begun = asyncio.Event()

    async def request(*_):
        begun.set()
        await asyncio.Event().wait()

    bot._api_request = request
    source = bot.build_source(chat_id='group', user_id='member', chat_type='group')
    event = MessageEvent(source=source, message_id='cancel-source', text='question')
    key = bot._source_session_key(source)
    with bind_delivery('group', turn_context(event, key)):
        task = asyncio.create_task(bot._post_message('/v2/groups/group/messages', {'content': 'private'}))
        await begun.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    rows = receipt_journal(bot, API_BASE).lookup_source(chat_id='group', source_session_key=key,
                                                      source_message_id=event.message_id)
    assert len(rows) == 1 and rows[0].delivery_status == 'unknown' and rows[0].message_id is None
