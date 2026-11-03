"""QQ expression context must cross native admission, not just a direct prepare call."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import json

import httpx
import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session_envelope import prepare_native, restore_native, snapshot_native
from gateway.session_ingress import deliver_response, deliver_settled, execute_admission
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, wire, make_event
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, TAG


def native_runner(adapter):
    return NS(config=NS(multiplex_profiles=False),
              session_store=NS(_generate_session_key=adapter._source_session_key),
              _adapter_for_source=lambda source: adapter,
              _adapter_profile_for_source=lambda source: None,
              _is_user_authorized_for_source=lambda *a, **kw: True,
              _adapter_flag=lambda *a: False)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['group', 'dm'])
@pytest.mark.parametrize('waiting', [True, False])
@pytest.mark.parametrize('response', [TAG, '这张给管理员。\n' + TAG])
async def test_native_json_execution_and_final_share_bound_expression_context(
        expression_bot, wire, kind, waiting, response):
    adapter = expression_bot
    adapter._chat_type_map['owner'] = 'c2c'
    original = make_event(adapter, **({'chat': 'owner', 'user': 'owner', 'kind': 'dm'} if kind == 'dm' else {}))
    key = adapter._source_session_key(original.source)
    runner = native_runner(adapter)
    # prepare_native freezes the event with dataclasses.replace, then JSON persists it.
    payload = json.loads(json.dumps(await prepare_native(runner, original)))
    row = {'payload': payload, 'admission_id': 'admission-one', 'request_id': original.message_id}
    ref = NS(session_id='native-session')
    notes_seen = []

    async def handle(executed):
        assert executed is not original and executed.source is not original.source
        notes = await adapter.prepare_turn_notes(executed, ref.session_id, key, [])
        notes_seen.extend(notes)
        assert any('QQ表情能力/v1' in note and 'affirmation' in note for note in notes)
        return response

    runner._handle_message = handle
    authority = NS(runner=runner, sessions={ref.session_id: NS(source=original.source, route=key)},
                   native_waiters={'admission-one'} if waiting else set(), pending_results={}, pending_deliveries={},
                   export_delivery_waiters={'admission-one': original} if waiting else {})
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        answer = await execute_admission(authority, ref, row)
        if waiting:
            await deliver_response(adapter, original, key, answer)
        else:
            await deliver_settled(authority, 'admission-one')
    assert notes_seen
    assert [body['msg_type'] for _, body in requests] == ([2, 7] if response != TAG else [7])
    assert not {'content', 'markdown', 'caption'} & requests[-1][1].keys()
    next_event = make_event(adapter, chat=original.source.chat_id, user=original.source.user_id,
                            kind=kind, identifier='next')
    notes = await adapter.prepare_turn_notes(next_event, ref.session_id, key, [])
    assert any('"outcome":"sent"' in note and 'affirmation' in note for note in notes)
    controller_for(adapter).selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_ingest_is_the_only_source_of_the_durable_marker(expression_bot):
    adapter = expression_bot
    received = []
    adapter.handle_message = AsyncMock(side_effect=received.append)
    raw = {'id': 'native-ingest', 'content': 'only a GIF please'}
    await adapter._ingest(raw, raw['id'], raw['content'], [], '',
                          chat_id='owner', qq_chat_type='c2c', user_id='owner', chat_type='dm')
    assert len(received) == 1
    original = received[0]
    runner = native_runner(adapter)
    payload = json.loads(json.dumps(snapshot_native(runner, original)))
    restored = restore_native(payload, runner)
    key = adapter._source_session_key(restored.source)
    assert any('QQ表情能力/v1' in note for note in
               await adapter.prepare_turn_notes(restored, 'real-ingest-session', key, []))
    synthetic = MessageEvent(source=replace(original.source), text=original.text,
                             message_id='synthetic', raw_message=raw)
    payload = json.loads(json.dumps(await prepare_native(runner, synthetic)))
    restored = restore_native(payload, runner)
    assert await adapter.prepare_turn_notes(restored, 'real-ingest-session', key, []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['message', 'subject', 'chat', 'profile', 'thread', 'transport'])
async def test_native_snapshot_cannot_be_rebound(expression_bot, change):
    adapter = expression_bot
    original = make_event(adapter)
    runner = native_runner(adapter)
    payload = json.loads(json.dumps(await prepare_native(runner, original)))
    altered = deepcopy(payload)
    envelope = altered['native_text_v1']
    if change == 'message':
        envelope['event']['message_id'] = 'another'
    elif change == 'transport':
        adapter._app_id = 'another-bot'
    else:
        field = {'subject': 'user_id', 'chat': 'chat_id', 'profile': 'profile', 'thread': 'thread_id'}[change]
        envelope['source'][field] = 'another'
    restored = restore_native(altered, runner)
    notes = await adapter.prepare_turn_notes(restored, 'native-session',
                                            adapter._source_session_key(restored.source), [])
    assert not notes


@pytest.mark.asyncio
async def test_old_native_rows_and_other_adapters_keep_the_old_contract(expression_bot):
    adapter = expression_bot
    runner = native_runner(adapter)
    event = make_event(adapter)
    payload = json.loads(json.dumps(await prepare_native(runner, event)))
    # Old admissions never had an adapter-owned stamp: do not infer one from human text.
    payload['native_text_v1'].pop('adapter_turn', None)
    restored = restore_native(payload, runner)
    assert await adapter.prepare_turn_notes(restored, 'old-session', adapter._source_session_key(restored.source), []) == []
    other_adapter = object()
    runner._adapter_for_source = lambda source: other_adapter
    event.source.platform = Platform.TELEGRAM
    payload = json.loads(json.dumps(snapshot_native(runner, event)))
    assert 'adapter_turn' not in payload['native_text_v1']
    assert restore_native(payload, runner).text == event.text


@pytest.mark.asyncio
async def test_real_native_authority_fifo_restores_each_turn_and_never_resends(expression_bot, wire, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority, SessionAuthority
    from gateway.session_ingress_context import native_callback
    from hermes_constants import get_hermes_home
    from hermes_state_runtime import list_session_admissions

    adapter = expression_bot
    ctrl = controller_for(adapter)
    ctrl.settings = replace(ctrl.settings, cooldown=0, recent=0)
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('QQ_ALLOW_ALL_USERS', 'true')
    runner = GatewayRunner(GatewayConfig(platforms={Platform.QQBOT: adapter.config}))
    runner.adapters = {Platform.QQBOT: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='native-owner')
    runner._wire_adapter_handlers(adapter)
    events = [make_event(adapter, identifier=name) for name in ('first-native', 'second-native')]
    receipts = []
    for event in events:
        with native_callback(runner, event, get_hermes_home()):
            receipts.append(await authority.admit_native(event))
    notes_seen = []

    async def handle(event):
        key = runner.session_store._generate_session_key(event.source)
        session_id = runner.session_store.peek_session_id(key)
        ctrl.clock = lambda: 1000 + len(notes_seen)
        notes = await runner._hmwa_adapter_turn_notes(event, session_id, key, [])
        assert any('QQ表情能力/v1' in note for note in notes)
        notes_seen.append(notes)
        return '给管理员。\n' + TAG

    monkeypatch.setattr(runner, '_handle_message', handle)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        await SessionAuthority._drain(authority, receipts[0].ref)
        assert [body['msg_type'] for _, body in requests] == [2, 7, 2, 7]
        assert any('"outcome":"sent"' in note for note in notes_seen[1])
        with native_callback(runner, events[0], get_hermes_home()):
            duplicate = await authority.admit_native(events[0])
        assert duplicate.admission_id == receipts[0].admission_id
        assert len(requests) == 4
    rows = list_session_admissions(authority.db, session_id=receipts[0].ref.session_id, pending_only=False)
    assert [(row['request_id'], row['status']) for row in rows] == [
        ('first-native', 'terminal'), ('second-native', 'terminal')]
