"""Office executor binary/path/pipe and fail-closed lifecycle contracts."""
import base64
import json
import sys

import pytest

from tools import guest_office_docker as runtime
from tools.guest_office_worker import validate_files, validate_name

IMAGE = 'sha256:' + 'a' * 64


def wire(**changes):
    value = dict(success=True, stdout='', stderr='', files={}, errors=[], error_code=None)
    value.update(changes)
    return json.dumps(value).encode()


@pytest.mark.parametrize('name', ['../a', '/x', 'a\\b', 'NUL.pdf', 'x./a', 'a:b',
                                  'x/' * 8 + 'y', 'a' * 101, 'a\0b'])
def test_bad_paths(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_binary_capacity_and_aliases():
    validate_files({'archive.ZIP': b'\0\xff' * (12 * 1024 * 1024), 'a/b.bin': b'\xfe'})
    for files in ({'A.pdf': b'', 'a.pdf': b''}, {'a': b'', 'A/b': b''},
                  {'x': b'x' * (32 * 1024 * 1024 + 1)},
                  {str(i): b'' for i in range(257)}):
        with pytest.raises(ValueError):
            validate_files(files)


def test_decode_binary_and_stream_expansion():
    assert runtime._decode_result(wire(files={'x.bin': base64.b64encode(b'\0\xff').decode()}))['files']['x.bin'] == b'\0\xff'
    with pytest.raises(ValueError):
        runtime._decode_result(wire(stdout='\ufffd' * 23000))
    with pytest.raises(ValueError):
        runtime._decode_result(wire(stdout='a' * 40000, stderr='b' * 30000))
    with pytest.raises(ValueError):
        runtime._decode_result(wire(success=False, files={'x': 'eA=='}))


@pytest.mark.parametrize('command,timeout,image', [('x' * 32769, 1, IMAGE), ('x', True, IMAGE),
                                                 ('x', 91, IMAGE), ('x', 1, 'arbitrary:tag')],
                         ids=['oversize-command', 'boolean-timeout', 'oversize-timeout', 'mutable-image'])
def test_reject_before_docker(monkeypatch, command, timeout, image):
    monkeypatch.setattr(runtime, '_exchange', lambda *a, **k: pytest.fail('Docker called'))
    result = runtime.run_office_sandbox(command, {}, image=image, timeout=timeout)
    assert not result['success'] and result['error_code'] == 'invalid_request'


def test_cleanup_precedes_release_and_failure_drops_files(monkeypatch):
    monkeypatch.setattr(runtime, '_verify_image', lambda *a: None)
    monkeypatch.setattr(runtime, '_exchange', lambda *a, **k: (0, wire(files={'x': 'eA=='}), b''))
    removed = []
    monkeypatch.setattr(runtime, '_cleanup', lambda name, env: removed.append(name))
    result = runtime.run_office_sandbox('true', {}, image=IMAGE)
    assert result['success'] and result['files'] == {'x': b'x'} and len(removed) == 1

    def fail(*args):
        raise RuntimeError('cleanup failed')
    monkeypatch.setattr(runtime, '_cleanup', fail)
    result = runtime.run_office_sandbox('true', {}, image=IMAGE)
    assert not result['success'] and result['files'] == {} and result['error_code'] == 'cleanup_unconfirmed'


def test_cli_env_drops_credentials_and_remote_daemon(monkeypatch):
    for key in ('API_KEY', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'HOME', 'HTTP_PROXY'):
        monkeypatch.setenv(key, 'secret')
    assert not set(runtime._docker_env()) & {'API_KEY', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'HOME', 'HTTP_PROXY'}


def test_fixed_runtime_limits():
    command = runtime._command(IMAGE, 'test-owned')
    for flag in ('--network=none', '--read-only', '--user=65534:65534', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges', '--pids-limit=128', '--cpus=1',
                 '--memory=1g', '--memory-swap=1g', '--log-driver=none'):
        assert flag in command
    assert not any(x.startswith(('--volume', '--mount', '--env', '--pid', '--privileged')) for x in command if x != '--pids-limit=128')


def test_real_bounded_pipe():
    env = runtime._docker_env()
    assert runtime._exchange([sys.executable, '-I', '-c', 'print(123)'], b'', 5, env, cap=100)[1].strip() == b'123'
    with pytest.raises(RuntimeError):
        runtime._exchange([sys.executable, '-I', '-c', 'import sys;sys.stdout.write("x"*100000)'], b'', 5, env, cap=100)
