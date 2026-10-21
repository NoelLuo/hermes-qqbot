"""Bounded application-level guest file storage; NOT an OS sandbox.

The storage tree must be service-owned, with no other writers. A process/thread
lock serializes this backend (including quota accounting) across all tenants.
Reparse/link checks reject existing filesystem aliases, but do not defend against
an administrator concurrently replacing directories outside this backend.
Persistent user files are never age-pruned; private lifecycle metadata and exports
count towards quotas. Lifecycle operations share the storage lock.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent.request_security import RequestSecurityPolicy, current_policy
from tools.guest_workspace_text import TextDecodingError, decode_text

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 128 * 1024
MAX_FILES = 2048
MAX_ENTRIES = 4096
MAX_DEPTH = 8
WORKSPACE_BYTES = 256 * 1024 * 1024
GLOBAL_BYTES = 2 * 1024 * 1024 * 1024
GLOBAL_ENTRIES = 65536
_LOCK = threading.RLock()
_RESERVED = {'.exports', '.lock', '.txn', '.lifecycle.json'}
_DEVICE = re.compile(r'^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)', re.IGNORECASE)
_FIELDS = {
    'info': set(), 'list': {'path'}, 'read': {'path'},
    'write': {'path', 'text', 'overwrite'}, 'mkdir': {'path'},
    'copy': {'path', 'destination', 'overwrite'},
    'move': {'path', 'destination', 'overwrite'},
    'search': {'path', 'query'},
    'organize_csv': {'path', 'destination', 'sort_by', 'dedupe_by', 'overwrite'},
    'import_attachment': {'source', 'path', 'overwrite', 'temporary'}, 'export': {'path'},
    'delete': {'path', 'confirm'}, 'cleanup': set(),
    'run_python': {'code', 'inputs', 'overwrite'},
}


class WorkspaceError(Exception):
    pass


def _deny(message):
    raise WorkspaceError(message)


def _check_node(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        _deny('filesystem aliases are forbidden')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        _deny('unsupported filesystem object')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        _deny('hardlinked files are forbidden')
    return info


def _chain(path, create=False):
    """Inspect every existing ancestor, including trusted base ancestors."""
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        if not item.exists() and not item.is_symlink():
            if create:
                item.mkdir()
            else:
                _deny('path does not exist')
        _check_node(item)
    return path


def _parts(value, root_ok=False):
    if root_ok and value in ('', '.'):
        return []
    if not isinstance(value, str) or not value or len(value) > 512:
        _deny('invalid relative path')
    if any(ord(c) < 32 or c in '\\:<>"|?*' for c in value) or value.startswith('/'):
        _deny('invalid relative path')
    parts = value.split('/')
    if len(parts) > MAX_DEPTH:
        _deny('directory depth limit')
    for part in parts:
        if (not part or part in ('.', '..') or part.endswith(('.', ' '))
                or len(part) > 100 or _DEVICE.match(part.split('.')[0].rstrip(' ') + '.')
                or part.casefold() in _RESERVED or part.casefold().startswith('.txn-')):
            _deny('forbidden path component')
    return parts


def _path(root, value, root_ok=False):
    result = root
    for part in _parts(value, root_ok):
        result = result / part
        if result.exists() or result.is_symlink():
            _check_node(result)
    return result


def _scan(root, cap):
    entries = files = size = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        _check_node(directory)
        for path in directory.iterdir():
            info = _check_node(path)
            entries += 1
            if entries > cap:
                _deny('entry quota exceeded')
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                files += 1
                size += info.st_size
    return entries, files, size


@contextmanager
def _serialized(base):
    with _LOCK:
        lock = base / '.lock'
        if lock.exists() or lock.is_symlink():
            _check_node(lock)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _budget(base, root, added_bytes=0, added_entries=0, added_files=0):
    entries, files, size = _scan(root, MAX_ENTRIES)
    global_entries, _, global_size = _scan(base, GLOBAL_ENTRIES)
    if (entries + added_entries > MAX_ENTRIES or files + added_files > MAX_FILES
            or size + added_bytes > WORKSPACE_BYTES
            or global_size + added_bytes > GLOBAL_BYTES
            or global_entries + added_entries > GLOBAL_ENTRIES):
        _deny('storage quota exceeded')
    return {'entries': entries, 'files': files, 'bytes': size}


def _read(path, maximum=MAX_FILE_BYTES):
    info = _check_node(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        _deny('not an eligible bounded file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if actual.st_nlink != 1 or (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            _deny('file identity changed')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        _deny('file size limit')
    return data


def _put(base, root, target, data, overwrite=False, retention=None):
    if len(data) > MAX_FILE_BYTES:
        _deny('file size limit')
    if not target.parent.is_dir():
        _deny('destination parent does not exist')
    exists = target.exists()
    if exists:
        if not overwrite:
            _deny('destination exists; overwrite required')
        if not stat.S_ISREG(_check_node(target).st_mode):
            _deny('destination is not a regular file')
    # Conservative peak quota: old file and transaction coexist until replace.
    _budget(base, root, len(data), 1, 1)
    temporary = root / ('.txn-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if retention is not None:
            from tools.guest_workspace_lifecycle import record
            record(base, root, target, temporary, retention)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _csv(data, args):
    text = decode_text(data)['text']
    reader = csv.DictReader(io.StringIO(text, newline=''))
    headers = reader.fieldnames
    if not headers or len(set(headers)) != len(headers) or any(not h for h in headers):
        _deny('CSV requires unique nonempty headers')
    columns = {}
    for key in ('sort_by', 'dedupe_by'):
        value = args.get(key, [])
        if not isinstance(value, list) or any(not isinstance(c, str) or c not in headers for c in value):
            _deny('invalid CSV header columns')
        columns[key] = value
    rows = list(reader)
    if any(None in row or any(v is None for v in row.values()) for row in rows):
        _deny('CSV row width mismatch')
    if columns['sort_by']:
        rows.sort(key=lambda row: tuple(row[c] for c in columns['sort_by']))
    if columns['dedupe_by']:
        seen, unique = set(), []
        for row in rows:
            key = tuple(row[c] for c in columns['dedupe_by'])
            if key not in seen:
                seen.add(key)
                unique.append(row)
        rows = unique
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode('utf-8'), len(rows)


def _import_source(args, policy):
    source = args.get('source')
    if (current_policy() is not policy or not isinstance(source, str)
            or not source or source.startswith(('//', '\\')) or '\x00' in source
            or not Path(source).is_absolute()):
        _deny('attachment requires current request local artifact grant')
    # Ledger lookup precedes any filesystem access. Never use allows_media:
    # its admin/URL blanket authorization is deliberately insufficient here.
    if not policy._artifacts.allows(source):
        _deny('attachment is not in current request artifact ledger')
    path = _chain(Path(source))
    data = _read(path)
    if not policy._artifacts.allows(source):
        _deny('attachment identity changed')
    return data


def _sandbox_image():
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    try:
        image = config['plugins']['entries']['guest-workspace']['docker_image']
    except (KeyError, TypeError):
        _deny('Docker workspace image is not configured; no host fallback')
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        _deny('Docker workspace image requires a pinned full sha256 image ID; no host fallback')
    return image


def _public_snapshot(root, inputs=None):
    snapshot = {}
    total = 0

    def add(name, path):
        nonlocal total
        info = _check_node(path)
        if (len(snapshot) >= 32 or not stat.S_ISREG(info.st_mode)
                or info.st_size > 256 * 1024 or total + info.st_size > 1024 * 1024):
            _deny('Python input limits: 32 files, 256 KiB/file, 1 MiB total')
        data = _read(path, min(256 * 1024, 1024 * 1024 - total))
        total += len(data)
        snapshot[name] = data
    if inputs is not None:
        if not isinstance(inputs, list) or len(inputs) > 32 or any(not isinstance(name, str) for name in inputs):
            _deny('Python inputs must be a list of at most 32 relative document paths')
        if len({name.casefold() for name in inputs}) != len(inputs):
            _deny('duplicate Python input paths')
        for name in inputs:
            add(name, _path(root, name))
        return snapshot
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(part.casefold() in _RESERVED or part.casefold().startswith('.txn-') for part in relative.parts):
            continue
        _parts(relative.as_posix())
        info = _check_node(path)
        if stat.S_ISREG(info.st_mode):
            add(relative.as_posix(), path)
    return snapshot


def _apply_sandbox_outputs(base, root, before, outputs, overwrite, *,
                           max_files=32, max_file_bytes=256 * 1024,
                           max_total_bytes=1024 * 1024):
    if type(outputs) is not dict or len(outputs) > max_files:
        _deny('invalid or excessive sandbox outputs')
    changes, directories = {}, set()
    if any(not isinstance(name, str) for name in outputs):
        _deny('invalid sandbox output path')
    if len({name.casefold() for name in outputs}) != len(outputs):
        _deny('case-aliased sandbox output paths')
    for name, data in outputs.items():
        target = _path(root, name)
        if type(data) is not bytes or len(data) > min(max_file_bytes, MAX_FILE_BYTES):
            _deny('invalid sandbox output file or size')
        if name in before and before[name] == data:
            continue
        if target.exists():
            if not overwrite or not target.is_file():
                _deny('sandbox changed an existing file; overwrite required')
        changes[name] = data
        parent = target.parent
        while parent != root:
            if parent.exists():
                if not parent.is_dir():
                    _deny('sandbox output parent is not a directory')
                break
            directories.add(parent)
            parent = parent.parent
    names = {name.casefold() for name in outputs}
    if sum(map(len, outputs.values())) > max_total_bytes:
        _deny('sandbox total output size limit')
    if any('/'.join(name.split('/')[:i]) in names for name in names for i in range(1, len(name.split('/')))):
        _deny('sandbox file/directory output conflict')
    # Staged copies coexist with originals. Reserve transaction directory plus
    # final new directories; rename-based backups require no duplicate bytes.
    _budget(base, root, sum(map(len, changes.values())),
            len(changes) + len(directories) + (1 if changes else 0), len(changes))
    if not changes:
        return []
    transaction = root / ('.txn-' + uuid.uuid4().hex)
    transaction.mkdir()
    installed, backups, created = [], [], []
    try:
        for index, data in enumerate(changes.values()):
            (transaction / str(index)).write_bytes(data)
        for directory in sorted(directories, key=lambda p: len(p.parts)):
            directory.mkdir()
            created.append(directory)
        for index, name in enumerate(changes):
            target = _path(root, name)
            if target.exists():
                backup = transaction / ('backup-' + str(index))
                os.replace(target, backup)
                backups.append((backup, target))
            os.replace(transaction / str(index), target)
            installed.append(target)
    except BaseException:
        for target in reversed(installed):
            target.unlink()
        for backup, target in reversed(backups):
            os.replace(backup, target)
        for directory in reversed(created):
            directory.rmdir()
        raise
    finally:
        shutil.rmtree(transaction)
    return sorted(changes)


def _run_python(args, base, root):
    code = args.get('code')
    if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 32768:
        _deny('Python code must be nonempty and at most 32768 UTF-8 bytes')
    image = _sandbox_image()
    before = _public_snapshot(root, args.get('inputs'))
    try:
        from tools.guest_workspace_docker import run_python_sandbox
        result = run_python_sandbox(code, dict(before), image=image, timeout=20)
    except (ImportError, OSError, RuntimeError, ValueError):
        _deny('Docker workspace runner unavailable; no host fallback')
    if type(result) is not dict or result.get('success') is not True:
        _deny('Docker workspace execution failed or unavailable; no outputs saved; no host fallback')
    stdout, stderr = result.get('stdout', ''), result.get('stderr', '')
    if (not isinstance(stdout, str) or not isinstance(stderr, str)
            or len((stdout + stderr).encode('utf-8')) > MAX_OUTPUT_BYTES // 2):
        _deny('sandbox text output limit exceeded; no outputs saved')
    outputs = result.get('files')
    # Validate response size before committing any output files.
    if isinstance(outputs, dict):
        preview = {'success': True, 'output_files': list(outputs), 'stdout': stdout, 'stderr': stderr}
        if len(json.dumps(preview, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT_BYTES:
            _deny('sandbox output listing limit exceeded; no outputs saved')
    changed = _apply_sandbox_outputs(base, root, before, outputs, args.get('overwrite', False))
    return {'output_files': changed, 'stdout': stdout, 'stderr': stderr}


def _run(args, policy, base, root):
    action = args['action']
    from tools.guest_workspace_lifecycle import cleanup_workspace, delete_file
    cleaned = cleanup_workspace(root)
    if action == 'cleanup':
        return cleaned
    if action == 'delete':
        return delete_file(root, args.get('path'), args.get('confirm'))
    usage = _budget(base, root)
    if action == 'run_python':
        return _run_python(args, base, root)
    if action == 'info':
        return {'usage': usage, 'limits': {'file_bytes': MAX_FILE_BYTES,
                'text_bytes': MAX_TEXT_BYTES, 'output_bytes': MAX_OUTPUT_BYTES,
                'files': MAX_FILES, 'entries': MAX_ENTRIES, 'depth': MAX_DEPTH,
                'workspace_bytes': WORKSPACE_BYTES, 'global_bytes': GLOBAL_BYTES,
                'global_entries': GLOBAL_ENTRIES},
                'retention': {'exports_seconds': 7 * 86400, 'temporary_seconds': 72 * 3600,
                              'ordinary_files': 'persistent; no automatic age deletion'},
                'actions': {name: sorted(fields) for name, fields in _FIELDS.items()},
                'python_limits': {'input_files': 32, 'document_bytes': 256 * 1024,
                                  'total_document_bytes': 1024 * 1024, 'seconds': 20,
                                  'extensions': ['.txt', '.csv', '.json', '.md'],
                                  'network': False, 'subprocesses_and_threads': False},
                'help': {'write': 'path + text (UTF-8)',
                         'read': 'path; UTF-8, BOM-marked UTF-16/32, GB18030 fallback; returns encoding and encoding_detection; bytes unchanged; text_decode_error is not a permission denial',
                         'copy/move': 'path (source) + destination',
                         'import_attachment': 'source (granted absolute transport path) + path (relative destination)',
                         'temporary': 'import_attachment only: temporary=true expires unchanged file after 72 hours; default persistent',
                         'delete': 'path + confirm=true; own regular file or empty directory only; no recursion, exports or metadata',
                         'cleanup': 'own expired export snapshots (7 days) and explicitly temporary imports (72 hours) only',
                         'organize_csv': 'path + destination; sort_by/dedupe_by are header-name arrays; lexical sort then first-row dedupe',
                         'run_python': 'code; optional inputs selects own UTF-8 documents ([] means none); trusted configured Docker image required; overwrite opt-in; returned files never delete originals',
                         'paths': 'slash-separated relative paths; mkdir creates one level; private metadata and exports count in quota'},
                'boundary': 'application scoped file workspace; not an OS sandbox'}
    path = _path(root, args.get('path', ''), action in {'list', 'search'})
    if action == 'mkdir':
        _budget(base, root, added_entries=1)
        path.mkdir()
        return {'path': args['path']}
    if action == 'list':
        items = []
        output_size = 64
        for item in sorted(path.iterdir(), key=lambda p: p.name):
            if item.name.casefold() in _RESERVED or item.name.startswith('.txn-'):
                continue
            info = _check_node(item)
            entry = {'path': item.relative_to(root).as_posix(),
                     'directory': stat.S_ISDIR(info.st_mode), 'bytes': info.st_size if item.is_file() else 0}
            output_size += len(json.dumps(entry, ensure_ascii=False).encode('utf-8')) + 2
            if output_size > MAX_OUTPUT_BYTES - 256:
                return {'entries': items, 'truncated': True}
            items.append(entry)
        return {'entries': items, 'truncated': False}
    if action == 'read':
        return {'path': args['path'], **decode_text(_read(path, MAX_OUTPUT_BYTES))}
    if action == 'search':
        query = args.get('query')
        if not isinstance(query, str) or not query or len(query.encode('utf-8')) > 1024:
            _deny('invalid search query')
        matches, total = [], 0
        candidates = [path] if path.is_file() else sorted(path.rglob('*'))
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            if any(p.casefold() in _RESERVED or p.startswith('.txn-') for p in candidate.relative_to(root).parts):
                continue
            if not candidate.is_file():
                continue
            try:
                decoded = decode_text(_read(candidate, MAX_TEXT_BYTES))
                text = decoded['text']
            except (UnicodeError, WorkspaceError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    total += len(line.encode('utf-8')) + len(relative.encode('utf-8')) + 80
                    if total > MAX_OUTPUT_BYTES:
                        return {'matches': matches, 'truncated': True}
                    matches.append({'path': relative, 'line': number, 'text': line, 'encoding': decoded['encoding']})
        return {'matches': matches, 'truncated': False}
    if action == 'export':
        data = _read(path)
        exports = root / '.exports'
        if not exports.exists():
            _budget(base, root, added_entries=1)
            exports.mkdir()
        snapshot = exports / (uuid.uuid4().hex + '-' + path.name)
        _put(base, root, snapshot, data, retention='export')
        return {'path': args['path'], 'file_path': str(snapshot), 'media': 'MEDIA:' + str(snapshot),
                'bytes': len(data), 'delivery_status': 'pending_transport'}
    target = path
    extra = {}
    if action == 'write':
        text = args.get('text')
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_TEXT_BYTES:
            _deny('text input limit or invalid text')
        data = text.encode('utf-8')
    elif action == 'import_attachment':
        data = _import_source(args, policy)
    else:
        data = _read(path, MAX_TEXT_BYTES if action == 'organize_csv' else MAX_FILE_BYTES)
        target = _path(root, args.get('destination'))
        if target == path and action != 'organize_csv':
            _deny('source and destination must differ')
        if action == 'organize_csv':
            data, extra['rows'] = _csv(data, args)
    retention = 'temporary' if action == 'import_attachment' and args.get('temporary', False) else None
    _put(base, root, target, data, args.get('overwrite', False), retention=retention)
    if action == 'move':
        path.unlink()
    return {'path': target.relative_to(root).as_posix(), 'bytes': len(data), **extra}


def execute_workspace(args: dict, policy: RequestSecurityPolicy, *, base_dir: Path | None = None) -> dict:
    """Execute one bounded operation. base_dir is trusted internal injection only.

    Paths/destinations are slash-separated relative strings. write takes text;
    copy/move/organize_csv take destination; CSV columns use sort_by/dedupe_by
    arrays (lexical ascending, sort then first-row dedupe). mkdir is single-level.
    import_attachment takes an absolute source already granted in the *current*
    policy ledger. No source URL, host execution, archive extraction, or user-selected storage roots.
    """
    try:
        if type(policy) is not RequestSecurityPolicy or policy.public_context is not True:
            _deny('explicit public request policy required')
        active = current_policy()
        if active is not None and active is not policy:
            _deny('policy does not match current request')
        identifiers = (policy.scope_id, policy.platform, policy.chat_id, policy.subject_id)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in identifiers):
            _deny('policy identity missing or malformed')
        if type(args) is not dict or not isinstance(args.get('action'), str):
            _deny('invalid arguments')
        action = args['action']
        if action not in _FIELDS or set(args) - (_FIELDS[action] | {'action'}):
            _deny('unsupported action or argument')
        if 'overwrite' in args and type(args['overwrite']) is not bool:
            _deny('overwrite must be boolean')
        for flag in ('temporary', 'confirm'):
            if flag in args and type(args[flag]) is not bool:
                _deny(flag + ' must be boolean')
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        base = _chain(Path(base_dir) if base_dir is not None else home / 'workspaces' / 'guests', create=True)
        # Profile home is hashed as well, even for explicitly shared test bases.
        values = (str(home), *identifiers)
        digest = hashlib.sha256(repr(values).encode('utf-8')).hexdigest()
        with _serialized(base):
            root = base / digest
            if not root.exists():
                global_entries, _, _ = _scan(base, GLOBAL_ENTRIES)
                if global_entries + 1 > GLOBAL_ENTRIES:
                    _deny('global entry quota exceeded')
                root.mkdir()
            _check_node(root)
            result = _run(args, policy, base, root)
            response = {'success': True, **result}
            if len(json.dumps(response, ensure_ascii=False).encode('utf-8')) > MAX_OUTPUT_BYTES:
                _deny('text output limit exceeded')
            if action == 'export':
                snapshot = result['file_path']
                if not policy.grant_artifact(snapshot) or not policy._exports.record(policy, snapshot):
                    Path(snapshot).unlink()
                    _deny('export grant failed')
            return response
    except TextDecodingError as exc:
        return {'success': False, 'error': str(exc), 'error_code': 'text_decode_error'}
    except WorkspaceError as exc:
        return {'success': False, 'error': str(exc)}
    except (OSError, ValueError, TypeError, UnicodeError, csv.Error, RuntimeError):
        # Do not echo exception filenames or user-supplied absolute host paths.
        return {'success': False, 'error': 'workspace operation refused or unavailable'}


def save_browser_artifacts(files: dict[str, bytes], policy: RequestSecurityPolicy, *,
                           base_dir: Path | None = None) -> dict:
    """Trusted browser output sink, NOT transport ingress or a model action.

    Enforces the exact current public policy's guest_browser/browse permission
    before any filesystem I/O. Saves a bounded atomic batch as persistent own
    browser/<uuid>-<basename> files without overwrite or media grants. base_dir
    is trusted integration/test injection, never a value from browser arguments.
    """
    try:
        if (type(policy) is not RequestSecurityPolicy or policy.public_context is not True
                or current_policy() is not policy
                or not policy.allows_tool('guest_browser', {'action': 'browse'})):
            _deny('current public guest_browser policy required')
        identifiers = (policy.scope_id, policy.platform, policy.chat_id, policy.subject_id)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4096 for v in identifiers):
            _deny('policy identity missing or malformed')
        if type(files) is not dict or not 1 <= len(files) <= 8:
            _deny('browser artifacts require 1 to 8 files')
        extensions = {'.txt', '.csv', '.json', '.md', '.pdf', '.png', '.jpg', '.jpeg', '.webp'}
        outputs = {}
        for name, data in files.items():
            if (len(_parts(name)) != 1 or len(name) > 67
                    or Path(name).suffix.lower() not in extensions
                    or type(data) is not bytes or len(data) > MAX_FILE_BYTES):
                _deny('invalid browser artifact basename, type or size')
            outputs['browser/' + uuid.uuid4().hex + '-' + name] = data
        if sum(map(len, outputs.values())) > 64 * 1024 * 1024:
            _deny('browser artifact batch exceeds 64 MiB')
        from hermes_constants import get_hermes_home
        from tools.guest_workspace_lifecycle import cleanup_workspace
        home = get_hermes_home()
        base = _chain(Path(base_dir) if base_dir is not None else home / 'workspaces' / 'guests', create=True)
        digest = hashlib.sha256(repr((str(home), *identifiers)).encode('utf-8')).hexdigest()
        with _serialized(base):
            root = base / digest
            if not root.exists():
                entries, _, _ = _scan(base, GLOBAL_ENTRIES)
                if entries + 1 > GLOBAL_ENTRIES:
                    _deny('global entry quota exceeded')
                root.mkdir()
            _check_node(root)
            cleanup_workspace(root)
            changed = _apply_sandbox_outputs(base, root, {}, outputs, False,
                                             max_files=8, max_file_bytes=MAX_FILE_BYTES,
                                             max_total_bytes=64 * 1024 * 1024)
            return {'success': True, 'files': [{'path': name, 'bytes': len(outputs[name])}
                                               for name in changed]}
    except WorkspaceError as exc:
        return {'success': False, 'error': str(exc)}
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        return {'success': False, 'error': 'browser artifact storage refused or unavailable'}
