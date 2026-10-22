"""Fail-closed, binary office exchange with a trusted disposable Docker image.

No mounts, host execution fallback, caller flags, credentials or remote daemon
settings. The image ID is administrator configuration, never guest input.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from tools.guest_office_worker import (
    MAX_COMMAND_BYTES, MAX_FILES, MAX_FILE_BYTES, MAX_STREAM_BYTES, MAX_WIRE_BYTES,
    validate_files, validate_name,
)


def _docker_env():
    return {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT') if k in os.environ}


def _exchange(command, payload, timeout, env, cap=MAX_WIRE_BYTES):
    """Bound both drains and wall time, including a blocked stdin writer."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, shell=False)
    buffers = [bytearray(), bytearray()]
    overrun = threading.Event()

    def reader(pipe, buf):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if len(buf) + len(chunk) > cap:
                    overrun.set()
                    break
                buf.extend(chunk)
        finally:
            pipe.close()

    def writer():
        try:
            process.stdin.write(payload)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=reader, args=(process.stdout, buffers[0]), daemon=True),
               threading.Thread(target=reader, args=(process.stderr, buffers[1]), daemon=True),
               threading.Thread(target=writer, daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    failure = False
    try:
        while process.poll() is None:
            if overrun.wait(0.03) or time.monotonic() >= deadline:
                failure = True
                break
        if failure:
            process.kill()
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
        for thread in threads:
            thread.join(timeout=2)
    if failure or overrun.is_set() or any(t.is_alive() for t in threads):
        raise RuntimeError('Docker bounded exchange failed')
    return process.returncode, bytes(buffers[0]), bytes(buffers[1])


def _command(image, name):
    return ['docker', 'run', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--user=65534:65534',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128',
            '--cpus=1', '--memory=1g', '--memory-swap=1g', '--ulimit=core=0:0',
            '--tmpfs=/work:rw,nosuid,nodev,noexec,size=256m,mode=0700,uid=65534,gid=65534',
            '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=65534,gid=65534',
            '--workdir=/work', '--log-driver=none', '--entrypoint=/opt/office/bin/python3',
            '-i', image, '-I', '-B', '/opt/guest_office_worker.py']


def _decode_result(raw):
    result = json.loads(raw)
    if not isinstance(result, dict) or type(result.get('success')) is not bool:
        raise ValueError('Invalid response')
    if any(not isinstance(result.get(k), str) for k in ('stdout', 'stderr')):
        raise ValueError('Invalid streams')
    if sum(len(result[k].encode()) for k in ('stdout', 'stderr')) > MAX_STREAM_BYTES:
        raise ValueError('Stream limit')
    errors = result.get('errors')
    if (not isinstance(errors, list) or len(errors) > 8
            or any(not isinstance(e, str) or len(e.encode()) > 512 for e in errors)):
        raise ValueError('Invalid errors')
    codes = {None, 'timeout', 'output_limit', 'command_failed', 'worker_failed'}
    if result.get('error_code') not in codes:
        raise ValueError('Invalid error code')
    encoded = result.get('files')
    if not isinstance(encoded, dict) or len(encoded) > MAX_FILES:
        raise ValueError('Invalid files')
    files = {}
    for name, value in encoded.items():
        validate_name(name)
        if not isinstance(value, str) or len(value) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError('Encoded file limit')
        files[name] = base64.b64decode(value, validate=True)
    validate_files(files)
    if not result['success'] and files:
        raise ValueError('Failure returned files')
    return {**result, 'files': files}


def _verify_image(image, env):
    status, raw, _ = _exchange(['docker', 'image', 'inspect', '--format', '{{json .}}', image], b'', 8, env, cap=1024 * 1024)
    if status:
        raise RuntimeError('Image unavailable')
    info = json.loads(raw)
    config = info.get('Config', {})
    digest = hashlib.sha256(Path(__file__).with_name('guest_office_worker.py').read_bytes()).hexdigest()
    labels = config.get('Labels') or {}
    if (info.get('Id') != image or info.get('Os') != 'linux' or config.get('Volumes')
            or labels.get('org.hermes.office.worker-sha256') != digest
            or labels.get('org.hermes.office.runtime') != '1'):
        raise ValueError('Untrusted image metadata')


def _cleanup(name, env):
    _exchange(['docker', 'rm', '-f', name], b'', 8, env, cap=65536)
    status, _, diagnostic = _exchange(['docker', 'container', 'inspect', name], b'', 5, env, cap=65536)
    if status != 1 or b'No such' not in diagnostic:
        raise RuntimeError('Cleanup unconfirmed')


def run_office_sandbox(command: str, files: dict[str, bytes], *, image: str,
                       timeout: int = 60) -> dict:
    """Execute guest bash in a fresh private PID namespace; return binary files.

    256 files, 32 MiB/file, 128 MiB total, path depth 8, components 100 chars;
    command 32 KiB, timeout 1..90 seconds, stdout+stderr 64 KiB after decoding.
    Outputs are released only after worker namespace reaping AND verified
    container removal. Any execution/validation/cleanup failure returns no files.
    """
    result = dict(success=False, stdout='', stderr='', files={}, errors=[], error_code='invalid_request')
    name = None
    env = _docker_env()
    try:
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise ValueError('Immutable image ID required')
        if type(timeout) is not int or not 1 <= timeout <= 90:
            raise ValueError('Timeout must be 1..90')
        if not isinstance(command, str) or '\0' in command or len(command.encode()) > MAX_COMMAND_BYTES:
            raise ValueError('Command limit')
        validate_files(files)
        payload = json.dumps({'command': command, 'timeout': timeout, 'files': {
            n: base64.b64encode(v).decode('ascii') for n, v in files.items()}}, ensure_ascii=True).encode()
        if len(payload) > MAX_WIRE_BYTES:
            raise ValueError('Request wire limit')
        result['error_code'] = 'image_unavailable'
        _verify_image(image, env)
        name = 'hermes-guest-office-' + secrets.token_hex(16)
        result['error_code'] = 'runtime_failed'
        status, raw, _ = _exchange(_command(image, name), payload, timeout + 15, env)
        if status:
            raise RuntimeError('Docker failed')
        result = _decode_result(raw)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        result.update(success=False, files={}, errors=[result['error_code']])
    finally:
        if name is not None:
            try:
                _cleanup(name, env)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                result.update(success=False, files={}, errors=['cleanup_unconfirmed'], error_code='cleanup_unconfirmed')
    return result
