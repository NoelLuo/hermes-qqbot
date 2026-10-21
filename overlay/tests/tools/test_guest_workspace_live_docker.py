"""Real plugin discovery -> schemas -> file broker -> disposable Docker -> export."""
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

import model_tools
from agent.agent_init import _load_tools
from agent.request_security import RequestSecurityPolicy, policy_scope
from hermes_constants import get_hermes_home
from hermes_cli.plugins import discover_plugins

# Test-only prerequisite: operator supplies a locally verified immutable image ID.
IMAGE = os.environ.get('QQBOT_TEST_PYTHON_IMAGE', '')


@pytest.fixture
def live_workspace(monkeypatch):
    if not IMAGE:
        pytest.skip('Set QQBOT_TEST_PYTHON_IMAGE to a verified local sha256 image ID')
    if re.fullmatch(r'sha256:[0-9a-f]{64}', IMAGE) is None:
        pytest.fail('QQBOT_TEST_PYTHON_IMAGE must be a full immutable sha256 image ID')
    if not shutil.which('docker'):
        pytest.skip('Docker CLI unavailable')
    probe = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', IMAGE],
                           capture_output=True, text=True, timeout=15, check=False)
    if probe.returncode or probe.stdout.strip() != IMAGE:
        pytest.skip('Pinned local Docker image/engine unavailable')
    monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
    monkeypatch.setenv('GUEST_WORKSPACE_HOST_CANARY', 'test-only-not-a-secret')
    home = get_hermes_home()
    plugin = home / 'plugins' / 'guest-workspace'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.yaml').write_text(
        'name: guest-workspace\nversion: 0.1.0\nprovides_tools:\n  - guest_workspace\n', encoding='utf-8')
    (plugin / '__init__.py').write_text(
        'def register(ctx):\n    from tools.guest_workspace_interface import register\n    register(ctx)\n', encoding='utf-8')
    (home / 'config.yaml').write_text(yaml.safe_dump({
        'model': {'default': 'offline-test', 'context_length': 8192},
        'plugins': {'enabled': ['guest-workspace'], 'entries': {'guest-workspace': {'docker_image': IMAGE}}},
        'tools': {'tool_search': {'enabled': 'on'}},
    }), encoding='utf-8')
    discover_plugins(force=True)
    policy = RequestSecurityPolicy('live-test-visitor', 'qqbot', 'live-test-group')
    agent = SimpleNamespace(quiet_mode=True, platform='qqbot', _request_security_policy=policy)
    _load_tools(agent, ['guest_workspace', 'cronjob'], None)
    assert {'guest_workspace', 'cronjob_manage'}.issubset(agent.valid_tool_names)
    assert 'tool_search' not in agent.valid_tool_names

    def call(**args):
        with policy_scope(policy):
            result = model_tools.handle_function_call('guest_workspace', args)
        return json.loads(result)
    return call, policy


def test_live_registry_docker_transform_and_export(live_workspace):
    call, policy = live_workspace
    assert call(action='write', path='input.csv', text='name,value\nb,2\na,1\na,1\n')['success']
    code = (
        "import csv,os,socket\n"
        "assert os.getuid()==65534\n"
        "assert 'GUEST_WORKSPACE_HOST_CANARY' not in os.environ\n"
        "assert not os.path.exists('/var/run/docker.sock')\n"
        "try:\n socket.create_connection(('1.1.1.1',80),timeout=0.2)\n"
        "except OSError:\n pass\n"
        "else:\n raise RuntimeError('network unexpectedly available')\n"
        "with open('input.csv',newline='') as f: rows=list(csv.DictReader(f))\n"
        "rows=sorted({(r['name'],r['value']) for r in rows})\n"
        "with open('result.csv','w',newline='') as f:\n"
        " w=csv.writer(f,lineterminator='\\n');w.writerow(['name','value']);w.writerows(rows)\n"
        "print('REAL_DOCKER_TRANSFORM_OK')\n"
    )
    result = call(action='run_python', code=code)
    assert result['success'], result
    assert result['output_files'] == ['result.csv']
    assert result['stdout'].strip() == 'REAL_DOCKER_TRANSFORM_OK'
    assert call(action='read', path='result.csv')['text'] == 'name,value\na,1\nb,2\n'
    exported = call(action='export', path='result.csv')
    assert exported['success'] and policy.allows_media(exported['file_path'])
    assert not RequestSecurityPolicy('other', 'qqbot', 'live-test-group').allows_media(exported['file_path'])


def test_live_failed_link_output_is_not_persisted(live_workspace):
    call, _ = live_workspace
    result = call(action='run_python', code="import os;os.symlink('/etc/passwd','leak.txt')")
    assert not result['success']
    assert call(action='list')['entries'] == []


def test_live_changed_input_requires_overwrite_and_is_transactional(live_workspace):
    call, _ = live_workspace
    assert call(action='write', path='original.txt', text='original')['success']
    result = call(action='run_python', code="open('original.txt','w').write('changed');open('new.txt','w').write('new')")
    assert not result['success']
    assert call(action='read', path='original.txt')['text'] == 'original'
    assert not call(action='read', path='new.txt')['success']


def test_live_explicit_inputs_exclude_unrelated_files_and_reject_escape(live_workspace):
    call, _ = live_workspace
    assert call(action='write', path='needed.txt', text='selected')['success']
    assert call(action='write', path='unrelated.bin', text='not a sandbox document')['success']
    result = call(action='run_python', inputs=['needed.txt'], code=(
        "from pathlib import Path\n"
        "assert not Path('unrelated.bin').exists()\n"
        "Path('result.md').write_text(Path('needed.txt').read_text().upper())\n"
    ))
    assert result['success'], result
    assert call(action='read', path='result.md')['text'] == 'SELECTED'
    assert not call(action='run_python', inputs=['../config.yaml'], code='pass')['success']
    assert not call(action='run_python', inputs=['needed.txt', 'NEEDED.txt'], code='pass')['success']
