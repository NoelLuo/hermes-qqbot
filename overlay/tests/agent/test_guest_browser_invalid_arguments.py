"""Missing guest browser action is a validation error only after a real tool grant."""
import json
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from run_agent import AIAgent


@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('granted', [False, True])
def test_missing_action_never_executes_and_only_granted_tool_gets_hint(monkeypatch, concurrent, granted):
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('agent.process_bootstrap.OpenAI', MagicMock())
    monkeypatch.setattr('model_tools.check_toolset_requirements', lambda **kw: {})
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kw: [{
        'type': 'function', 'function': {'name': 'guest_browser', 'description': 'browser',
        'parameters': {'type': 'object', 'properties': {'action': {'type': 'string'}}}}}])
    policy = RequestSecurityPolicy('visitor', 'qqbot', 'room',
        allowed_tools=frozenset({'guest_browser'}) if granted else frozenset())
    with policy_scope(policy):
        agent = AIAgent(api_key='test-key', base_url='https://openrouter.ai/api/v1',
                        quiet_mode=True, skip_memory=True)
    agent.valid_tool_names.add('guest_browser')
    dispatch = MagicMock(side_effect=AssertionError('must not execute without action'))
    monkeypatch.setattr('model_tools.handle_function_call', dispatch)
    call = NS(id='tc', type='function', function=NS(name='guest_browser', arguments=json.dumps({
        'url': 'https://example.com', 'actions': [{'type': 'screenshot'}]})))
    history = []
    execute = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
    execute(NS(content='', tool_calls=[call]), history, 'task')
    dispatch.assert_not_called()
    results = '\n'.join(str(row.get('content', '')) for row in history)
    if granted:
        assert 'invalid_arguments' in results and 'action' in results and 'browse' in results
        assert 'not authorized' not in results
    else:
        assert 'not authorized' in results
        assert 'browse' not in results
    assert not policy.allows_tool('guest_browser', {'url': 'https://example.com'})
