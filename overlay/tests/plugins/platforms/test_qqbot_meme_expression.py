"""The assistant chooses; the real QQ boundary sends and supplies durable feedback."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
import pytest

from gateway.platforms.base import ProcessingOutcome, SendResult
from plugins.platforms.qqbot.meme_expression import capability_note, parse_expression
from plugins.platforms.qqbot.meme_expression_delivery import engine_for
from plugins.platforms.qqbot.memes import controller_for
from plugins.platforms.qqbot.receipt_adapter import receipt_journal
from plugins.platforms.qqbot.adapter import API_BASE, QQAdapter
from tests.plugins.platforms.test_qqbot_memes import bot, wire, make_event

TAG = '[[qq_meme:yes]]'


@pytest.mark.parametrize('text', [TAG, 'Hermes选了这张。\n' + TAG, 'hello\r\n' + TAG + '\r\n'])
def test_final_standalone_marker_is_an_explicit_choice(text):
    result = parse_expression(text)
    assert result.marked and result.meme_id == 'yes' and not result.optional and not result.error
    assert 'qq_meme' not in result.text


@pytest.mark.parametrize('text', ['`' + TAG + '`', '> ' + TAG, '    ' + TAG,
    '```text\n' + TAG + '\n```', '~~~\n' + TAG + '\n~~~', '```\n' + TAG,
    '``code\n' + TAG + '\ncode``'])
def test_code_and_quotes_are_not_expression_actions(text):
    result = parse_expression(text)
    assert not result.marked and result.text == text


@pytest.mark.parametrize('text', ['hi ' + TAG, TAG + '\nmore text', TAG + '\n' + TAG,
    '[[qq_meme:../private.gif]]', '[[qq_meme:yes', '[[QQ_MEME:yes]]', '[[qq_meme:yes target="other"]]'])
def test_malformed_or_multiple_control_is_removed_but_never_authorized(text):
    result = parse_expression(text)
    assert result.marked and result.error and result.meme_id is None
    assert 'qq_meme' not in result.text.lower()


def test_optional_choice_is_distinct_from_an_explicit_send():
    result = parse_expression('fine\n[[qq_meme_optional:yes]]')
    assert result.meme_id == 'yes' and result.optional


@pytest.mark.parametrize('text', ['判断过了。\n[[qq_meme_none]]', '[[qq_meme_none]]'])
def test_none_verdict_is_a_judgement_that_requests_no_image(text):
    result = parse_expression(text)
    assert result.judged and not result.marked and result.meme_id is None and not result.error
    assert 'qq_meme_none' not in result.text


@pytest.mark.parametrize('text', ['[[qq_meme_none]]\n后面还有正文', '[[qq_meme_none]]\n[[qq_meme:yes]]'])
def test_none_verdict_must_be_the_single_final_line(text):
    result = parse_expression(text)
    assert result.judged and result.error == 'invalid_marker'


def test_missing_verdict_is_not_a_judgement():
    result = parse_expression('普通回复，没有控制标记。')
    assert not result.judged and not result.marked


@pytest.fixture
def expression_bot(bot):
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, mode='expression', probability=.2)
    ctrl.selector = AsyncMock(side_effect=AssertionError('Background selector must not run'))
    return bot


async def prepare(bot, event, history=(), session='session-one'):
    key = bot._source_session_key(event.source)
    notes = await bot.prepare_turn_notes(event, session, key, list(history))
    return key, notes


async def emit(bot, event, text, *, history=(), session='session-one'):
    key, notes = await prepare(bot, event, history, session)
    extracted = await bot._extract_response_content(text, event, key, is_ephemeral_response=False)
    results = []
    if extracted.text_content:
        result, _ = await bot.send_final_ledgered(event, key, extracted.text_content, {}, reply_to=event.message_id)
        results.append(result)
    await bot._deliver_attachments(event, extracted, {}, anything_sent=bool(results), record_delivery=results.append)
    return notes, results


@pytest.mark.asyncio
@pytest.mark.parametrize('text,types', [(TAG, [7]), ('这张给管理员。\n' + TAG, [2, 7])])
async def test_explicit_choices_bypass_rng_and_use_separate_native_gif(expression_bot, wire, text, types):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.draw = lambda: pytest.fail('An explicit choice must not be randomly dropped')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        _, results = await emit(bot, make_event(bot), text)
    assert [body['msg_type'] for _, body in requests] == types
    assert results and all(r.success for r in results)
    gif = requests[-1][1]
    assert not {'content', 'markdown', 'caption'} & gif.keys()
    ctrl.selector.assert_not_awaited()
    assert not list((ctrl.home / 'cache/qqbot/meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
async def test_catalog_is_a_one_time_sidecar_and_known_send_survives_controller_recreation(expression_bot, wire):
    bot = expression_bot
    event = make_event(bot)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        notes, _ = await emit(bot, event, TAG)
    catalog_note = next(n for n in notes if n.startswith('[QQ表情能力/v1]'))
    assert 'affirmation' in catalog_note and 'relative_path' not in catalog_note
    del bot._meme_controller
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, mode='expression', probability=.2)
    ctrl.clock = lambda: 1001
    next_event = make_event(bot, identifier='next-question')
    _, next_notes = await prepare(bot, next_event, [{'role': 'user', 'api_content': 'hello\n' + '\n'.join(notes)}])
    assert catalog_note not in next_notes
    facts = next(n for n in next_notes if n.startswith('[本机QQ表情发送事实/v1]'))
    assert 'affirmation' in facts and '"outcome":"sent"' in facts and '"id":"yes"' in facts
    assert len(requests) == 1
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT outcome,message_id FROM expressions').fetchone() == ('sent', 'actual-platform-1')
    receipts = receipt_journal(bot, API_BASE).lookup_source(chat_id=event.source.chat_id,
        source_session_key=bot._source_session_key(event.source), source_message_id=event.message_id)
    assert receipts[-1].message_id == 'actual-platform-1'


@pytest.mark.asyncio
async def test_facts_do_not_cross_session_chat_or_transport(expression_bot, wire):
    bot = expression_bot
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), TAG)
    _, notes = await prepare(bot, make_event(bot, identifier='new-session'), session='session-two')
    assert not any('本机QQ表情发送事实/v1' in n for n in notes)
    group = 'other-group'
    bot._chat_type_map[group] = 'group'
    controller_for(bot).settings = replace(controller_for(bot).settings, all_groups=True)
    _, notes = await prepare(bot, make_event(bot, chat=group, identifier='other'))
    assert not any('本机QQ表情发送事实/v1' in n for n in notes)


@pytest.mark.asyncio
async def test_plain_chat_does_not_get_background_decoration(expression_bot, wire):
    bot = expression_bot
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), '普通回复，没有选择表情。')
    assert len(requests) == 1
    bot._upload_local_file.assert_not_awaited()
    controller_for(bot).selector.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['[[qq_meme:unknown]]', '[[qq_meme:../secret.gif]]', '[[qq_meme_optional:yes]]'])
async def test_invalid_or_optional_only_choices_do_not_send_images(expression_bot, wire, text):
    bot = expression_bot
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        _, results = await emit(bot, make_event(bot), text)
    assert not requests
    assert all('qq_meme' not in json.dumps(body) for _, body in requests)
    bot._upload_local_file.assert_not_awaited()
    assert not results


@pytest.mark.asyncio
async def test_unknown_media_outcome_is_not_sent_and_is_not_replayed(expression_bot, wire):
    bot = expression_bot
    bot.send_image_file = AsyncMock(return_value=SendResult(success=False, error='ACK lost'))
    event = make_event(bot)
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, event, '给管理员看这张。\n' + TAG)
        await emit(bot, event, TAG)
    bot.send_image_file.assert_awaited_once()
    _, notes = await prepare(bot, make_event(bot, identifier='what-was-that'))
    facts = next(n for n in notes if n.startswith('[本机QQ表情发送事实/v1]'))
    assert '"outcome":"unknown"' in facts and '"outcome":"sent"' not in facts


@pytest.mark.asyncio
async def test_failed_text_prevents_the_following_gif(expression_bot, wire):
    bot = expression_bot
    bot._send_with_retry = AsyncMock(return_value=SendResult(success=False, error='refused'))
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), '这张给管理员。\n' + TAG)
    bot._upload_local_file.assert_not_awaited()
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT outcome,reason FROM expressions').fetchone() == ('blocked', 'text_not_confirmed')


@pytest.mark.asyncio
async def test_real_background_delivery_accepts_a_gif_only_reply_without_empty_notice(expression_bot, wire):
    bot = expression_bot
    event = make_event(bot)
    key, _ = await prepare(bot, event)
    bot._message_handler = AsyncMock(return_value=TAG)
    bot._run_processing_hook = AsyncMock()
    bot._start_typing_refresh = lambda *a, **kw: None
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await bot._process_message_background(event, key)
    assert [body['msg_type'] for _, body in requests] == [7]
    outcomes = [call.args[-1] for call in bot._run_processing_hook.call_args_list
                if call.args[0] == 'on_processing_complete']
    assert outcomes == [ProcessingOutcome.SUCCESS]


@pytest.mark.asyncio
async def test_interim_or_direct_send_cannot_leak_a_control_marker(expression_bot, wire):
    bot = expression_bot
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result = await bot.send(chat_id=make_event(bot).source.chat_id, content='看这张。\n' + TAG)
    assert not result.success and not requests


@pytest.mark.asyncio
async def test_generic_gateway_note_seam_uses_the_live_adapter(expression_bot):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = lambda source: expression_bot
    event = make_event(expression_bot)
    key = expression_bot._source_session_key(event.source)
    notes = await runner._hmwa_adapter_turn_notes(event, 'session-one', key, [])
    assert any('QQ表情能力/v1' in n for n in notes)
    assert event._qq_expression_context.session_id == 'session-one'


@pytest.mark.asyncio
async def test_expression_journal_failure_does_not_destroy_the_text(expression_bot, wire):
    bot = expression_bot
    journal = engine_for(bot).journal
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        with patch.object(journal, 'add', side_effect=OSError('disk unavailable')):
            await emit(bot, make_event(bot), '文字仍须送达。\n' + TAG)
    assert [body['msg_type'] for _, body in requests] == [2]
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_fact_write_failure_does_not_replay_or_fail_the_text(expression_bot, wire):
    bot = expression_bot
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        with patch.object(engine_for(bot).journal, 'end_send', side_effect=OSError('disk unavailable')):
            _, results = await emit(bot, make_event(bot), '文字仍须送达。\n' + TAG)
    assert [body['msg_type'] for _, body in requests] == [2, 7]
    assert results[0].success
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT outcome FROM expressions').fetchone() == ('attempting',)


@pytest.mark.asyncio
async def test_reentrant_extract_does_not_orphan_or_generically_resend_the_gif(expression_bot, wire):
    bot, event = expression_bot, make_event(expression_bot)
    key, _ = await prepare(bot, event)
    first = await bot._extract_response_content(TAG, event, key, is_ephemeral_response=False)
    second = await bot._extract_response_content(TAG, event, key, is_ephemeral_response=False)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await bot._deliver_attachments(event, first, {}, anything_sent=False, record_delivery=lambda r: None)
        await bot._deliver_attachments(event, second, {}, anything_sent=False, record_delivery=lambda r: None)
    assert [body['msg_type'] for _, body in requests] == [7]
    bot._upload_local_file.assert_awaited_once()
    assert not list((controller_for(bot).home / 'cache/qqbot/meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
async def test_optional_miss_is_persisted_without_pulling_another_model(expression_bot, wire):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.draw = lambda: .99
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), '普通文字，不承诺图。\n[[qq_meme_optional:yes]]')
    assert [body['msg_type'] for _, body in requests] == [2]
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT outcome FROM expressions').fetchone() == ('not_selected',)
        assert db.execute('SELECT misses FROM meme_prd').fetchone() == (1,)
    ctrl.selector.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['disabled', 'cooldown', 'daily_limit'])
async def test_hard_limits_still_stop_an_explicit_choice(expression_bot, wire, reason):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, recent=0)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        if reason == 'disabled':
            ctrl.settings = replace(ctrl.settings, probability=0)
        else:
            await emit(bot, make_event(bot), TAG)
            ctrl.clock = lambda: 1040
            if reason == 'daily_limit':
                ctrl.settings = replace(ctrl.settings, cooldown=0, daily_limit=1)
        await emit(bot, make_event(bot, identifier='another-turn'), '这次选图。\n' + TAG)
    assert sum(body['msg_type'] == 7 for _, body in requests) == (0 if reason == 'disabled' else 1)
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT outcome,reason FROM expressions ORDER BY rowid DESC LIMIT 1').fetchone() == ('blocked', reason)


@pytest.mark.asyncio
async def test_zero_daily_limit_keeps_an_explicit_choice_available(expression_bot, wire):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0, daily_limit=0)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        for index in range(2):
            ctrl.clock = lambda index=index: 1000 + index * 10
            await emit(bot, make_event(bot, identifier=f'turn-{index}'), '给管理员。\n' + TAG)
    assert sum(body['msg_type'] == 7 for _, body in requests) == 2
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute("SELECT COUNT(*) FROM expressions WHERE outcome='sent'").fetchone() == (2,)


@pytest.mark.asyncio
async def test_changed_reviewed_bytes_are_not_sent_and_do_not_mute_the_text(expression_bot, wire):
    bot, event = expression_bot, make_event(expression_bot)
    await prepare(bot, event)
    original = Path(controller_for(bot).settings.root) / '肯定.gif'
    original.write_bytes(b'GIF89a replaced after review')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, event, '文字仍须送达。\n' + TAG)
    assert [body['msg_type'] for _, body in requests] == [2]
    bot._upload_local_file.assert_not_awaited()
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT outcome,reason FROM expressions').fetchone() == ('error', 'preflight_failed')


@pytest.mark.asyncio
async def test_unapproved_private_chat_gets_no_catalog_and_no_gif(expression_bot, wire):
    bot = expression_bot
    bot._chat_type_map['stranger'] = 'c2c'
    event = make_event(bot, chat='stranger', user='stranger', kind='dm')
    _key, notes = await prepare(bot, event)
    assert notes == [] and not hasattr(event, '_qq_expression_context')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, event, TAG)
    assert not any(body['msg_type'] == 7 for _, body in requests)
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_after_attempt_is_unknown_and_releases_the_snapshot(expression_bot, wire):
    bot = expression_bot
    bot.send_image_file = AsyncMock(side_effect=asyncio.CancelledError())
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        with pytest.raises(asyncio.CancelledError):
            await emit(bot, make_event(bot), TAG)
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT outcome FROM expressions').fetchone() == ('unknown',)
    assert not list((controller_for(bot).home / 'cache/qqbot/meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
async def test_none_verdict_strips_the_marker_and_sends_only_text(expression_bot, wire):
    bot = expression_bot
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await emit(bot, make_event(bot), '这轮不配图。\n[[qq_meme_none]]')
    assert [body['msg_type'] for _, body in requests] == [2]
    assert 'qq_meme' not in json.dumps(requests[0][1], ensure_ascii=False)
    bot._upload_local_file.assert_not_awaited()
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT COUNT(*) FROM expressions').fetchone() == (0,)


@pytest.mark.asyncio
async def test_raw_none_marker_cannot_leak_through_a_plain_send(expression_bot):
    result = await expression_bot.send(chat_id='meme-test-group', content='正文 [[qq_meme_none]]')
    assert not result.success and 'Expression directive' in result.error


@pytest.mark.asyncio
async def test_missing_verdict_on_an_eligible_turn_is_recorded(expression_bot, wire, caplog):
    import logging
    bot = expression_bot
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        with caplog.at_level(logging.INFO):
            await emit(bot, make_event(bot), '老老实实回答，没有给结论。')
    assert any('turn without verdict' in record.getMessage() for record in caplog.records)
    caplog.clear()
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        with caplog.at_level(logging.INFO):
            await emit(bot, make_event(bot, identifier='judged'), '判断过了。\n[[qq_meme_none]]')
    assert not any('turn without verdict' in record.getMessage() for record in caplog.records)
    assert any('none verdict' in record.getMessage() for record in caplog.records)
