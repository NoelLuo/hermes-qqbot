"""Review regressions: queued chains, compression, slow text, and historical facts."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.run import GatewayRunner
from gateway.turn_context import TurnContext
from hermes_state import SessionDB
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, wire, make_event
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, emit, prepare, TAG


@pytest.mark.asyncio
@pytest.mark.parametrize('catalog_state', ['empty', 'broken'])
async def test_sent_facts_remain_available_when_the_catalog_is_unusable(expression_bot, wire, catalog_state):
    bot = expression_bot
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), TAG)
    catalog = Path(controller_for(bot).settings.catalog)
    catalog.write_text('{invalid' if catalog_state == 'broken' else json.dumps({'schema_version': 1, 'items': []}), encoding='utf-8')
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: bot
    event = make_event(bot, identifier='follow-up')
    notes = await runner._hmwa_adapter_turn_notes(event, 'session-one', bot._source_session_key(event.source), [])
    assert any('"outcome":"sent"' in note and 'affirmation' in note for note in notes)
    assert any('status=unavailable' in note for note in notes)


@pytest.mark.asyncio
async def test_reviewed_entries_after_twenty_are_not_silently_hidden(expression_bot, wire):
    bot = expression_bot
    path = Path(controller_for(bot).settings.catalog)
    data = json.loads(path.read_text(encoding='utf-8'))
    base = data['items'][0]
    data['items'] = [{**base, 'id': f'choice_{n:02d}'} for n in range(21)]
    path.write_text(json.dumps(data), encoding='utf-8')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        notes, _ = await emit(bot, make_event(bot), '[[qq_meme:choice_20]]')
    assert any('choice_20' in note for note in notes)
    assert [body['msg_type'] for _, body in requests] == [7]


@pytest.mark.asyncio
@pytest.mark.parametrize('reset', [False, True])
async def test_only_compression_continuations_inherit_expression_facts(expression_bot, wire, reset):
    bot = expression_bot
    ctrl = controller_for(bot)
    event = make_event(bot)
    key = bot._source_session_key(event.source)
    db = SessionDB(ctrl.home / 'state.db')
    route = {'session_key': key, 'chat_id': event.source.chat_id, 'chat_type': 'group'}
    db.create_session('parent', 'qqbot', **route)
    _, transport = wire
    try:
        # The run began in parent. Compression commits before the GIF is sent.
        await prepare(bot, event, session='parent')
        db.end_session('parent', 'compression')
        db.create_session('child', 'qqbot', parent_session_id='parent',
                          model_config={'_reset_from': 'parent'} if reset else {}, **route)
        async with httpx.AsyncClient(transport=transport) as client:
            bot._http_client = client
            await emit(bot, event, TAG, session='parent')
        _, notes = await prepare(bot, make_event(bot, identifier='after-split'), session='child')
        assert any('"outcome":"sent"' in note for note in notes) is not reset
    finally:
        db.close()


@pytest.mark.asyncio
async def test_slow_text_cannot_reserve_an_unlimited_late_gif(expression_bot):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, cooldown=0, recent=0, daily_limit=1)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def request(req):
        body = json.loads(req.content)
        requests.append(body)
        if (body.get('markdown') or {}).get('content') == 'A':
            entered.set()
            await release.wait()
        return httpx.Response(200, json={'id': f'mock-{len(requests)}'})

    async with asyncio.timeout(20), httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        first = asyncio.create_task(emit(bot, make_event(bot, identifier='A'), 'A\n' + TAG))
        try:
            await entered.wait()
            ctrl.clock = lambda: 1040  # elapsed logical time, no sleeps or timing races
            await emit(bot, make_event(bot, identifier='B'), 'B\n' + TAG)
        finally:
            release.set()
        await first
    assert sum(body['msg_type'] == 7 for body in requests) == 1
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM expressions WHERE outcome='sent'").fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize('first_text,second_text,types', [
    ('A\n' + TAG, 'B\n' + TAG, [2, 7, 2, 7]), (TAG, TAG, [7, 7]),
])
async def test_actual_queued_chain_keeps_both_events_and_expression_feedback(
        expression_bot, wire, first_text, second_text, types):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, cooldown=0, recent=0, probability=1)
    a, b = make_event(bot, identifier='A'), make_event(bot, identifier='B')
    key, notes = await prepare(bot, a)
    runner = object.__new__(GatewayRunner)
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._delivery_adapter_for = runner._intake_adapter_for = lambda source: bot
    runner._is_goal_continuation_event = lambda event: False
    runner._session_key_for_source = lambda source: key
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value=b.text)
    runner._pinned_channel_inputs = lambda key, prompt, source, **kwargs: (prompt, source)
    runner._persist_prompt_pins = runner._refresh_agent_cache_message_count = AsyncMock()
    runner._pending_turn_sidecar_notes = {}
    bot.send_typing = AsyncMock()
    ctx = TurnContext(source=a.source, session_id='session-one', session_key=key,
                      inbound_message_id='A', event_message_id='A', run_generation=1,
                      _run_still_current=lambda: True)
    ctx.inbound_event = a
    history = [{'role': 'user', 'content': a.text + '\n'.join(notes)},
               {'role': 'assistant', 'content': first_text}]

    async def model_boundary(**kwargs):
        fresh = runner._consume_pending_turn_sidecar_notes(key)
        assert any('"outcome":"sent"' in note for note in fresh)
        assert getattr(b, '_qq_expression_context', None) is not None
        ctrl.clock = lambda: 1001  # B finishes after A; zero cooldown is not a frozen-time tie.
        return {'final_response': second_text, 'messages': history, 'completed': True}

    runner._run_agent = model_boundary
    result = {'final_response': first_text, 'messages': history, 'completed': True}
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        final = await runner._run_agent_queued_followup(ctx, bot, b.text, b, result, result, None)
    assert final['already_sent'] is True
    with sqlite3.connect(ctrl.state.path) as db:
        outcomes = db.execute('SELECT meme_id,outcome,reason FROM expressions ORDER BY rowid').fetchall()
    assert [body['msg_type'] for _, body in requests] == types, outcomes
    assert all('qq_meme' not in json.dumps(body) for _, body in requests)
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM expressions WHERE outcome='sent'").fetchone() == (2,)
    assert not list((ctrl.home / 'cache/qqbot/meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
async def test_outer_queue_fallback_cannot_reuse_the_previous_events_snapshot(expression_bot, wire):
    bot, event = expression_bot, make_event(expression_bot)
    key, _ = await prepare(bot, event)
    await bot._extract_response_content(TAG, event, key, is_ephemeral_response=False)
    event._adapter_chained_response = True
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        extracted = await bot._extract_response_content(TAG, event, key, is_ephemeral_response=False)
        assert not extracted.text_content and not extracted.media_files
        await bot._deliver_attachments(event, extracted, {}, anything_sent=False, record_delivery=lambda result: None)
    assert not requests
    bot._upload_local_file.assert_not_awaited()
    assert not list((controller_for(bot).home / 'cache/qqbot/meme_snapshots').glob('*.gif'))
