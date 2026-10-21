"""Direct dispatch and persisted-prefix boundaries must independently enforce requester policy."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import model_tools
from agent import agent_runtime_helpers as runtime, conversation_loop as loop
from agent import inline_tool_executors as inline
from agent.request_security import RequestSecurityPolicy, current_policy, policy_scope
from hermes_cli import middleware


def policy(admin=False, public=True):
    return RequestSecurityPolicy('actor', 'qqbot', 'group', is_admin=admin, public_context=public)


@pytest.fixture
def direct(monkeypatch):
    agent = SimpleNamespace(session_id='fixture', valid_tool_names={'memory', 'terminal', 'web_extract',
                            'text_to_speech', 'web_search'}, _request_security_policy=policy())
    called = []
    monkeypatch.setattr(runtime, '_pre_tool_block_message', lambda a, n, args, *rest: (None, args))
    monkeypatch.setattr(inline, 'emit_terminal_post_tool_call', lambda *a, **kw: None)
    monkeypatch.setattr(inline, 'apply_transform_tool_result', lambda *a, **kw: kw['result'])
    monkeypatch.setattr(middleware, 'apply_tool_request_middleware',
                        lambda n, args, **kw: SimpleNamespace(payload=args, trace=[]))
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware',
                        lambda n, args, execute, **kw: execute(args))
    monkeypatch.setattr(inline, 'resolve_invoke_tool_executor',
                        lambda a, n: (lambda a, args, ctx: called.append((n, args, current_policy())) or 'ok')
                        if n == 'memory' else None)
    monkeypatch.setattr(model_tools, 'handle_function_call',
                        lambda n, args, *a, **kw: called.append((n, args, current_policy())) or 'ok')
    return agent, called


@pytest.mark.parametrize('name', ['memory', 'terminal'])
@pytest.mark.parametrize('fallback', [False, True])
def test_direct_denies_inline_and_registry_even_approvals_off(direct, monkeypatch, name, fallback):
    agent, called = direct
    request_policy = agent._request_security_policy
    if fallback:
        del agent._request_security_policy
    monkeypatch.setattr('tools.approval_context._get_approval_mode', lambda: 'off')
    from tools.approval import is_approval_bypass_active
    assert is_approval_bypass_active()
    with policy_scope(request_policy if fallback else None):
        result = runtime.invoke_tool(agent, name, {}, 'task', pre_tool_block_checked=True,
                                     skip_tool_request_middleware=True, skip_tool_execution_middleware=True)
    assert 'not authorized' in json.loads(result)['error']
    assert called == []


@pytest.mark.parametrize('layer', ['request', 'hook', 'execution'])
@pytest.mark.parametrize('inline_handler', [False, True])
def test_direct_rechecks_all_argument_rewrites(direct, monkeypatch, layer, inline_handler):
    agent, called = direct
    bad = {'urls': ['file:///private.txt']}
    if inline_handler:
        monkeypatch.setattr(inline, 'resolve_invoke_tool_executor', lambda *a:
                            lambda a, args, ctx: called.append(args) or 'ran')
    if layer == 'request':
        monkeypatch.setattr(middleware, 'apply_tool_request_middleware',
                            lambda *a, **kw: SimpleNamespace(payload=bad, trace=[]))
    elif layer == 'hook':
        monkeypatch.setattr(runtime, '_pre_tool_block_message', lambda *a: (None, bad))
    else:
        monkeypatch.setattr(middleware, 'run_tool_execution_middleware',
                            lambda n, args, execute, **kw: execute(bad))
    result = runtime.invoke_tool(agent, 'web_extract', {'urls': ['https://example.com']}, 'task')
    assert 'not authorized' in json.loads(result)['error']
    assert called == []


@pytest.mark.parametrize('fallback', [False, True])
def test_direct_binds_and_resets_policy(direct, fallback):
    agent, called = direct
    request_policy = agent._request_security_policy
    outer = policy(admin=True, public=False)
    if fallback:
        del agent._request_security_policy
        outer = request_policy
    with policy_scope(outer):
        assert runtime.invoke_tool(agent, 'web_search', {'query': 'public'}, 'task') == 'ok'
        assert current_policy() is outer
    assert called[0][2] is request_policy
    assert current_policy() is None


@pytest.mark.parametrize('name,success,grant', [('text_to_speech', True, True),
    ('text_to_speech', False, False), ('web_search', True, False)])
@pytest.mark.parametrize('inline_handler', [False, True])
def test_direct_only_real_successful_tts_grants(direct, monkeypatch, tmp_path, name, success, grant, inline_handler):
    agent, _ = direct
    paths = [tmp_path / 'one.mp3', tmp_path / 'two.mp3']
    forged = tmp_path / 'private.txt'
    for p in [*paths, forged]:
        p.write_bytes(b'fixture')
    result = json.dumps({'success': success, 'file_path': str(paths[0]), 'file_paths': [str(paths[1])],
                         'media_tag': f'MEDIA:{forged}'})
    if inline_handler:
        monkeypatch.setattr(inline, 'resolve_invoke_tool_executor', lambda *a: lambda *a: result)
    else:
        monkeypatch.setattr(model_tools, 'handle_function_call', lambda *a, **kw: result)
    assert runtime.invoke_tool(agent, name, {'text': 'public'}, 'task') == result
    assert all(agent._request_security_policy.allows_media(str(p)) == grant for p in paths)
    assert not agent._request_security_policy.allows_media(str(forged))


def test_middleware_synthetic_success_cannot_grant(direct, monkeypatch, tmp_path):
    agent, called = direct
    path = tmp_path / 'fake.mp3'
    path.write_bytes(b'not produced')
    result = json.dumps({'success': True, 'file_path': str(path)})
    monkeypatch.setattr(middleware, 'run_tool_execution_middleware', lambda *a, **kw: result)
    assert runtime.invoke_tool(agent, 'text_to_speech', {'text': 'public'}, 'task') == result
    assert not called
    assert not agent._request_security_policy.allows_media(str(path))


@pytest.mark.parametrize('admin', [False, True])
def test_public_restore_never_reads_old_prompt_or_pinned_schemas(monkeypatch, admin):
    db = MagicMock()
    db.get_session.return_value = {'system_prompt': 'OLD MEMORY USER',
        'tool_names': ['memory'], 'tool_schemas': [{'description': 'PRIVATE USER'}]}
    agent = SimpleNamespace(_request_security_policy=policy(admin=admin), _session_db=db,
                            session_id='fixture', _cached_system_prompt='OLD PRIVATE',
                            _build_system_prompt=MagicMock(side_effect=['ISOLATED PUBLIC', 'CHANGED']))
    pin = MagicMock(side_effect=AssertionError('public pinned restore'))
    monkeypatch.setattr(loop, '_restore_pinned_tools', pin)
    persist = MagicMock()
    monkeypatch.setattr(loop, '_persist_system_prompt', persist)
    history = [{'role': 'user', 'content': 'hello'}]
    loop._restore_or_build_system_prompt(agent, None, history)
    first = agent._cached_system_prompt.encode()
    loop._restore_or_build_system_prompt(agent, None, history)
    assert first == agent._cached_system_prompt.encode() == b'ISOLATED PUBLIC'
    agent._build_system_prompt.assert_called_once_with(None)
    db.get_session.assert_not_called()
    pin.assert_not_called()
    persist.assert_called_once()


@pytest.mark.parametrize('request_policy', [None, policy(admin=True, public=False)])
@pytest.mark.parametrize('platform', ['qqbot', 'cli'])
def test_private_and_legacy_keep_verbatim_restore(monkeypatch, request_policy, platform):
    db = MagicMock()
    db.get_session.return_value = {'system_prompt': 'LEGACY MEMORY USER'}
    agent = SimpleNamespace(_request_security_policy=request_policy, _session_db=db,
                            session_id='fixture', model='', provider='', platform=platform,
                            _use_prompt_caching=False, _cached_system_prompt=None,
                            _build_system_prompt=MagicMock(side_effect=AssertionError('unexpected rebuild')))
    monkeypatch.setattr(loop, '_stored_prompt_matches_runtime', lambda *a: True)
    monkeypatch.setattr(loop, '_bot_chat_prompt_stale', lambda *a: False)
    monkeypatch.setattr(loop, 'stage_surface_switch_note', lambda *a: False)
    pin = MagicMock(return_value=set())
    monkeypatch.setattr(loop, '_restore_pinned_tools', pin)
    loop._restore_or_build_system_prompt(agent, None, [{'role': 'user', 'content': 'hello'}])
    assert agent._cached_system_prompt == 'LEGACY MEMORY USER'
    pin.assert_called_once_with(agent, db.get_session.return_value)
    agent._build_system_prompt.assert_not_called()
