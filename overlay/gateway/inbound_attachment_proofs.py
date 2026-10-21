"""Transport-owned attachment receipts, distinct from visual background and grants.

Only adapter intake writes receipts. Transcript metadata is never an authority.
Native snapshots retain receipt IDs plus media indexes, not request policy ledgers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager

from hermes_constants import get_hermes_home

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 4
TTL = 600


def _identity(value):
    path = Path(value)
    if not path.is_absolute() or str(value).startswith(('\\\\', '//')):
        raise ValueError('attachment must be local')
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('attachment link/reparse point')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_BYTES:
        raise ValueError('attachment identity/size invalid')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    if (identity != [opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns]
            or identity != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]):
        raise ValueError('attachment changed')
    return str(path.resolve()), identity, digest


@contextmanager
def _database():
    root = get_hermes_home() / 'cache' / 'qqbot'
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / 'inbound_attachments.sqlite3')
    connection.execute('CREATE TABLE IF NOT EXISTS receipts '
                       '(id TEXT PRIMARY KEY, scope TEXT, chat TEXT, actor TEXT, message TEXT, '
                       'created REAL, path TEXT, identity TEXT, digest TEXT, mime TEXT, request TEXT)')
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _scope(source):
    # The active home is bound by the owning adapter/turn; never a model field.
    namespace = [str(get_hermes_home().resolve()), source.profile or 'default',
                 source.scope_id or '', source.chat_type, source.thread_id or '']
    return json.dumps(namespace), str(source.chat_id), str(source.user_id)


def _prune(connection):
    # Prune receipts only, never downloaded files or user workspace outputs.
    connection.execute('DELETE FROM receipts WHERE created < ?', (time.time() - TTL,))
    connection.execute('DELETE FROM receipts WHERE id IN '
                       '(SELECT id FROM receipts ORDER BY created DESC LIMIT -1 OFFSET 4092)')


def record_attachment(event, path, mime):
    """Called only with successfully downloaded current transport attachments."""
    if not event.message_id or not event.source.user_id or not event.source.chat_id:
        return None
    try:
        resolved, identity, digest = _identity(path)
    except (OSError, ValueError):
        return None
    from gateway.platforms.base import get_document_cache_dir, get_image_cache_dir
    if not any(Path(resolved).is_relative_to(root.resolve())
               for root in (get_document_cache_dir(), get_image_cache_dir())):
        return None
    receipt = uuid.uuid4().hex
    with _database() as connection:
        _prune(connection)
        connection.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (receipt, *_scope(event.source), event.message_id, time.time(), resolved,
                            json.dumps(identity), digest, mime, event.message_id))
    return receipt


def _matches_identity(resolved, identity, stored, digest):
    if identity == stored:
        return True
    # Native content-addressed aliases refresh mtime for retention on recapture.
    # Only those aliases may vary that metadata: digest, device, inode and size
    # must still match, and _identity() rechecks links and a stable open/read.
    from gateway.session_ingress_media import _media_root
    snapshot = Path(resolved)
    return (identity[:3] == stored[:3] and snapshot.parent.parent == _media_root().resolve()
            and snapshot.parent.name == digest)


def verify_receipt(source, receipt, path=None, *, message_id=None, request_id=None):
    if not isinstance(receipt, str) or len(receipt) != 32:
        return None
    with _database() as connection:
        row = connection.execute('SELECT scope,chat,actor,message,created,path,identity,digest,mime,request '
                                 'FROM receipts WHERE id=?', (receipt,)).fetchone()
    if not row or tuple(row[:3]) != _scope(source) or not 0 <= time.time() - row[4] <= TTL:
        return None
    if message_id is not None and row[3] != message_id:
        return None
    if request_id is not None and row[9] != request_id:
        return None
    try:
        resolved, identity, digest = _identity(path or row[5])
        if digest != row[7] or not _matches_identity(resolved, identity, json.loads(row[6]), digest):
            return None
        # A relocated path must itself have been recorded by the native capture seam.
        if path is not None and resolved != row[5]:
            return None
    except (OSError, ValueError):
        return None
    return resolved, row[8], row[3]


def _receipts(event):
    values = getattr(event, '_inbound_attachment_receipts', ())
    if not isinstance(values, (list, tuple)) or not event.message_id:
        return []
    return [item for item in values[:MAX_FILES]
            if isinstance(item, (list, tuple)) and len(item) == 2
            and isinstance(item[0], str) and len(item[0]) == 32 and type(item[1]) is int]


def relocate_receipts(event, media):
    """Validate original receipts then bind exact captured native bytes by index."""
    result = []
    for receipt, index in _receipts(event):
        if not isinstance(index, int) or not 0 <= index < len(media):
            continue
        original = verify_receipt(event.source, receipt, event.media_urls[index], request_id=event.message_id)
        if original is None:
            continue
        resolved, identity, digest = _identity(media[index]['path'])
        with _database() as connection:
            _prune(connection)
            old = connection.execute('SELECT digest FROM receipts WHERE id=?', (receipt,)).fetchone()
            if old is None or old[0] != digest:
                continue
            relocated = uuid.uuid5(uuid.NAMESPACE_URL, receipt + resolved).hex
            connection.execute('INSERT OR IGNORE INTO receipts '
                               'SELECT ?,scope,chat,actor,message,created,?,?,digest,mime,request '
                               'FROM receipts WHERE id=?',
                               (relocated, resolved, json.dumps(identity), receipt))
        result.append([relocated, index])
    return result


def eligible_paths(event):
    paths = []
    for receipt, index in _receipts(event):
        if type(index) is not int or not 0 <= index < len(event.media_urls):
            continue
        verified = verify_receipt(event.source, receipt, event.media_urls[index], request_id=event.message_id)
        if verified is not None:
            paths.append(verified[0])
    return paths


def own_pending_attachments(event, message_ids):
    source = event.source
    if not message_ids or not event.message_id:
        return []
    with _database() as connection:
        rows = connection.execute('SELECT id FROM receipts WHERE scope=? AND chat=? AND actor=? '
                                  'AND created>=? ORDER BY created DESC LIMIT 32',
                                  (*_scope(source), time.time() - TTL)).fetchall()
    result = []
    seen = set()
    for (receipt,) in rows:
        verified = verify_receipt(source, receipt)
        if verified and verified[2] in message_ids and verified[0] not in seen:
            seen.add(verified[0])
            bound = uuid.uuid5(uuid.NAMESPACE_URL, receipt + str(event.message_id)).hex
            with _database() as connection:
                _prune(connection)
                connection.execute('INSERT OR IGNORE INTO receipts '
                                   'SELECT ?,scope,chat,actor,message,created,path,identity,digest,mime,? '
                                   'FROM receipts WHERE id=?', (bound, event.message_id, receipt))
            result.append((bound, *verified))
        if len(result) >= MAX_FILES:
            break
    return result
