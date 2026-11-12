"""Expression stamps survive the real QQ callback and cold native admission."""
import asyncio
from copy import deepcopy
import importlib
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session_authority import SessionAuthority, initialize_session_authority
from hermes_state_runtime import list_session_admissions
from tests.plugins.platforms.test_qqbot_memes import bot, wire
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_real_c2c_callback_keeps_expression_stamp(expression_bot, monkeypatch):
    adapter = expression_bot
    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('QQ_ALLOW_ALL_USERS', 'true')
    runner = GatewayRunner(GatewayConfig(platforms={Platform.QQBOT: adapter.config}))
    runner.adapters = {Platform.QQBOT: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='expression-ingress')
    runner._wire_adapter_handlers(adapter)
    raw = {'id': 'cold-expression', 'content': 'a GIF please', 'author': {'user_openid': 'owner'}}

    def admissions():
        session_key = adapter._source_session_key(adapter.build_source(
            chat_id='owner', user_id='owner', chat_type='dm'))
        entry = runner.session_store.lookup_by_session_key(session_key)
        return list_session_admissions(authority.db, session_id=entry.session_id, pending_only=False) if entry else []

    try:
        await adapter._on_message('C2C_MESSAGE_CREATE', raw)
        rows = await async_wait_until(admissions, what='QQ callback native admission')
        payload = rows[0]['payload']
        assert payload['native_text_v1'].get('adapter_turn'), payload['native_text_v1'].keys()
    finally:
        tasks = list(adapter._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['dm', 'group'])
@pytest.mark.parametrize('response', ['[[qq_meme:yes]]', 'hello\n[[qq_meme:yes]]'])
async def test_live_adapter_keeps_its_expression_generation_after_plugin_reload(expression_bot, wire, kind, response):
    from hermes_cli.plugins import PluginManager, PluginManifest
    from hermes_cli.plugins_loader import _evict_modules
    from gateway.session_envelope import snapshot_native, restore_native
    from gateway.native_adapter_turns import bridge_adapter_turn

    prefix = 'hermes_plugins.qq_expression_reload_probe'
    package = Path(importlib.import_module(type(expression_bot).__module__).__file__).parent
    manager = PluginManager()
    manifest = PluginManifest(name='qqbot', key='platforms/qqbot', source='bundled', path=package)
    try:
        manager._load_directory_module(manifest, module_name=prefix)
        old_module = importlib.import_module(prefix + '.adapter')
        old_receipts = importlib.import_module(prefix + '.receipt_adapter')
        extra = deepcopy(expression_bot.config.extra)
        extra['memes'].update(mode='expression', cooldown_seconds=0, recent_exclude=0)
        adapter = old_module.QQAdapter(PlatformConfig(enabled=True, extra=extra))
        events = []
        adapter.handle_message = AsyncMock(side_effect=events.append)
        adapter._ensure_connected = AsyncMock(return_value=True)
        adapter._auth_headers = AsyncMock(return_value={})
        adapter._upload_local_file = AsyncMock(return_value={'file_info': 'synthetic-upload'})
        raw = {'id': 'before-reload', 'content': 'a GIF please'}
        chat = 'owner' if kind == 'dm' else extra['memes']['groups'][0]
        await adapter._ingest(raw, raw['id'], raw['content'], [], '',
                              chat_id=chat, qq_chat_type='c2c' if kind == 'dm' else 'group',
                              user_id='owner', chat_type=kind)
        event = events[0]
        runner = NS(config=NS(multiplex_profiles=False),
                    session_store=NS(_generate_session_key=adapter._source_session_key),
                    _adapter_for_source=lambda source: adapter,
                    _adapter_profile_for_source=lambda source: None,
                    _is_user_authorized_for_source=lambda *args, **kwargs: True,
                    _adapter_flag=lambda *args: False)
        # This is the real loader eviction: the connected adapter must not import
        # the replacement generation's marker classes or delivery ContextVars.
        manager._load_directory_module(manifest, module_name=prefix)
        payload = snapshot_native(runner, event)
        assert payload['native_text_v1'].get('adapter_turn')
        restored = restore_native(payload, runner)
        key = adapter._source_session_key(restored.source)
        notes = await adapter.prepare_turn_notes(restored, 'reload-session', key, [])
        assert any('QQ表情能力/v1' in note for note in notes)
        bridge_adapter_turn(runner, restored, event)
        extracted = await adapter._extract_response_content(response, event, key,
                                                           is_ephemeral_response=False)
        requests, transport = wire
        async with httpx.AsyncClient(transport=transport) as client:
            adapter._http_client = client
            if extracted.text_content:
                result, _ = await adapter.send_final_ledgered(event, key, extracted.text_content, {},
                                                             reply_to=event.message_id)
                assert result.success
            await adapter._deliver_attachments(event, extracted, {}, anything_sent=bool(extracted.text_content),
                                               record_delivery=lambda result: None)
        assert [body['msg_type'] for _, body in requests] == ([2, 7] if extracted.text_content else [7])
        assert not {'content', 'markdown', 'caption'} & requests[-1][1].keys()
        receipts = old_receipts.receipt_journal(adapter, old_module.API_BASE).lookup_source(
            chat_id=chat, source_session_key=key, source_message_id=event.message_id)
        assert len(receipts) == len(requests)
        notes = await adapter.prepare_turn_notes(restored, 'reload-session', key, [])
        assert any('"outcome":"sent"' in note for note in notes)
    finally:
        _evict_modules(prefix)
