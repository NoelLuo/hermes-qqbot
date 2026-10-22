"""Trusted PID-namespace init for the disposable office executor (stdlib only)."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

MAX_FILES = 256
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_STREAM_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 32 * 1024
MAX_WIRE_BYTES = 180 * 1024 * 1024
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:\.|$)", re.IGNORECASE)


def validate_name(name):
    if not isinstance(name, str) or not name or len(name.split('/')) > 8:
        raise ValueError('Unsafe path')
    for part in name.split('/'):
        if (not part or len(part) > 100 or part in {'.', '..'} or part[-1] in ' .'
                or _RESERVED.match(part)
                or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)):
            raise ValueError('Unsafe path')


def validate_files(files):
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError('File count limit')
    seen = set()
    total = 0
    for name, data in files.items():
        validate_name(name)
        folded = name.casefold()
        # File/directory aliases must also be rejected across Windows exchange.
        if folded in seen:
            raise ValueError('Path collision')
        seen.add(folded)
        if not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise ValueError('File byte limit')
        total += len(data)
    if total > MAX_TOTAL_BYTES:
        raise ValueError('Total byte limit')
    for name in seen:
        parts = name.split('/')
        if any('/'.join(parts[:i]) in seen for i in range(1, len(parts))):
            raise ValueError('File/directory collision')


def collect():
    files = {}
    total = 0
    entries = 0
    for root, dirs, names in os.walk('/work', followlinks=False):
        for name in dirs + names:
            entries += 1
            if entries > MAX_FILES * 8:
                raise ValueError('Entry limit')
            path = os.path.join(root, name)
            relative = os.path.relpath(path, '/work')
            validate_name(relative)
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Links and special files forbidden')
            if info.st_size > MAX_FILE_BYTES or len(files) >= MAX_FILES:
                raise ValueError('Output file limit')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError('Output byte limit')
            files[relative] = data
    validate_files(files)
    return {n: base64.b64encode(v).decode('ascii') for n, v in files.items()}


def child_limits():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES + 1, MAX_FILE_BYTES + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))


def reap_namespace():
    """PID 1 is kernel-protected; kill ALL peers, including setsid escapees.

    The private PID namespace contains only this init and its descendants. No
    privileged UID switch is possible (all capabilities dropped + NNP). PID 1
    adopts every orphan, so ECHILD after SIGKILL proves no concurrent writers.
    Never install signal handlers: namespace PID 1's default dispositions are
    the protection against a same-UID guest signalling its supervisor.
    """
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        # A short command can already have exited and been reaped by Popen.poll.
        pass
    deadline = time.monotonic() + 3
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Reaping not confirmed')
        if pid == 0:
            time.sleep(0.01)


def execute(req):
    command = req['command']
    budget = req['timeout']
    if (not isinstance(command, str) or len(command.encode()) > MAX_COMMAND_BYTES
            or type(budget) is not int or not 1 <= budget <= 90):
        raise ValueError('Invalid command/timeout')
    files = {n: base64.b64decode(v, validate=True) for n, v in req['files'].items()}
    validate_files(files)
    for name, data in files.items():
        path = '/work/' + name
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'xb') as stream:
            stream.write(data)
    del files
    # This fresh single-threaded init also fits these limits; children inherit
    # them without running Python in an unsafe post-fork preexec callback.
    child_limits()
    process = subprocess.Popen(
        ['/bin/bash', '--noprofile', '--norc', '-c', command], cwd='/work',
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/opt/office/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp',
             'TMPDIR': '/tmp', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'},
        start_new_session=True)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    deadline = time.monotonic() + budget
    failure = None
    try:
        while process.poll() is None or selector.get_map():
            if time.monotonic() >= deadline:
                failure = 'timeout'
                break
            for key, _ in selector.select(0.03):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                candidate = buffers[key.data] + chunk
                # Decode expansion counts toward the actual returned UTF-8 cap.
                other = 'stderr' if key.data == 'stdout' else 'stdout'
                if (len(candidate.decode('utf-8', 'replace').encode())
                        + len(buffers[other].decode('utf-8', 'replace').encode()) > MAX_STREAM_BYTES):
                    failure = 'output_limit'
                    break
                buffers[key.data].extend(chunk)
            if failure:
                break
    finally:
        selector.close()
        reap_namespace()
        process.stdout.close()
        process.stderr.close()
    streams = {k: v.decode('utf-8', 'replace') for k, v in buffers.items()}
    if failure:
        return dict(success=False, **streams, files={}, errors=[failure], error_code=failure)
    if process.returncode:
        return dict(success=False, **streams, files={}, errors=['Command failed'], error_code='command_failed')
    return dict(success=True, **streams, files=collect(), errors=[], error_code=None)


def main():
    result = dict(success=False, stdout='', stderr='', files={}, errors=['Worker failure'], error_code='worker_failed')
    try:
        if os.getpid() != 1 or os.getuid() == 0:
            raise RuntimeError('Private nonroot namespace init required')
        if ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) != 0:
            raise RuntimeError('Supervisor protection failed')
        raw = sys.stdin.buffer.read(MAX_WIRE_BYTES + 1)
        if len(raw) > MAX_WIRE_BYTES:
            raise ValueError('Wire limit')
        result = execute(json.loads(raw))
    except BaseException:
        # No internal tracebacks or potentially sensitive image paths on wire.
        result['files'] = {}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
