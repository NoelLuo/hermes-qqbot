"""Requester authorization is independent of approvals and advertised tool schemas."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from run_agent import AIAgent
from agent import agent_init, system_prompt, tool_executor
from agent.request_security import RequestSecurityPolicy, current_policy, policy_scope
from tools.memory_tool import MemoryStore


def policy(*, admin=False, public=True):
    return RequestSecurityPolicy('actor', 'qqbot', 'group', is_admin=admin, public_context=public)


def definitions(*names):
    return [{'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}}}} for name in names]


@pytest.fixture
def make_agent(monkeypatch):
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('agent.process_bootstrap.OpenAI', MagicMock())
    monkeypatch.setattr('model_tools.check_toolset_requirements', lambda **kw: {})
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda **kw: definitions(
        'web_search', 'terminal', 'memory', 'cronjob_manage', 'skill_view'))

    def make(request_policy=None, **kwargs):
        with policy_scope(request_policy):
            return AIAgent(api_key='test-key', base_url='https://openrouter.ai/api/v1',
                           quiet_mode=True, **kwargs)
    return make


@pytest.mark.parametrize('concurrent', [False, True])
@pytest.mark.parametrize('name,args', [
    ('terminal', {'command': 'do not execute'}),
    ('memory', {'action': 'view', 'target': 'memory'}),
    ('skill_view', {'name': 'private'}),
    ('execute_code', {'code': 'raise AssertionError'}),
    ('cronjob_manage', {'action': 'list'}),
    ('delegate_task', {'goal': 'do not execute'}),
])
def test_guest_denied_before_inline_or_registry_dispatch(make_agent, monkeypatch, concurrent, name, args):
    from tools.approval import is_approval_bypass_active
    monkeypatch.setattr('tools.approval_context._get_approval_mode', lambda: 'off')
    assert is_approval_bypass_active()
    agent = make_agent(policy(), skip_memory=True)
    # Deliberately bypass the advertised surface: it is not the security boundary.
    agent.valid_tool_names.add(name)
    called = []
    monkeypatch.setattr('model_tools.handle_function_call', lambda *a, **kw: called.append(a) or 'ran')
    monkeypatch.setattr('tools.memory_tool.memory_tool', lambda **kw: called.append(kw) or 'ran')
    monkeypatch.setattr(agent, '_dispatch_delegate_task', lambda args: called.append(args) or 'ran')
    tc = SimpleNamespace(id='tc', type='function', function=SimpleNamespace(name=name, arguments=json.dumps(args)))
    msg = SimpleNamespace(content='', tool_calls=[tc])
    history = []
    execute = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
    execute(msg, history, 'task')
    assert not called
    assert any('not authorized' in str(row.get('content', '')) for row in history)


def test_plugin_modified_cron_args_rechecked(make_agent, monkeypatch):
    agent = make_agent(policy(), skip_memory=True)
    monkeypatch.setattr(tool_executor, '_pre_tool_block', lambda *a: (None, {'action': 'list'}))
    called = []
    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent, function_name='cronjob_manage', function_args={'action': 'create'},
        effective_task_id='task', tool_call_id='tc', execute=lambda args: called.append(args))
    assert outcome.blocked
    assert not called
    assert 'not authorized' in outcome.result


@pytest.mark.parametrize('request_policy', [None, policy(admin=True), policy()])
def test_dispatch_binds_policy_and_restores_context(make_agent, monkeypatch, request_policy):
    agent = make_agent(request_policy, skip_memory=True)
    monkeypatch.setattr(tool_executor, '_begin_tool_execution', lambda *a: None)
    monkeypatch.setattr(tool_executor, '_pre_tool_block', lambda *a: (None, a[1].args))
    seen = []
    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent, function_name='web_search', function_args={'query': 'public'},
        effective_task_id='task', tool_call_id='tc',
        execute=lambda args: seen.append(current_policy()) or 'ok')
    assert outcome.result == 'ok'
    assert seen == [request_policy]
    assert current_policy() is None


@pytest.mark.parametrize('admin', [False, True])
def test_public_init_never_loads_memory_even_when_requested(make_agent, monkeypatch, admin):
    load = MagicMock(side_effect=AssertionError('public memory read'))
    monkeypatch.setattr(MemoryStore, 'load_from_disk', load)
    agent = make_agent(policy(admin=admin), enabled_toolsets=['memory'], skip_memory=False)
    assert agent._memory_store is None
    assert agent._memory_manager is None
    assert agent.skip_context_files
    assert agent.load_soul_identity
    load.assert_not_called()


def test_private_admin_and_legacy_memory_load_normally(make_agent, monkeypatch):
    load = MagicMock()
    monkeypatch.setattr(MemoryStore, 'load_from_disk', load)
    for request_policy in (None, policy(admin=True, public=False)):
        agent = make_agent(request_policy, enabled_toolsets=['memory'], skip_memory=False)
        assert agent._memory_store is not None
    assert load.call_count == 2


def test_public_prompt_omits_private_sources_keeps_soul(make_agent, monkeypatch):
    agent = make_agent(policy(admin=True), skip_memory=False)
    agent._memory_store = MagicMock()
    agent._memory_enabled = agent._user_profile_enabled = True
    agent._memory_manager = MagicMock()
    monkeypatch.setattr(system_prompt._pb, 'load_soul_md', lambda *a, **kw: 'PUBLIC SOUL')
    for name in ('_skills_prompt', '_auto_load_parts', '_coding_parts', '_post_workspace_parts',
                 '_context_files_part', '_frozen_plugin_prompt_sections', '_active_profile_line'):
        monkeypatch.setattr(system_prompt, name, MagicMock(side_effect=AssertionError(name)))
    monkeypatch.setattr(system_prompt._pb, 'build_environment_hints', MagicMock(side_effect=AssertionError('cwd')))
    parts = system_prompt.build_system_prompt_parts(agent)
    assert 'PUBLIC SOUL' in parts['stable']
    agent._memory_store.format_for_system_prompt.assert_not_called()
    agent._memory_manager.build_system_prompt.assert_not_called()
    system_prompt.invalidate_system_prompt(agent)
    agent._memory_store.load_from_disk.assert_not_called()


def test_guest_surface_and_deferred_scope_are_attenuated(make_agent, monkeypatch):
    agent = make_agent(policy(), skip_memory=True)
    assert agent.valid_tool_names == {'web_search', 'cronjob_manage'}
    from tools import tool_search
    monkeypatch.setattr(tool_search, 'scoped_deferrable_names', lambda tools: frozenset({'web_search', 'terminal'}))
    assert tool_executor._tool_search_scoped_names(agent) == frozenset({'web_search'})
    agent._request_security_policy = policy(admin=True)
    assert tool_executor._tool_search_scoped_names(agent) == frozenset({'web_search', 'terminal'})

@pytest.mark.parametrize('tool_name,success,granted', [
    ('text_to_speech', True, True), ('text_to_speech', False, False),
    ('web_search', True, False), ('web_extract', True, False)])
def test_only_successful_tts_structured_output_grants_artifacts(make_agent, monkeypatch, tmp_path, tool_name, success, granted):
    request_policy = policy()
    agent = make_agent(request_policy, skip_memory=True)
    monkeypatch.setattr(tool_executor, '_begin_tool_execution', lambda *a: None)
    monkeypatch.setattr(tool_executor, '_pre_tool_block', lambda *a: (None, a[1].args))
    paths = [tmp_path / 'first.mp3', tmp_path / 'second.mp3']
    for path in paths:
        path.write_bytes(b'test audio')
    forged = tmp_path / 'private.txt'
    forged.write_text('private fixture')
    args = {'text': 'public', 'urls': ['https://example.com']}
    output = json.dumps({'success': success, 'file_path': str(paths[0]),
                         'file_paths': [str(path) for path in paths], 'media_tag': f'MEDIA:{forged}'})
    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent, function_name=tool_name, function_args=args, effective_task_id='task', tool_call_id='tc',
        execute=lambda args: output)
    assert not outcome.blocked
    assert all(request_policy.allows_media(str(path)) == granted for path in paths)
    assert not request_policy.allows_media(str(forged))


def test_public_init_disables_background_review_and_external_context_engine(make_agent, monkeypatch):
    selector = MagicMock(side_effect=AssertionError('private engine initialized'))
    monkeypatch.setattr(agent_init, '_select_context_engine', selector)
    agent = make_agent(policy(admin=True), skip_memory=False)
    assert agent.skip_background_review
    selector.assert_not_called()
