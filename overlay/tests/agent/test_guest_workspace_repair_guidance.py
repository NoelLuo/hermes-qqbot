"""Granted-tool guidance uses actual parameter shapes and separates storage from delivery."""
import json
import re
from types import SimpleNamespace

import pytest

from agent.request_security import RequestSecurityPolicy
from agent.request_workspace_guidance import workspace_request_guidance
from tools.guest_browser_service import _browse_arguments


def _agent(names, allowed=None, **flags):
    policy = RequestSecurityPolicy('fixture-owner-not-for-prompt', 'qqbot', 'fixture-room',
                                   allowed_tools=frozenset(allowed if allowed is not None else names), **flags)
    return SimpleNamespace(_request_security_policy=policy, valid_tool_names=set(names))


def test_browser_example_is_accepted_by_actual_structured_argument_validator():
    text = '\n'.join(workspace_request_guidance(_agent({'guest_browser'})))
    match = re.search(r'(\{"action":"browse","url":.*?"actions":\[.*?\]\})', text)
    assert match, text
    args = json.loads(match[1])
    url, actions = _browse_arguments(args)
    assert args['action'] == 'browse' and url.startswith('https://')
    assert actions == [{'kind': 'read'}, {'kind': 'screenshot'}]
    assert 'clarify' in text and '普通聊天' in text
    assert 'fixture-owner-not-for-prompt' not in text


def test_workspace_guidance_distinguishes_export_send_and_current_attachment_reference():
    text = '\n'.join(workspace_request_guidance(_agent({'guest_workspace'})))
    assert 'MEDIA:' in text and '发送前不能说对方已经收到' in text
    assert '重新 export' in text and '不能复用旧回合' in text
    assert '先发附件' in text and 'source 和本人工作区目标 path' in text
    assert '附近有其他人的图片' in text and '不等于可导入' in text
    assert 'deleted_files/skipped/invalid_metadata' in text


@pytest.mark.parametrize('names,allowed,flags', [
    (set(), {'guest_workspace', 'guest_browser'}, {}),
    ({'guest_browser'}, {'web_search'}, {}),
    ({'guest_workspace', 'guest_browser'}, None, {'is_admin': True}),
    ({'guest_workspace', 'guest_browser'}, None, {'is_admin': True, 'public_context': False}),
])
def test_repair_guidance_never_manufactures_a_grant(names, allowed, flags):
    assert workspace_request_guidance(_agent(names, allowed, **flags)) == []
