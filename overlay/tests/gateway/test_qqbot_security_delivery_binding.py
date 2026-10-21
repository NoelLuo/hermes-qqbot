"""Real renderer/base delivery integration with synthetic requests and mocked transport."""
import asyncio
from concurrent.futures import Future
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from agent.request_security import policy_scope
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.request_security import policy_for_adapter
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from plugins.platforms.qqbot.adapter import QQAdapter, MEDIA_TYPE_IMAGE

EXTRA = {'role_policy': {'enabled': True}, 'group_allow_admin_from': ['owner'], 'allow_admin_from': ['owner']}
KEY = 'agent:main:qqbot:group:room'


def fixture_adapter():
    obj = QQAdapter.__new__(QQAdapter)
    obj.config = NS(extra=EXTRA)
    obj.platform = Platform.QQBOT
    obj._chat_type_map = {'room': 'group'}
    obj._last_msg_id = {}
    obj.send = AsyncMock(return_value=SendResult(success=True, message_id='text-card'))
    obj.send_with_keyboard = AsyncMock(return_value=SendResult(success=True, message_id='button-card'))
    obj.pause_typing_for_chat = Mock()
    return obj


def fixture_event():
    return MessageEvent(text='public question', message_type=MessageType.TEXT, source=SessionSource(
        platform=Platform.QQBOT, chat_id='room', chat_type='group', user_id='visitor'))


def fixture_turn(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = NS(platforms={Platform.QQBOT: NS(extra=EXTRA)})
    ctx = TurnContext(source=SessionSource(platform=Platform.QQBOT, chat_id='room', chat_type='group', user_id='owner'),
        _status_adapter=adapter, _status_chat_id='room', _status_thread_metadata={}, session_key=KEY)
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner, turn._ctx = runner, ctx
    turn._close_native_stream_boundary = Mock()
    def schedule(coro, label):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future
    turn._schedule = schedule
    return turn


def test_renderer_carries_original_request_id_to_real_qq_card(monkeypatch):
    from gateway import run_turn_runner_approval_settle
    monkeypatch.setattr(run_turn_runner_approval_settle, 'register_timeout_notice', Mock())
    adapter = fixture_adapter()
    turn = fixture_turn(adapter)
    turn._render_approval_sync({'command': 'synthetic operation', 'description': 'synthetic approval', 'request_id': 'original-request'})
    keyboard = adapter.send_with_keyboard.call_args.args[2]
    assert all(button.action.data.startswith('approve-request:original-request:')
        for row in keyboard.content.rows for button in row.buttons)
    adapter.send.assert_not_awaited()


def test_text_fallback_includes_exact_request_not_fifo_hint(monkeypatch):
    from gateway import run_turn_runner_approval_settle
    monkeypatch.setattr(run_turn_runner_approval_settle, 'register_timeout_notice', Mock())
    adapter = fixture_adapter()
    adapter.send_with_keyboard.return_value = SendResult(success=False, error='synthetic unavailable')
    fixture_turn(adapter)._render_approval_sync({'command': 'synthetic', 'request_id': 'exact-request'})
    text = adapter.send.call_args.args[1]
    assert '/approve request:exact-request' in text and '/deny request:exact-request' in text


def test_missing_request_identity_never_falls_back_to_unbound_approval(monkeypatch):
    from gateway import run_turn_runner_approval_settle
    monkeypatch.setattr(run_turn_runner_approval_settle, 'register_timeout_notice', Mock())
    adapter = fixture_adapter()
    with pytest.raises(RuntimeError, match='identity unavailable'):
        fixture_turn(adapter)._render_approval_sync({'command': 'synthetic'})
    adapter.send.assert_not_awaited()
    adapter.send_with_keyboard.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_transport_receipt_granted_only_at_final_ingress(tmp_path, monkeypatch):
    from gateway.inbound_attachment_proofs import record_attachment
    from gateway.request_security import grant_verified_event_attachments
    monkeypatch.setattr('gateway.platforms.base.get_image_cache_dir', lambda: tmp_path)
    image, private = tmp_path / 'public.png', tmp_path / 'private.png'
    image.write_bytes(b'public')
    private.write_bytes(b'private')
    adapter = fixture_adapter()
    adapter._message_handler = Mock()
    adapter._drop_unresolved = Mock(return_value=False)
    adapter._event_session_key = Mock(return_value=KEY)
    adapter._active_sessions = {}
    adapter._shared_burst_pending = Mock(return_value=False)
    adapter._start_session_processing = Mock(return_value=True)
    event = fixture_event()
    event.message_id = 'trusted-transport-request'
    event.text = f'MEDIA:{private}'
    event.media_urls = [str(image)]
    receipt = record_attachment(event, str(image), 'image/png')
    assert receipt is not None
    event._inbound_attachment_receipts = [(receipt, 0)]
    await adapter.handle_message(event)
    policy = policy_for_adapter(adapter, event.source)
    assert not policy.allows_media(image)
    runner = NS(config=NS(platforms={Platform.QQBOT: NS(extra=EXTRA)}))
    assert grant_verified_event_attachments(runner, event) == [str(image.resolve())]
    assert policy.allows_media(image)
    assert not policy.allows_media(private)
    assert event._gateway_accepted


@pytest.mark.asyncio
async def test_media_delivery_blocks_private_and_retains_this_requests_voice_and_image(tmp_path):
    image, voice, private = (tmp_path / name for name in ['public image 中文.png', 'voice.mp3', 'private.mp3'])
    for file in (image, voice, private):
        file.write_bytes(b'synthetic media')
    adapter = fixture_adapter()
    adapter.send_voice = AsyncMock(return_value=SendResult(success=True))
    adapter.send_image_file = AsyncMock(return_value=SendResult(success=True))
    event = fixture_event()
    policy = policy_for_adapter(adapter, event.source)
    assert policy.grant_artifact(image) and policy.grant_artifact(voice)
    results = []
    await adapter._deliver_media_attachments(event, [(str(image), False), (str(voice), True), (str(private), True)], [],
        force_document_attachments=False, human_delay=0, metadata={}, record_delivery=results.append)
    adapter.send_voice.assert_awaited_once()
    assert adapter.send_voice.call_args.kwargs['audio_path'] == str(voice)
    adapter.send_image_file.assert_awaited_once()
    assert adapter.send_image_file.call_args.kwargs['image_path'] == str(image)
    assert len(results) == 2 and all(result.success for result in results)


@pytest.mark.asyncio
async def test_qq_native_second_gate_rejects_cross_chat_and_private_url_before_connect(tmp_path):
    adapter = fixture_adapter()
    adapter._ensure_connected = AsyncMock(return_value=False)
    policy = policy_for_adapter(adapter, fixture_event().source)
    image = tmp_path / 'public.png'
    image.write_bytes(b'public')
    assert policy.grant_artifact(image)
    with policy_scope(policy):
        wrong_chat = await adapter._send_media('different-room', str(image), MEDIA_TYPE_IMAGE, 'image')
        private_url = await adapter._send_media('room', 'http://127.0.0.1/private', MEDIA_TYPE_IMAGE, 'image')
        private_file = await adapter._send_media('room', str(tmp_path / 'private.png'), MEDIA_TYPE_IMAGE, 'image')
        assert not wrong_chat.success and not private_url.success and not private_file.success
        adapter._ensure_connected.assert_not_awaited()
        await adapter._send_media('room', str(image), MEDIA_TYPE_IMAGE, 'image')
        adapter._ensure_connected.assert_awaited_once()
