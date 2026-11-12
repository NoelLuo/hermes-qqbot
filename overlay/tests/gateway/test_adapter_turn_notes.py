"""Adapter notes reach real model input/persistence without changing the cached prefix."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent import title_generator
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry
from gateway.session_surface import arm_surface_turn
from hermes_state import SessionDB
from run_agent import AIAgent
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, wire, make_event
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, emit, TAG
from gateway.session_envelope import prepare_native, restore_native
from tests.gateway.test_qqbot_expression_native import native_runner


class _ModelInputObserved(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('durable', [False, True])
async def test_real_prepare_and_agent_sidecar_persistence_keep_old_prefix_stable(
        expression_bot, wire, tmp_path, monkeypatch, durable):
    bot = expression_bot
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, cooldown=0, recent=0)
    bot._chat_type_map['owner'] = 'c2c'
    event = make_event(bot, chat='owner', user='owner', kind='dm')
    if durable:
        native = native_runner(bot)
        payload = json.loads(json.dumps(await prepare_native(native, event)))
        event = restore_native(payload, native)
    key = bot._source_session_key(event.source)
    session_id = 'sidecar-session'
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.QQBOT: bot.config})
    runner._delivery_adapter_for = lambda source: bot
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._set_session_env = lambda context: []
    runner._clear_session_env = lambda tokens: None
    runner._pinned_session_context_prompt = lambda *a, **kw: 'FIXED SESSION PROMPT'
    runner._hmwa_acquire_turn_lease = runner._mark_durable_active_turn = AsyncMock()
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._voice_channel_sidecar_note = lambda *a: None
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw['event'].text)
    runner._bind_adapter_run_generation = lambda *a: None
    runner._pending_turn_sidecar_notes = {}
    monkeypatch.setattr('gateway.run._load_gateway_config', dict)
    monkeypatch.setattr('hermes_cli.config.load_config_readonly', dict)
    monkeypatch.setattr(title_generator, 'maybe_auto_title', lambda *a, **kw: None)
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store = object()

    async def load(sid):
        return db.get_messages_as_conversation(sid)

    async def hygiene(event, source, entry, session_key, history, *args):
        return history

    runner._async_session_store = SimpleNamespace(_store=runner.session_store, load_transcript=load)
    runner._hmwa_run_session_hygiene = hygiene
    entry = SessionEntry(session_id=session_id, session_key=key,
                         created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
    agent = AIAgent(session_db=db, model='test-model', api_key='test-key',
                    base_url='http://127.0.0.1:1/v1', platform='qqbot', session_id=session_id,
                    enabled_toolsets=[], quiet_mode=True, skip_memory=True, skip_context_files=True)
    agent.compression_enabled = False
    inputs = []

    def observe(messages, tools_for_api=None):
        inputs.append(deepcopy(messages))
        raise _ModelInputObserved

    # This is the actual provider-request boundary, after api_content projection;
    # begin_iteration still carries DB-only row IDs and does not contain a system message.
    monkeypatch.setattr(agent, '_build_api_kwargs', observe)
    try:
        first, _ = await runner._hmwa_prepare_turn(event, event.source, entry, key, key, 1)
        first_notes = runner._consume_pending_turn_sidecar_notes(key)
        assert any('QQ表情能力/v1' in note for note in first_notes)
        arm_surface_turn(agent, first_notes)
        with pytest.raises(_ModelInputObserved):
            agent.run_conversation(first.message_text, conversation_history=first.history,
                                   persist_user_message=first.persist_user_message)
        first_rows = db.get_messages(session_id)
        assert first_rows[0]['content'] == event.text
        assert 'QQ表情能力/v1' in first_rows[0]['api_content']
        db.append_message(session_id, 'assistant', TAG)
        _, transport = wire
        async with httpx.AsyncClient(transport=transport) as client:
            bot._http_client = client
            await emit(bot, event, TAG, session=session_id)
        old_rows = deepcopy(db.get_messages(session_id))
        next_event = make_event(bot, chat='owner', user='owner', kind='dm', identifier='follow-up')
        next_event.text = '刚才那张是什么意思？'
        second, _ = await runner._hmwa_prepare_turn(next_event, next_event.source, entry, key, key, 2)
        second_notes = runner._consume_pending_turn_sidecar_notes(key)
        assert not any(note.startswith('[QQ表情能力/v1]') for note in second_notes)
        assert any('"outcome":"sent"' in note and 'affirmation' in note for note in second_notes)
        assert first.context_prompt == second.context_prompt == 'FIXED SESSION PROMPT'
        arm_surface_turn(agent, second_notes)
        with pytest.raises(_ModelInputObserved):
            agent.run_conversation(second.message_text, conversation_history=second.history,
                                   persist_user_message=second.persist_user_message)
        assert inputs[0][0] == inputs[1][0]  # exact system message, not merely its hash
        assert inputs[0][-1]['content'] == inputs[1][1]['content']  # previous user API bytes unchanged
        for payload, count in zip(inputs, [0, 1]):
            choice_json = payload[-1]['content'].split('\n候选=', 1)[1]
            candidates, _ = json.JSONDecoder().raw_decode(choice_json)
            assert candidates[0]['use_when'] == ['agree']
            assert candidates[0]['recent_sent_count'] == count
        assert '本机QQ表情发送事实/v1' in inputs[1][-1]['content']
        assert db.get_messages(session_id)[:len(old_rows)] == old_rows
        assert runner._consume_pending_turn_sidecar_notes(key) == []
    finally:
        agent.close()
        db.close()
