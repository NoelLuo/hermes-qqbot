"""Registry/handler/ledger integration, using only an isolated temporary profile."""
import json
from types import SimpleNamespace

import pytest

import model_tools
from agent.agent_init import _load_tools
from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from tools.guest_workspace_interface import SCHEMA, register
from tools.registry import registry


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    scope = registry.current_scope_key()
    previous = registry.snapshot_registration('guest_workspace', scope=scope)

    class Context:
        def register_tool(self, **kwargs):
            registry.register(**kwargs, scope=scope)

    register(Context())
    current = registry.snapshot_registration('guest_workspace', scope=scope)
    yield
    registry.restore_registration('guest_workspace', current, previous, scope=scope)


def policy(subject='visitor', chat='group'):
    return RequestSecurityPolicy(subject, 'qqbot', chat)


def call(policy_value, **args):
    with policy_scope(policy_value):
        result = model_tools.handle_function_call('guest_workspace', args)
    return json.loads(result)


def test_registry_real_document_sort_export_and_request_ledger(registered):
    p = policy()
    agent = SimpleNamespace(quiet_mode=True, platform='qqbot', _request_security_policy=p)
    _load_tools(agent, ['guest_workspace'], None)
    assert agent.valid_tool_names == {'guest_workspace'}
    assert call(p, action='write', path='input.csv', text='name,value\nb,2\na,1\na,1\n')['success']
    transformed = call(p, action='organize_csv', path='input.csv', destination='sorted.csv',
                       sort_by=['name'], dedupe_by=['name', 'value'])
    assert transformed['success'] and transformed['rows'] == 2
    assert call(p, action='read', path='sorted.csv')['text'] == 'name,value\na,1\nb,2\n'
    exported = call(p, action='export', path='sorted.csv')
    assert exported['success'] and p.allows_media(exported['file_path'])
    assert exported['media'] == 'MEDIA:' + exported['file_path']
    assert not policy().allows_media(exported['file_path'])
    assert not call(policy('other'), action='read', path='sorted.csv')['success']
    assert not call(policy(chat='other'), action='read', path='sorted.csv')['success']
    assert not call(p, action='read', path='../config.yaml')['success']


def test_handler_rejects_missing_or_restricted_requester_before_storage(registered):
    before = get_hermes_home() / 'workspaces'
    assert not call(None, action='info')['success']
    p = RequestSecurityPolicy('visitor', 'qqbot', 'group', allowed_tools=frozenset({'web_search'}))
    assert not call(p, action='info')['success']
    assert not before.exists()


def test_schema_fields_can_drive_real_backend_without_undocumented_write_args(registered):
    p = policy()
    assert 'text' in SCHEMA['parameters']['properties']
    assert 'dedupe_by' in SCHEMA['parameters']['properties']
    assert call(p, action='mkdir', path='notes')['success']
    assert call(p, action='write', path='notes/a.txt', text='hello')['success']
    assert call(p, action='copy', path='notes/a.txt', destination='b.txt')['success']
    assert call(p, action='move', path='b.txt', destination='c.txt')['success']
    assert call(p, action='search', query='hello')['matches']
    assert not call(p, action='write', path='d.txt', text='x', root='C:/')['success']
    assert not call(p, action='run_python', code='pass', image='arbitrary')['success']


def test_attachment_import_is_transport_grant_only(registered, tmp_path):
    p = policy()
    source = tmp_path / 'uploaded.txt'
    source.write_text('uploaded harmless fixture', encoding='utf-8')
    assert not call(p, action='import_attachment', source=str(source), path='upload.txt')['success']
    assert p.grant_artifact(str(source))
    assert call(p, action='import_attachment', source=str(source), path='upload.txt')['success']
    assert call(p, action='read', path='upload.txt')['text'] == 'uploaded harmless fixture'
    assert not call(policy(), action='import_attachment', source=str(source), path='other.txt')['success']
