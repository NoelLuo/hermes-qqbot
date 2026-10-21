"""Real schema assembly keeps guest tools eager without granting tool discovery."""
from types import SimpleNamespace

import hermes_bootstrap  # activate real dependencies before collection-time imports
import pytest
import hermes_yaml as yaml

import model_tools
from agent.agent_init import _load_tools
from agent.request_security import PUBLIC_TOOLS, RequestSecurityPolicy
from hermes_constants import get_hermes_home
from tools.guest_workspace_interface import register
from tools.registry import registry


@pytest.fixture
def registered_workspace(monkeypatch):
    # Stage only test configuration, never load the live profile's plugins.
    monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'model': {'default': 'offline-test', 'context_length': 8192},
        'tools': {'tool_search': {'enabled': 'on'}},
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


def guest(**changes):
    values = dict(subject_id='visitor', platform='qqbot', chat_id='group')
    values.update(changes)
    return RequestSecurityPolicy(**values)


def load(policy, enabled=None, disabled=None):
    agent = SimpleNamespace(quiet_mode=True, platform='qqbot', _request_security_policy=policy)
    _load_tools(agent, enabled or ['cronjob', 'guest_workspace', 'terminal', 'memory'], disabled)
    return agent


def test_guest_real_assembly_exposes_workspace_and_cron_directly(registered_workspace):
    agent = load(guest())
    assert {'guest_workspace', 'cronjob_manage'}.issubset(agent.valid_tool_names)
    assert not ({'terminal', 'memory', 'tool_search', 'tool_describe', 'tool_call'} & agent.valid_tool_names)
    assert agent.valid_tool_names.issubset(PUBLIC_TOOLS)
    schema = next(x['function'] for x in agent.tools if x['function']['name'] == 'guest_workspace')
    assert 'run_python' in schema['parameters']['properties']['action']['enum']
    assert not schema['parameters']['additionalProperties']


def test_explicit_public_allowlist_can_withhold_workspace(registered_workspace):
    agent = load(guest(allowed_tools=frozenset({'cronjob_manage'})))
    assert agent.valid_tool_names == {'cronjob_manage'}


def test_disabled_toolset_is_not_reenabled_by_public_surface(registered_workspace):
    agent = load(guest(), disabled=['guest_workspace'])
    assert 'guest_workspace' not in agent.valid_tool_names
    assert 'cronjob_manage' in agent.valid_tool_names


@pytest.mark.parametrize('policy', [None, guest(is_admin=True)])
def test_admin_and_legacy_preserve_existing_discovery_assembly(registered_workspace, policy):
    agent = load(policy)
    assert 'tool_search' in agent.valid_tool_names
    assert 'terminal' in agent.valid_tool_names


@pytest.mark.parametrize('args', [None, {}, {'action': []}, {'action': {}}, {'action': 'terminal'}, {'action': 'delete'}, {'action': 'delete', 'confirm': False}])
def test_workspace_permission_rejects_non_operations(args):
    assert not guest().allows_tool('guest_workspace', args)


@pytest.mark.parametrize('action', ['info', 'write', 'organize_csv', 'export', 'run_python', 'cleanup'])
def test_workspace_permission_allows_only_named_broker_operations(action):
    assert guest().allows_tool('guest_workspace', {'action': action})
    assert not guest().allows_tool('terminal', {'command': 'echo no'})
    assert not guest().allows_tool('execute_code', {'code': 'print(1)'})


def test_durable_public_workspace_grant_stays_bound_and_cannot_spawn_cron():
    policy = RequestSecurityPolicy.from_payload(guest().to_payload())
    assert policy.subject_id == 'visitor' and policy.chat_id == 'group'
    assert policy.allows_tool('guest_workspace', {'action': 'list'})
    assert not policy.allows_tool('cronjob_manage', {'action': 'create'})
