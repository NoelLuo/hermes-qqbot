"""Fresh per-turn choices survive old none verdicts without rewriting history."""
import copy
import json
from dataclasses import replace

import httpx
import pytest

from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, make_event, wire
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, prepare, emit


def choices(notes):
    note = next(n for n in notes if n.startswith('[本轮QQ表情可用性]'))
    return note, json.loads(note.split('\n候选=', 1)[1])


@pytest.mark.asyncio
async def test_each_turn_reoffers_nonexcluded_semantics_without_mutating_history(expression_bot):
    adapter = expression_bot
    _, first = await prepare(adapter, make_event(adapter))
    history = [{'role': 'user', 'api_content': '\n'.join(first)},
               {'role': 'assistant', 'content': '普通回答\n[[qq_meme_none]]'}]
    before = copy.deepcopy(history)
    _, notes = await prepare(adapter, make_event(adapter, identifier='next'), history)
    note, candidates = choices(notes)
    assert candidates == [{'id': 'yes', 'description': 'affirmation',
                           'use_when': ['agree'], 'recent_sent_count': 0}]
    assert 'status=available' in note and '[[qq_meme_optional:完整ID]]' in note
    assert not any(n.startswith('[QQ表情能力/v1]') for n in notes)
    assert history == before


@pytest.mark.asyncio
async def test_excluded_catalog_is_reported_as_no_candidates(expression_bot, wire):
    ctrl = controller_for(expression_bot)
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        expression_bot._http_client = client
        await emit(expression_bot, make_event(expression_bot), '[[qq_meme:yes]]')
    ctrl.clock = lambda: 2000
    _, notes = await prepare(expression_bot, make_event(expression_bot, identifier='after'))
    note, candidates = choices(notes)
    assert 'status=no_candidates' in note and candidates == []
    assert '[[qq_meme_none]]' in note


@pytest.mark.asyncio
async def test_disabled_probability_does_not_offer_send_choices(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, probability=0)
    _, notes = await prepare(expression_bot, make_event(expression_bot))
    note, candidates = choices(notes)
    assert 'status=disabled' in note and candidates == []


@pytest.mark.asyncio
async def test_second_explicit_dm_user_gets_choices_and_native_gif_not_other_users(expression_bot, wire):
    adapter = expression_bot
    ctrl = controller_for(adapter)
    ctrl.settings = replace(ctrl.settings, dm_users=frozenset({'owner', 'second-approved-user'}))
    adapter._chat_type_map['second-approved-user'] = 'c2c'
    event = make_event(adapter, chat='second-approved-user', user='second-approved-user', kind='dm')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        notes, results = await emit(adapter, event, '好的。\n[[qq_meme_optional:yes]]')
    assert choices(notes)[1] == [{'id': 'yes', 'description': 'affirmation',
                                 'use_when': ['agree'], 'recent_sent_count': 0}]
    assert all(r.success for r in results)
    assert [body['msg_type'] for _, body in requests] == [2, 7]
    assert all(path == '/v2/users/second-approved-user/messages' for path, _ in requests)
    assert not {'content', 'markdown', 'caption'} & requests[-1][1].keys()
    adapter._chat_type_map['unlisted-user'] = 'c2c'
    _, other = await prepare(adapter, make_event(adapter, chat='unlisted-user', user='unlisted-user', kind='dm'))
    assert other == []
