"""Actual public browser -> requester store -> export ledger integration, isolated home."""
import json
from pathlib import Path
import struct
import subprocess

import pytest
import hermes_yaml
import model_tools
from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from tools.guest_browser_interface import register as register_browser
from tools.guest_workspace_interface import register as register_workspace
from tools.guest_workspace_docker import _docker_env
from tools.registry import registry


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr('hermes_cli.left_core_migration.recover_at_startup', lambda **kw: None)
    image = subprocess.check_output(
        ['docker', 'image', 'inspect', 'hermes-guest-browser:1.63.0', '--format', '{{.Id}}'],
        env=_docker_env(), text=True, timeout=10).strip()
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.yaml').write_text(hermes_yaml.safe_dump({
        'plugins': {'entries': {'guest-workspace': {'browser_enabled': True, 'browser_image': image}}},
    }), encoding='utf-8')
    scope = registry.current_scope_key()
    names = ['guest_workspace', 'guest_browser']
    before = {name: registry.snapshot_registration(name, scope=scope) for name in names}

    class Context:
        def register_tool(self, **kwargs):
            registry.register(**kwargs, scope=scope)

    register_browser(Context())
    register_workspace(Context())
    current = {name: registry.snapshot_registration(name, scope=scope) for name in names}
    from tools import guest_browser_docker
    real_driver = guest_browser_docker.run_browser_sandbox
    diagnostics = []

    def observed_driver(*args, **kwargs):
        result = real_driver(*args, **kwargs)
        diagnostics.append(result.get('error', 'success'))
        return result

    monkeypatch.setattr(guest_browser_docker, 'run_browser_sandbox', observed_driver)
    yield diagnostics
    for name in names:
        registry.restore_registration(name, current[name], before[name], scope=scope)


def call(p, tool, **args):
    with policy_scope(p):
        return json.loads(model_tools.handle_function_call(tool, args))


def test_actual_public_screenshot_store_export_isolation_and_own_delete(ready):
    p = RequestSecurityPolicy('live-test-visitor', 'qqbot', 'live-test-room')
    result = call(p, 'guest_browser', action='browse', url='https://example.com/',
                  actions=[{'kind': 'read'}, {'kind': 'screenshot'}])
    assert result['success'], (result, ready)
    assert result['title'] == 'Example Domain'
    assert result['text'], 'Public page must yield actual rendered body text'
    path = result['files'][0]['path']
    assert path.startswith('browser/') and path.endswith('.png')
    exported = call(p, 'guest_workspace', action='export', path=path)
    assert exported['success'], exported
    raw = Path(exported['file_path']).read_bytes()
    assert raw.startswith(b'\x89PNG\r\n\x1a\n')
    assert struct.unpack('>II', raw[16:24]) == (1280, 720)
    assert p.allows_media(exported['file_path'])
    assert not RequestSecurityPolicy('live-test-visitor', 'qqbot', 'live-test-room').allows_media(exported['file_path'])
    other = RequestSecurityPolicy('other-visitor', 'qqbot', 'live-test-room')
    assert not call(other, 'guest_workspace', action='export', path=path)['success']
    assert not call(p, 'guest_workspace', action='delete', path=path, confirm=False)['success']
    assert call(p, 'guest_workspace', action='delete', path=path, confirm=True)['success']
    assert not call(p, 'guest_workspace', action='export', path=path)['success']
    assert Path(exported['file_path']).is_file(), 'Removing work must not delete a still-valid delivery snapshot'
    blocked = call(p, 'guest_browser', action='browse', url='http://127.0.0.1/')
    assert not blocked['success']
