"""Office capability through real session schema, registry and store authorization."""
import json
from types import SimpleNamespace

import hermes_bootstrap
import pytest
import hermes_yaml as yaml

import model_tools
from agent.agent_init import _load_tools
from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from tools import guest_office_docker
from tools.guest_workspace_interface import register
from tools.registry import registry


@pytest.fixture
def registered(monkeypatch):
    monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'model': {'default': 'offline-test', 'context_length': 8192},
        'plugins': {'entries': {'guest-workspace': {'office_image': 'sha256:' + 'a' * 64}}},
    }), encoding='utf-8')
    scope = registry.current_scope_key()
    previous = registry.snapshot_registration('guest_workspace', scope=scope)

    class Context:
        def register_tool(self, **kwargs):
            registry.register(**kwargs, scope=scope)

    register(Context())
    current = registry.snapshot_registration('guest_workspace', scope=scope)
    yield
    registry.restore_registration('guest_workspace', current, previous, scope=scope)


def call(policy, **args):
    with policy_scope(policy):
        return json.loads(model_tools.handle_function_call('guest_workspace', args))


def test_model_schema_real_registry_office_store_and_private_tools_withheld(registered, monkeypatch):
    policy = RequestSecurityPolicy('visitor', 'qqbot', 'group')
    agent = SimpleNamespace(quiet_mode=True, platform='qqbot', _request_security_policy=policy)
    _load_tools(agent, ['guest_workspace', 'terminal', 'memory'], None)
    assert agent.valid_tool_names == {'guest_workspace'}
    schema = next(t['function'] for t in agent.tools if t['function']['name'] == 'guest_workspace')
    fields = schema['parameters']['properties']
    assert 'run_office' in fields['action']['enum']
    assert fields['command']['type'] == 'string' and fields['timeout']['maximum'] == 90
    assert fields['timeout']['minimum'] == 1 and not schema['parameters']['additionalProperties']
    assert 'image' not in fields
    captured = []

    def unit_runner(command, files, *, image, timeout):
        captured.append((command, files, image, timeout))
        return {'success': True, 'stdout': 'ok', 'stderr': '', 'errors': [], 'files': {'result.bin': b'\x00\xff'}}

    monkeypatch.setattr(guest_office_docker, 'run_office_sandbox', unit_runner)
    result = call(policy, action='run_office', command='make-output', inputs=[], timeout=90)
    assert result['success'] and result['output_files'] == ['result.bin']
    assert captured == [('make-output', {}, 'sha256:' + 'a' * 64, 90)]
    exported = call(policy, action='export', path='result.bin')
    assert exported['success'] and policy.allows_media(exported['file_path'])
    assert not RequestSecurityPolicy('other', 'qqbot', 'group').allows_media(exported['file_path'])
    assert not call(policy, action='run_office', command='id', image='arbitrary')['success']
    assert len(captured) == 1


def test_office_registry_denies_missing_and_withheld_policies_before_io(registered):
    target = get_hermes_home() / 'workspaces'
    assert not call(None, action='run_office', command='id')['success']
    restricted = RequestSecurityPolicy('visitor', 'qqbot', 'group', allowed_tools=frozenset({'web_search'}))
    assert not call(restricted, action='run_office', command='id')['success']
    assert not target.exists()


def test_public_gate_durable_office_grant_never_grants_host_execution(registered):
    policy = RequestSecurityPolicy.from_payload(RequestSecurityPolicy('visitor', 'qqbot', 'group').to_payload())
    assert policy.allows_tool('guest_workspace', {'action': 'run_office', 'command': 'id'})
    assert not policy.allows_tool('terminal', {'command': 'id'})
    assert not policy.allows_tool('execute_code', {'code': 'pass'})
    assert not policy.allows_tool('guest_workspace', {'action': 'shell'})
    info = call(policy, action='info')
    assert info['success']
    limits = info['office_limits']
    assert limits['file_bytes'] == 32 * 1024 * 1024
    assert limits['total_bytes_per_direction'] == 128 * 1024 * 1024
    assert limits['input_files'] == limits['output_files'] == 256
    assert info['limits']['workspace_bytes'] == 256 * 1024 * 1024
    assert info['python_limits']['document_bytes'] == 256 * 1024
    assert limits['network'] is False
