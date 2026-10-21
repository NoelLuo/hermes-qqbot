"""Scoped workspace guidance explains granted work and reports real outcomes honestly."""
from types import SimpleNamespace

import pytest

from agent import system_prompt
from agent.request_security import RequestSecurityPolicy
from agent.request_workspace_guidance import workspace_request_guidance
from tests.agent.test_request_security import definitions, make_agent


def guest(**kwargs):
    values = {'subject_id': 'test-subject-not-for-prompt', 'platform': 'qqbot', 'chat_id': 'test-room'}
    values.update(kwargs)
    return RequestSecurityPolicy(**values)


@pytest.mark.parametrize('policy,names', [
    (None, {'guest_workspace'}),
    (guest(is_admin=True), {'guest_workspace'}),
    (guest(), set()),
    (guest(allowed_tools=frozenset({'web_search'})), {'guest_workspace'}),
    (SimpleNamespace(is_admin=False, public_context=True), {'guest_workspace'}),
])
def test_no_workspace_advertisement_without_both_grant_and_visible_schema(policy, names):
    assert workspace_request_guidance(SimpleNamespace(
        _request_security_policy=policy, valid_tool_names=names)) == []


def test_real_public_prompt_keeps_identity_and_explains_granted_workspace(make_agent, monkeypatch):
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kw: definitions(
        'guest_workspace', 'web_search', 'terminal'))
    agent = make_agent(guest(), skip_memory=False)
    assert agent.valid_tool_names == {'guest_workspace', 'web_search'}
    monkeypatch.setattr(system_prompt._pb, 'load_soul_md', lambda *a, **kw: 'PUBLIC IDENTITY FIXTURE')
    parts = system_prompt.build_system_prompt_parts(agent)
    stable = parts['stable']
    assert 'PUBLIC IDENTITY FIXTURE' in stable
    assert '已授权的访客工作区' in stable and 'guest_workspace' in stable
    assert 'success=true' in stable and '不得在已完成写入后' in stable
    assert '真实 SOUL/MEMORY/USER' in stable
    assert 'test-subject-not-for-prompt' not in stable
    assert agent._memory_store is None and agent._memory_manager is None
    assert not agent._request_security_policy.is_admin
    assert not agent._request_security_policy.allows_tool('terminal', {'command': 'echo no'})
    assert system_prompt.build_system_prompt_parts(agent)['stable'] == stable
