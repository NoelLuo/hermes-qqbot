"""Public reference preprocessing must never reach workspace/file/plugin expansion."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import session_context_refs as refs
from gateway.config import Platform
from gateway.session import SessionSource


@pytest.mark.asyncio
@pytest.mark.parametrize('user,kind', [('guest', 'group'), ('owner', 'group'), ('guest', 'dm')])
@pytest.mark.parametrize('text', ['@file:Documents/private.txt', '@folder:Documents', '@diff', '@git:HEAD', '@plugin:private'])
async def test_public_reference_stays_literal_before_any_io(monkeypatch, user, kind, text):
    extra = {'role_policy': {'enabled': True}, 'group_allow_admin_from': ['owner'], 'allow_admin_from': ['owner']}
    runner = NS(config=NS(platforms={Platform.QQBOT: NS(extra=extra)}),
        _inbound_model_context_length=AsyncMock(side_effect=AssertionError('no lookup')),
        _expand_inbound_context_references=AsyncMock(side_effect=AssertionError('no expansion')))
    source = SessionSource(platform=Platform.QQBOT, chat_id='room', user_id=user, chat_type=kind)
    monkeypatch.setattr(refs, '_inbound_cwd', Mock(side_effect=AssertionError('no workspace access')))
    monkeypatch.setattr(refs, 'expand_references', AsyncMock(side_effect=AssertionError('no file read')))
    assert await refs.inbound_references(runner, source, 'shared', text) == (text, None)
    assert await refs.turn_references(runner, NS(message_id='m'), source, 'shared', text) == text
    runner._expand_inbound_context_references.assert_not_awaited()
    runner._inbound_model_context_length.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,platform', [(True, Platform.QQBOT), (False, Platform.QQBOT), (True, Platform.LOCAL)])
async def test_private_admin_and_legacy_references_keep_existing_path(monkeypatch, enabled, platform):
    extra = {'role_policy': {'enabled': enabled}, 'allow_admin_from': ['owner']}
    runner = NS(config=NS(platforms={platform: NS(extra=extra)}),
        _inbound_model_context_length=AsyncMock(return_value=4096))
    source = SessionSource(platform=platform, chat_id='private', user_id='owner', chat_type='dm')
    monkeypatch.setattr(refs, '_inbound_cwd', lambda *args: 'synthetic-workspace')
    expand = AsyncMock(return_value=('expanded synthetic text', None))
    monkeypatch.setattr(refs, 'expand_references', expand)
    assert await refs.inbound_references(runner, source, 'private', '@file:public.txt') == ('expanded synthetic text', None)
    expand.assert_awaited_once()
