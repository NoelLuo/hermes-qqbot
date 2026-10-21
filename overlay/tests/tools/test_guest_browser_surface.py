"""Frontend permission/registry/store contracts; browser output is a deliberate unit fixture."""
import json
import sys
from types import SimpleNamespace

import pytest

import model_tools
from agent.agent_init import _load_tools
from agent.request_security import RequestSecurityPolicy, policy_scope
from agent.request_workspace_guidance import workspace_request_guidance
from tools import guest_browser_service as service
from tools.guest_browser_interface import register
from tools.guest_workspace_interface import register as register_workspace
from tools.registry import registry


def guest(**changes):
    values = dict(subject_id='visitor', platform='qqbot', chat_id='room')
    values.update(changes)
    return RequestSecurityPolicy(**values)


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    scope = registry.current_scope_key()
    names = ['guest_workspace', 'guest_browser']
    before = {name: registry.snapshot_registration(name, scope=scope) for name in names}

    class Context:
        def register_tool(self, **kwargs):
            registry.register(**kwargs, scope=scope)

    register(Context())
    register_workspace(Context())
    current = {name: registry.snapshot_registration(name, scope=scope) for name in names}
    yield
    for name in names:
        registry.restore_registration(name, current[name], before[name], scope=scope)


def call(policy, name='guest_browser', **args):
    with policy_scope(policy):
        return json.loads(model_tools.handle_function_call(name, args))


def test_real_guest_assembly_has_structured_browser_not_host_code(registered):
    p = guest()
    agent = SimpleNamespace(quiet_mode=True, platform='qqbot', _request_security_policy=p)
    _load_tools(agent, ['guest_browser', 'guest_workspace', 'browser', 'terminal'], None)
    assert {'guest_browser', 'guest_workspace'} <= agent.valid_tool_names
    assert not {'browser_exec', 'terminal', 'execute_code', 'tool_search', 'tool_call'} & agent.valid_tool_names
    schemas = {row['function']['name']: row['function'] for row in agent.tools}
    assert schemas['guest_browser']['parameters']['additionalProperties'] is False
    assert 'code' not in schemas['guest_browser']['parameters']['properties']
    assert call(p, action='info')['success']


@pytest.mark.parametrize('args', [None, {}, {'action': []}, {'action': 'execute'}, {'action': 'login'}, {'action': 'upload'}])
def test_policy_rejects_non_browser_operations(args):
    assert not guest().allows_tool('guest_browser', args)


@pytest.mark.parametrize('args', [
    {'action': 'browse', 'url': 'file:///C:/private.txt'},
    {'action': 'browse', 'url': 'http://127.0.0.1/'},
    {'action': 'browse', 'url': 'http://host.docker.internal/'},
    {'action': 'browse', 'url': 'https://example.com:444/'},
    {'action': 'browse', 'url': 'https://example.com/', 'image': 'override'},
    {'action': 'browse', 'url': 'https://example.com/', 'code': 'import os'},
    {'action': 'browse', 'url': 'https://example.com/', 'actions': [{'kind': 'evaluate', 'code': 'x'}]},
    {'action': 'browse', 'url': 'https://example.com/', 'actions': [{'kind': 'click_link', 'index': True}]},
    {'action': 'browse', 'url': 'https://example.com/', 'actions': [{'kind': 'download', 'url': 'https://example.com/x', 'name': '../x.txt'}]},
    {'action': 'browse', 'url': 'https://example.com/', 'actions': [{'kind': 'download', 'url': 'https://example.com/x', 'name': 'x.exe'}]},
    {'action': 'info', 'url': 'https://example.com/'},
])
def test_bad_arguments_refused_before_image_or_docker_lookup(registered, monkeypatch, args):
    monkeypatch.setattr(service, '_configured_image', lambda: pytest.fail('must not look up runtime'))
    assert not call(guest(), **args)['success']


def test_missing_or_wrong_request_context_cannot_reach_browser(registered, monkeypatch):
    monkeypatch.setattr(service, '_configured_image', lambda: pytest.fail('must not look up runtime'))
    assert not call(None, action='info')['success']
    restricted = guest(allowed_tools=frozenset({'web_search'}))
    assert not call(restricted, action='browse', url='https://example.com/')['success']
    p = guest()
    with policy_scope(guest(subject_id='other')):
        assert not service.execute_browser({'action': 'info'}, p)['success']


def test_browser_fixture_is_saved_in_real_scoped_store_and_export_ledger(registered, monkeypatch):
    fixture = {'success': True, 'title': 'Unit fixture', 'url': 'https://example.com/',
               'text': 'Fixture page', 'links': [{'text': 'next', 'url': 'https://example.com/next'}],
               'artifacts': {'table.csv': b'n\n1\n'}}
    calls = []

    def driver(url, actions, *, image, timeout):
        calls.append((url, actions, image, timeout))
        return fixture

    monkeypatch.setattr(service, '_configured_image', lambda: 'sha256:' + 'a' * 64)
    monkeypatch.setitem(sys.modules, 'tools.guest_browser_docker', SimpleNamespace(run_browser_sandbox=driver))
    p = guest()
    result = call(p, action='browse', url='https://example.com/', actions=[{'kind': 'read'}])
    assert result['success'] and calls
    path = result['files'][0]['path']
    assert path.startswith('browser/')
    assert call(p, 'guest_workspace', action='read', path=path)['text'] == 'n\n1\n'
    assert not call(guest(subject_id='other'), 'guest_workspace', action='read', path=path)['success']
    assert not call(guest(chat_id='other'), 'guest_workspace', action='read', path=path)['success']
    export = call(p, 'guest_workspace', action='export', path=path)
    assert export['success'] and p.allows_media(export['file_path'])
    assert not guest().allows_media(export['file_path'])
    assert result['links'][0]['index'] == 0


def test_browser_prompt_only_explains_existing_visible_grant():
    p = guest(allowed_tools=frozenset({'guest_browser'}))
    agent = SimpleNamespace(_request_security_policy=p, valid_tool_names={'guest_browser'})
    text = '\n'.join(workspace_request_guidance(agent))
    assert '已授权的访客公开浏览器' in text and '真实用户标识' not in text
    agent.valid_tool_names = set()
    assert workspace_request_guidance(agent) == []
    agent.valid_tool_names = {'guest_browser'}
    agent._request_security_policy = guest(allowed_tools=frozenset({'web_search'}))
    assert workspace_request_guidance(agent) == []


def test_deletion_permission_requires_explicit_true_confirmation():
    p = guest()
    for value in (None, False, 1, 'true'):
        assert not p.allows_tool('guest_workspace', {'action': 'delete', 'confirm': value})
    assert p.allows_tool('guest_workspace', {'action': 'delete', 'confirm': True})
    assert p.allows_tool('guest_workspace', {'action': 'cleanup'})
