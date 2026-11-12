"""Conversational uses and scoped successful-send counts inform, never replace, the model's choice."""
from dataclasses import replace

import httpx
import pytest

from plugins.platforms.qqbot import meme_expression
from plugins.platforms.qqbot.meme_catalog import MemeCatalog
from plugins.platforms.qqbot.meme_expression_delivery import engine_for
from plugins.platforms.qqbot.meme_state import MemeState
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import GROUP, bot, make_event, wire
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, prepare, emit
from tests.plugins.platforms.test_qqbot_meme_turn_choices import choices


def record_send(state, identifier, chat, meme_id, now, outcome='sent'):
    assert state.claim(identifier, chat, now, probability=1, cooldown=0, daily_limit=0)
    assert state.choose(identifier, 'strong', now, probability=1, draw=lambda: 0)
    state.attempting(identifier, meme_id)
    state.finish(identifier, outcome, 'test-receipt-' + identifier if outcome == 'sent' else None)


def entry_for(adapter):
    settings = controller_for(adapter).settings
    return MemeCatalog.load(settings.root, settings.catalog).entries[0]


def test_uses_and_frequency_are_soft_hints_not_a_replacement_selector(expression_bot):
    entry = entry_for(expression_bot)
    rare = replace(entry, id='rare', description='another suitable reaction',
                   use_when=('encouragement', 'patient support'))
    recent = ('yes',) * 40 + ('rare',)
    note = meme_expression.turn_choice_note((entry, rare), 'available', (), recent_sent=recent)
    _, candidates = choices([note])
    assert [item['id'] for item in candidates] == ['yes', 'rare']
    assert candidates[0]['recent_sent_count'] == 40
    assert candidates[1]['recent_sent_count'] == 1
    assert candidates[1]['use_when'] == list(rare.use_when)
    assert 'relative_path' not in note and 'sha256' not in note
    assert '同样贴切' in note and '不代表从未使用' in note
    assert entry_for(expression_bot) == entry


@pytest.mark.asyncio
async def test_usage_counts_only_confirmed_sends_not_intents_misses_or_unknown(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0)
    chat = 'group:' + GROUP
    record_send(ctrl.state, 'confirmed', chat, 'yes', 1000)
    record_send(ctrl.state, 'unknown', chat, 'yes', 1040, 'unknown')
    assert ctrl.state.claim('missed', chat, 1080, probability=.2, cooldown=0, daily_limit=0)
    assert not ctrl.state.choose('missed', 'natural', 1080, probability=.2, draw=lambda: .99)
    journal = engine_for(expression_bot).journal
    journal.add('selected-only', chat, 'session-one', 'yes', 'affirmation', 1120)
    journal.add('missed', chat, 'session-one', 'yes', 'affirmation', 1080)
    journal.finish('missed', 'not_selected', 'optional_prd')
    ctrl.clock = lambda: 2000
    _, notes = await prepare(expression_bot, make_event(expression_bot))
    _, candidates = choices(notes)
    assert candidates[0]['recent_sent_count'] == 1
    assert candidates[0]['use_when'] == ['agree']
    ctrl.selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_window_rolls_over_successful_sends_not_all_decisions(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0, daily_limit=0)
    chat = 'group:' + GROUP
    window = meme_expression.RECENT_SENT_WINDOW
    for index in range(window + 5):
        meme_id = 'yes' if index < 5 or index == window + 4 else 'retired-reaction'
        record_send(ctrl.state, f'confirmed-{index}', chat, meme_id, 1000 + index * 40)
    for index in range(3):
        record_send(ctrl.state, f'unknown-{index}', chat, 'yes', 5000 + index * 40, 'unknown')
    ctrl.clock = lambda: 6000
    _, notes = await prepare(expression_bot, make_event(expression_bot))
    note, candidates = choices(notes)
    assert candidates[0]['recent_sent_count'] == 1
    assert 'retired-reaction' not in note
    assert f'最近{window}次成功发送' in note


@pytest.mark.asyncio
async def test_usage_is_scoped_by_profile_transport_chat_type_and_chat_id(expression_bot, tmp_path):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0, all_groups=True)
    chat = 'group:' + GROUP
    record_send(ctrl.state, 'local', chat, 'yes', 1000)
    record_send(ctrl.state, 'other-group', 'group:other-group', 'yes', 1040)
    record_send(ctrl.state, 'same-id-private', 'dm:' + GROUP, 'yes', 1080)
    record_send(MemeState(ctrl.home, 'another-transport'), 'other-transport', chat, 'yes', 1120)
    record_send(MemeState(tmp_path / 'another-profile', ctrl.state.transport),
                'other-profile', chat, 'yes', 1160)
    ctrl.clock = lambda: 2000
    _, notes = await prepare(expression_bot, make_event(expression_bot))
    assert choices(notes)[1][0]['recent_sent_count'] == 1
    _, new_session_notes = await prepare(expression_bot, make_event(expression_bot, identifier='new'),
                                         session='new-session')
    assert choices(new_session_notes)[1][0]['recent_sent_count'] == 1
    expression_bot._chat_type_map['fresh-group'] = 'group'
    _, fresh_notes = await prepare(expression_bot, make_event(expression_bot, chat='fresh-group'))
    assert choices(fresh_notes)[1][0]['recent_sent_count'] == 0


@pytest.mark.asyncio
async def test_reentrant_preparation_keeps_one_snapshot_until_the_next_event(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0)
    event = make_event(expression_bot)
    ctrl.clock = lambda: 1000
    _, first = await prepare(expression_bot, event)
    context = event._qq_expression_context
    record_send(ctrl.state, 'intervening', 'group:' + GROUP, 'yes', 1040)
    ctrl.clock = lambda: 2000
    _, repeated = await prepare(expression_bot, event)
    assert repeated == first
    assert event._qq_expression_context is context
    repeated.clear()  # A caller cannot mutate the saved snapshot through its return value.
    _, repeated = await prepare(expression_bot, event)
    assert repeated == first
    _, next_notes = await prepare(expression_bot, make_event(expression_bot, identifier='next'))
    assert choices(first)[1][0]['recent_sent_count'] == 0
    assert choices(next_notes)[1][0]['recent_sent_count'] == 1


@pytest.mark.asyncio
async def test_reentrant_snapshot_is_bound_to_session_and_still_checks_eligibility(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=0, cooldown=0)
    event = make_event(expression_bot)
    _, first = await prepare(expression_bot, event)
    record_send(ctrl.state, 'intervening', 'group:' + GROUP, 'yes', 1040)
    ctrl.clock = lambda: 2000
    _, next_session = await prepare(expression_bot, event, session='another-session')
    assert choices(first)[1][0]['recent_sent_count'] == 0
    assert choices(next_session)[1][0]['recent_sent_count'] == 1
    ctrl.settings = replace(ctrl.settings, enabled=False)
    _, disabled = await prepare(expression_bot, event, session='another-session')
    assert disabled == []


@pytest.mark.asyncio
async def test_hard_recent_exclusion_remains_separate_from_usage_hints(expression_bot):
    ctrl = controller_for(expression_bot)
    ctrl.settings = replace(ctrl.settings, recent=1, cooldown=0)
    record_send(ctrl.state, 'latest', 'group:' + GROUP, 'yes', 1000)
    ctrl.clock = lambda: 2000
    _, notes = await prepare(expression_bot, make_event(expression_bot))
    note, candidates = choices(notes)
    assert 'status=no_candidates' in note and candidates == []
    ctrl.settings = replace(ctrl.settings, recent=0)
    _, allowed = await prepare(expression_bot, make_event(expression_bot, identifier='next'))
    assert choices(allowed)[1][0]['recent_sent_count'] == 1


@pytest.mark.parametrize('availability', ['disabled', 'cooldown', 'daily_limit'])
def test_unavailable_turns_expose_no_candidates_or_usage_hints(expression_bot, availability):
    note = meme_expression.turn_choice_note((entry_for(expression_bot),), availability, (),
                                            recent_sent=('yes',))
    _, candidates = choices([note])
    assert candidates == [] and 'recent_sent_count' not in note
    assert '[[qq_meme_none]]' in note


@pytest.mark.asyncio
async def test_popular_model_choice_is_not_reranked_or_dropped_by_diversity(expression_bot, wire):
    ctrl = controller_for(expression_bot)
    original = ctrl.settings
    ctrl.settings = replace(original, recent=0, cooldown=0)
    for index in range(4):
        record_send(ctrl.state, f'old-{index}', 'group:' + GROUP, 'yes', 1000 + index * 40)
    ctrl.clock = lambda: 2000
    ctrl.draw = lambda: pytest.fail('Diversity must not add random draws to GIF-only replies')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        expression_bot._http_client = client
        notes, results = await emit(expression_bot, make_event(expression_bot), '[[qq_meme:yes]]')
    assert choices(notes)[1][0]['recent_sent_count'] == 4
    assert [body['msg_type'] for _, body in requests] == [7]
    assert all(result.success for result in results)
    assert ctrl.settings.probability == original.probability
    assert ctrl.state.recent('group:' + GROUP, 1) == ('yes',)
    ctrl.selector.assert_not_awaited()
