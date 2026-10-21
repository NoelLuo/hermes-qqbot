"""Private guest retention. Call cleanup_workspace/delete_file under the store lock.

sweep_expired_workspaces requires an explicit trusted base_dir; it
never consults the launch profile. Invalid metadata fails closed without rewriting
or deleting it. Ordinary files have no age-based deletion rule.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

METADATA = '.lifecycle.json'
METADATA_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 4096
EXPORT_TTL = 7 * 86400
TEMPORARY_TTL = 72 * 3600
# Existing snapshots were produced with uuid.uuid4().hex + '-' + basename.
_EXPORT_NAME = re.compile(r'^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}-.+$')
_TENANT_NAME = re.compile(r'^[0-9a-f]{64}$')


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _record_path(root, name, kind):
    from tools import guest_workspace_store as store
    if kind == 'export':
        parts = name.split('/')
        if len(parts) != 2 or parts[0] != '.exports' or not _EXPORT_NAME.fullmatch(parts[1]):
            store._deny('invalid lifecycle export path')
        store._parts(parts[1][33:])
        return root / '.exports' / parts[1]
    if kind != 'temporary':
        store._deny('invalid lifecycle classification')
    return store._path(root, name)


def _load(root):
    from tools import guest_workspace_store as store
    path = root / METADATA
    if not path.exists() and not path.is_symlink():
        return {}
    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                store._deny('duplicate lifecycle metadata key')
            result[key] = item
        return result

    value = json.loads(store._read(path, METADATA_BYTES), object_pairs_hook=unique_pairs)
    if (type(value) is not dict or set(value) != {'version', 'records'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['records']) is not dict or len(value['records']) > MAX_RECORDS):
        store._deny('invalid lifecycle metadata')
    records = value['records']
    if len({name.casefold() for name in records}) != len(records):
        store._deny('aliased lifecycle metadata')
    for name, item in records.items():
        if (type(item) is not dict or set(item) != {'kind', 'created', 'identity'}
                or type(item['created']) not in (int, float)
                or not math.isfinite(item['created']) or item['created'] < 0
                or type(item['identity']) is not list or len(item['identity']) != 4
                or any(type(n) is not int or n < 0 for n in item['identity'])):
            store._deny('invalid lifecycle metadata record')
        _record_path(root, name, item['kind'])
    return records


def record(base, root, target, staged, kind):
    """Atomically publish classification BEFORE rename, binding the staged inode.

    A failed file commit leaves at worst an unmatched record; cleanup cannot
    delete another file at that path. Metadata and its transaction are quota
    accounted, including the already staged file's peak disk usage.
    """
    from tools import guest_workspace_store as store
    records = _load(root)
    records = {name: item for name, item in records.items()
               if _record_path(root, name, item['kind']).exists()}
    name = target.relative_to(root).as_posix()
    _record_path(root, name, kind)
    # Keep canonical casing when Windows resolves an existing destination alias.
    records = {key: value for key, value in records.items() if key.casefold() != name.casefold()}
    records[name] = {'kind': kind, 'created': time.time(),
                     'identity': _identity(store._check_node(staged))}
    if len(records) > MAX_RECORDS:
        store._deny('lifecycle record limit')
    data = json.dumps({'version': 1, 'records': records}, ensure_ascii=True).encode('utf-8')
    if len(data) > METADATA_BYTES:
        store._deny('lifecycle metadata size limit')
    store._budget(base, root, len(data), 1, 1)
    transaction = root / ('.txn-' + uuid.uuid4().hex)
    try:
        with transaction.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(transaction, root / METADATA)
    finally:
        if transaction.exists():
            transaction.unlink()


def _prune(root, path, identity, minimum_age, now):
    from tools import guest_workspace_store as store
    # Inspect every ancestor; never follow junctions or hardlinks on the sweep.
    store._chain(path.parent)
    info = store._check_node(path)
    if (not stat.S_ISREG(info.st_mode) or _identity(info) != identity
            or now - info.st_mtime < minimum_age):
        return 0, 0
    # Re-stat immediately before unlink; the service-owned tree + shared lock
    # excludes cooperating writers (not a hostile privileged host process).
    if _identity(store._check_node(path)) != identity:
        return 0, 0
    path.unlink()
    return 1, info.st_size


def cleanup_workspace(root, *, now=None, scan_limit=65536):
    """Bounded own-tenant pruning; requires the store's existing serialization.

    Unknown/invalid metadata disables ALL pruning for this tenant, including
    legacy export migration. Never silently discard corrupt classifications.
    """
    from tools import guest_workspace_store as store
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('invalid lifecycle clock')
    deleted = size = scanned = skipped = 0
    try:
        store._chain(root)
        records = _load(root)
    except (store.WorkspaceError, OSError, ValueError, TypeError, RecursionError):
        return {'deleted_files': 0, 'deleted_bytes': 0, 'scanned': 0,
                'skipped': 1, 'invalid_metadata': True}
    for name, item in records.items():
        if scanned >= scan_limit:
            break
        scanned += 1
        ttl = EXPORT_TTL if item['kind'] == 'export' else TEMPORARY_TTL
        if now - item['created'] < ttl:
            continue
        try:
            path = _record_path(root, name, item['kind'])
            count, freed = _prune(root, path, item['identity'], ttl, now)
            deleted += count
            size += freed
        except (store.WorkspaceError, OSError, ValueError):
            skipped += 1
    # Legacy migration is restricted to this tenant's PRIVATE export directory,
    # never old ordinary files; recorded identities always take precedence.
    exports = root / '.exports'
    try:
        if exports.exists() or exports.is_symlink():
            store._chain(exports)
            recorded = {name.casefold() for name in records}
            for path in exports.iterdir():
                if scanned >= scan_limit:
                    break
                scanned += 1
                if not _EXPORT_NAME.fullmatch(path.name) or ('.exports/' + path.name).casefold() in recorded:
                    continue
                try:
                    _record_path(root, '.exports/' + path.name, 'export')
                    info = store._check_node(path)
                    count, freed = _prune(root, path, _identity(info), EXPORT_TTL, now)
                    deleted += count
                    size += freed
                except (store.WorkspaceError, OSError, ValueError):
                    skipped += 1
    except (store.WorkspaceError, OSError, ValueError):
        skipped += 1
    return {'deleted_files': deleted, 'deleted_bytes': size, 'scanned': scanned,
            'skipped': skipped, 'invalid_metadata': False}


def delete_file(root, name, confirm):
    """Own regular file / empty directory only. No quota check or recursion."""
    from tools import guest_workspace_store as store
    if confirm is not True:
        store._deny('delete requires confirm=true')
    path = store._path(root, name)
    store._chain(path.parent)
    info = store._check_node(path)
    if _identity(store._check_node(path)) != _identity(info):
        store._deny('file identity changed')
    if stat.S_ISDIR(info.st_mode):
        path.rmdir()
    else:
        path.unlink()
    return {'path': name, 'deleted': True}


def sweep_expired_workspaces(*, base_dir: Path, now: float | None = None, max_tenants=4096,
                           max_entries=65536):
    """Trusted periodic hook. Explicit profile base required; no API args.

    Scans only immediate SHA256-named tenant directories, continues on corrupt
    tenants, never traverses aliases, and shares the backend's global file lock.
    The result reports bounded progress, not a promise that every tenant ran.
    """
    from tools import guest_workspace_store as store
    if base_dir is None:
        raise ValueError('explicit trusted base_dir is required')
    if now is not None and (type(now) not in (int, float) or not math.isfinite(now) or now < 0):
        raise ValueError('invalid lifecycle clock')
    if type(max_tenants) is not int or not 1 <= max_tenants <= 4096:
        raise ValueError('invalid tenant bound')
    if type(max_entries) is not int or not 1 <= max_entries <= 65536:
        raise ValueError('invalid scan bound')
    base = Path(base_dir)
    result = {'tenants': 0, 'deleted_files': 0, 'deleted_bytes': 0, 'skipped': 0,
              'scanned': 0, 'truncated': False}
    if not base.exists() and not base.is_symlink():
        return result
    base = store._chain(base)
    with store._serialized(base):
        for root in base.iterdir():
            if result['scanned'] >= max_entries or result['tenants'] >= max_tenants:
                result['truncated'] = True
                break
            result['scanned'] += 1
            if not _TENANT_NAME.fullmatch(root.name):
                if root.name != '.lock':
                    result['skipped'] += 1
                continue
            try:
                info = store._check_node(root)
                if not stat.S_ISDIR(info.st_mode):
                    result['skipped'] += 1
                    continue
                report = cleanup_workspace(root, now=now, scan_limit=max_entries - result['scanned'])
                result['tenants'] += 1
                for key in ('deleted_files', 'deleted_bytes', 'skipped', 'scanned'):
                    result[key] += report[key]
                if result['scanned'] >= max_entries:
                    result['truncated'] = True
            except (store.WorkspaceError, OSError, ValueError):
                result['skipped'] += 1
    return result
