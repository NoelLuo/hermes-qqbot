"""Blocked expression-only finals are handled without publishing a fallback."""
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.platforms.base import SendResult
from plugins.platforms.qqbot.memes import controller_for
from tests.plugins.platforms.test_qqbot_memes import bot, wire, make_event
from tests.plugins.platforms.test_qqbot_meme_expression import expression_bot, prepare, TAG


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['disabled', 'invalid', 'context', 'snapshot', 'unknown', 'chained'])
@pytest.mark.parametrize('text', ['', '正文保留。\n'])
@pytest.mark.parametrize('queued', [False, True, 'native'])
async def test_failed_expression_is_silent_without_text_and_terminal(
        expression_bot, wire, monkeypatch, failure, text, queued):
    adapter = expression_bot
    ctrl = controller_for(adapter)
    event = make_event(adapter)
    key, _ = await prepare(adapter, event)
    tag = TAG
    if failure == 'disabled':
        ctrl.settings = replace(ctrl.settings, probability=0)
    elif failure == 'invalid':
        tag = '[[qq_meme:unknown]]'
    elif failure == 'context':
        del event._qq_expression_context
    elif failure == 'snapshot':
        from plugins.platforms.qqbot.meme_catalog import MemeCatalog
        monkeypatch.setattr(MemeCatalog, 'snapshot', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    elif failure == 'unknown':
        adapter.send_image_file = AsyncMock(return_value=SendResult(success=False, error='ACK lost'))
    elif failure == 'chained':
        event._adapter_chained_response = True
    response = text + tag
    adapter._message_handler = AsyncMock(return_value=response)
    adapter._run_processing_hook = AsyncMock()
    adapter._start_typing_refresh = lambda *a, **kw: None
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        adapter._http_client = client
        if queued == 'native':
            from gateway.session_ingress import deliver_response
            await deliver_response(adapter, event, key, response)
        elif queued:
            assert await adapter.deliver_queued_response(response, event, key) is True
        else:
            await adapter._process_message_background(event, key)
        # Repeat preparation AND attachment delivery: neither can reopen an attempted GIF.
        extracted = await adapter._extract_response_content(response, event, key, is_ephemeral_response=False)
        assert extracted.text_content == text.strip()
        await adapter._deliver_attachments(event, extracted, {}, anything_sent=bool(text), record_delivery=lambda r: None)
    assert [body['msg_type'] for _, body in requests] == ([2] if text else [])
    if failure == 'unknown':
        adapter.send_image_file.assert_awaited_once()
    else:
        adapter._upload_local_file.assert_not_awaited()
    if failure not in ('context', 'chained'):
        with sqlite3.connect(ctrl.state.path) as db:
            rows = db.execute('SELECT outcome FROM expressions').fetchall()
        assert rows == [('unknown' if failure == 'unknown' else 'error' if failure == 'snapshot' else 'blocked',)]
    assert not list((ctrl.home / 'cache/qqbot/meme_snapshots').glob('*.gif'))
