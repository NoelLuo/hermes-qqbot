"""Sole GIF replies are expressions, not frequency-limited decorations."""
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.platforms.base import ProcessingOutcome
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, make_event, wire
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, emit, prepare


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', ['qq_meme', 'qq_meme_optional'])
@pytest.mark.parametrize('gate', ['recent', 'cooldown', 'same_instant'])
async def test_sole_gif_bypasses_decoration_frequency_but_not_event_dedup(expression_bot, wire, marker, gate):
    adapter = expression_bot
    ctrl = controller_for(adapter)
    ctrl.settings = replace(ctrl.settings, recent=3 if gate == 'recent' else 0,
                            cooldown=180 if gate == 'cooldown' else 0)
    ctrl.draw = lambda: pytest.fail('Sole GIF must never draw')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        await emit(adapter, make_event(adapter), '[[qq_meme:yes]]')
        ctrl.clock = lambda: 1000 if gate == 'same_instant' else 1001
        event = make_event(adapter, identifier='second')
        notes, results = await emit(adapter, event, f' \n[[{marker}:yes]]\n ')
        await emit(adapter, event, f' \n[[{marker}:yes]]\n ')
    assert [body['msg_type'] for _, body in requests] == [7, 7]
    assert all(r.success for r in results)
    assert all(not {'content', 'markdown', 'caption'} & body.keys() for _, body in requests)
    assert any('纯GIF状态=available' in note for note in notes)
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT outcome FROM expressions ORDER BY rowid').fetchall() == [('sent',), ('sent',)]
    assert not list((ctrl.home / 'cache/qqbot/meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['disabled', 'daily_limit', 'busy'])
async def test_pure_reply_keeps_hard_off_quota_and_inflight_guard(expression_bot, wire, gate):
    adapter = expression_bot
    ctrl = controller_for(adapter)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        if gate == 'disabled':
            ctrl.settings = replace(ctrl.settings, probability=0)
        elif gate == 'daily_limit':
            await emit(adapter, make_event(adapter), '[[qq_meme:yes]]')
            ctrl.settings = replace(ctrl.settings, daily_limit=1, recent=0, cooldown=0)
            ctrl.clock = lambda: 1001
        else:
            assert ctrl.state.claim('inflight', 'group:meme-test-group', 1000,
                                    probability=1, cooldown=0, daily_limit=0)
        before = len(requests)
        await emit(adapter, make_event(adapter, identifier='blocked'), '[[qq_meme_optional:yes]]')
        assert len(requests) == before


@pytest.mark.asyncio
async def test_recent_candidate_remains_available_for_pure_reply_only(expression_bot, wire):
    adapter = expression_bot
    ctrl = controller_for(adapter)
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        await emit(adapter, make_event(adapter), '[[qq_meme:yes]]')
    ctrl.clock = lambda: 2000
    _, notes = await prepare(adapter, make_event(adapter, identifier='next'))
    note = next(n for n in notes if n.startswith('[本轮QQ表情可用性]'))
    assert 'status=no_candidates' in note
    assert json.loads(note.split('\n候选=', 1)[1]) == []
    assert '纯GIF状态=available' in note
    pure = json.loads(note.split('\n纯GIF候选=', 1)[1].split('\n候选=', 1)[0])
    assert [entry['id'] for entry in pure] == ['yes']


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['background', 'queued'])
async def test_consecutive_pure_reply_real_delivery_paths_have_no_empty_notice(expression_bot, wire, path):
    adapter = expression_bot
    adapter._message_handler = AsyncMock(return_value='[[qq_meme_optional:yes]]')
    adapter._run_processing_hook = AsyncMock()
    adapter._start_typing_refresh = lambda *a, **kw: None
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        for identifier in ('first', 'second'):
            event = make_event(adapter, identifier=identifier)
            key, _ = await prepare(adapter, event)
            if path == 'background':
                await adapter._process_message_background(event, key)
            else:
                assert await adapter.deliver_queued_response('[[qq_meme_optional:yes]]', event, key)
    assert [body['msg_type'] for _, body in requests] == [7, 7]
    if path == 'background':
        outcomes = [call.args[-1] for call in adapter._run_processing_hook.call_args_list
                    if call.args[0] == 'on_processing_complete']
        assert outcomes == [ProcessingOutcome.SUCCESS, ProcessingOutcome.SUCCESS]
