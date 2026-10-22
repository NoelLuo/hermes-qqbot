"""Immutable requester grants, independent of prompts and optional human approvals.

This is an application boundary, not an OS sandbox. None preserves legacy callers;
configured QQ requests always get an explicit policy, including unknown subjects.
"""
from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from urllib.parse import urlsplit

PUBLIC_TOOLS = frozenset({
    'web_search', 'web_extract', 'vision_analyze', 'text_to_speech', 'cronjob_manage',
    'guest_workspace', 'guest_browser',
})
PUBLIC_COMMANDS = frozenset({'help', 'whoami'})


def _public_url(value: Any) -> bool:
    """Reject local schemes/obvious private hosts; network clients still enforce DNS/redirect SSRF checks."""
    if not isinstance(value, str) or not value or '\\' in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower().rstrip('.')
        if parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password:
            return False
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.lan', '.internal')):
            return False
        try:
            return ip_address(host).is_global
        except ValueError:
            return '.' in host
    except ValueError:
        return False


def _artifact_key(value: Any) -> str | None:
    if not isinstance(value, (str, Path)) or not value:
        return None
    # Never stat an ungranted UNC/network path: the lookup itself can authenticate
    # to an attacker-controlled SMB server before a file permission check rejects it.
    text = str(value)
    if text.startswith(('\\', '//')) or '\x00' in text:
        return None
    return os.path.normcase(os.path.abspath(os.path.expanduser(text)))


def _artifact_identity(value: Any) -> tuple[str, tuple] | None:
    if _artifact_key(value) is None:
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            return None
        info = path.stat()
        return _artifact_key(path), (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except (OSError, RuntimeError, ValueError):
        return None


class _RequestArtifacts:
    """Runtime-only, per-request file grants; never serialized into a cron job or source row."""

    def __init__(self) -> None:
        self._files: dict[str, tuple] = {}
        self._lock = Lock()

    def grant(self, value: Any) -> bool:
        item = _artifact_identity(value)
        if item is None:
            return False
        with self._lock:
            self._files[item[0]] = item[1]
        return True

    def allows(self, value: Any) -> bool:
        key = _artifact_key(value)
        with self._lock:
            if key is None or key not in self._files:
                return False
        item = _artifact_identity(value)
        if item is None:
            return False
        with self._lock:
            return self._files.get(item[0]) == item[1]


class _RequestExports:
    """Successful producer snapshots, separate from incoming attachment grants."""

    def __init__(self):
        self.lock = Lock()
        self.turn = None
        self.records = {}

    def record(self, policy, path):
        from hermes_constants import get_hermes_home
        if self.turn is None:
            return True  # Legacy direct store callers have no outbound lifecycle.
        if (current_policy() is not policy or _current_export_turn.get() is not self.turn
                or self.turn.policy is not policy):
            return False
        item = _artifact_identity(path)
        if item is None or not policy._artifacts.allows(path):
            return False
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        with self.lock:
            if (self.turn is not _current_export_turn.get()
                    or self.turn.home != _artifact_key(get_hermes_home())):
                return False
            self.records[item[0]] = (item[1], digest)
        return True

    def unchanged(self, path):
        key = _artifact_key(path)
        with self.lock:
            record = self.records.get(key)
        if record is None:
            return True
        try:
            identity, digest = record
            return (_artifact_identity(path) == (key, identity)
                    and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest)
        except OSError:
            return False


@dataclass(frozen=True)
class RequestSecurityPolicy:
    subject_id: str
    platform: str
    chat_id: str
    is_admin: bool = False
    public_context: bool = True
    allowed_tools: frozenset[str] = PUBLIC_TOOLS
    allowed_commands: frozenset[str] = PUBLIC_COMMANDS
    scope_id: str = 'default'
    _exports: _RequestExports = field(default_factory=_RequestExports, compare=False, repr=False)
    _artifacts: _RequestArtifacts = field(default_factory=_RequestArtifacts, compare=False, repr=False)

    def __post_init__(self) -> None:
        for name in ('subject_id', 'platform', 'chat_id', 'scope_id'):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f'{name} must be a string')
        if type(self.is_admin) is not bool or type(self.public_context) is not bool:
            raise ValueError('policy flags must be booleans')
        for name in ('allowed_tools', 'allowed_commands'):
            values = getattr(self, name)
            if not isinstance(values, frozenset) or any(not isinstance(x, str) or not x for x in values):
                raise ValueError(f'{name} must be a frozen set of names')
        if not self.is_admin and not self.allowed_tools.issubset(PUBLIC_TOOLS):
            raise ValueError('public policy cannot grant an execution or private-data tool')
        if not self.is_admin and not self.public_context:
            raise ValueError('guest policy cannot load private context')

    def allows_command(self, command: str) -> bool:
        if not isinstance(command, str) or not self.subject_id or not self.chat_id:
            return False
        parts = command.strip().lstrip('/').split(maxsplit=1)
        name = parts[0].lower() if parts else ''
        # Extra command names cannot turn a public control grant into admin rights.
        return self.is_admin or name in (self.allowed_commands & PUBLIC_COMMANDS)

    def allows_tool(self, name: str, args: dict[str, Any] | None = None) -> bool:
        if not self.subject_id or not self.chat_id or not isinstance(name, str):
            return False
        if self.is_admin:
            return True
        if name not in self.allowed_tools:
            return False
        if name == 'cronjob_manage':
            return isinstance(args, dict) and args.get('action') == 'create'
        if name == 'guest_workspace':
            return isinstance(args, dict) and isinstance(args.get('action'), str) and args['action'] in {
                'info', 'list', 'read', 'write', 'mkdir', 'copy', 'move', 'search',
                'organize_csv', 'import_attachment', 'export', 'run_python', 'run_office', 'delete', 'cleanup',
            } and (args['action'] != 'delete' or args.get('confirm') is True)
        if name == 'guest_browser':
            return isinstance(args, dict) and isinstance(args.get('action'), str) and args['action'] in {
                'info', 'browse',
            }
        if name == 'text_to_speech':
            return isinstance(args, dict) and args.get('output_path') in (None, '')
        if name == 'vision_analyze':
            if not isinstance(args, dict):
                return False
            image = args.get('image_url')
            data_image = isinstance(image, str) and image.startswith((
                'data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,',
                'data:image/gif;base64,'))
            return data_image or _public_url(image) or self._artifacts.allows(image)
        if name == 'web_extract':
            urls = args.get('urls') if isinstance(args, dict) else None
            return isinstance(urls, list) and bool(urls) and all(_public_url(url) for url in urls)
        return True

    def grant_artifact(self, path: Any) -> bool:
        """Trusted ingress/tool code only. Never grant paths parsed from untrusted page text."""
        return bool(self.subject_id and self.chat_id) and self._artifacts.grant(path)

    def allows_media(self, path: Any) -> bool:
        return bool(self.subject_id and self.chat_id) and self._exports.unchanged(path) and (
            self.is_admin or _public_url(path)
            or self._artifacts.allows(path))

    def cache_key(self) -> tuple:
        return (self.scope_id, self.platform, self.chat_id, self.subject_id, self.is_admin,
                self.public_context, tuple(sorted(self.allowed_tools)), tuple(sorted(self.allowed_commands)))

    def to_payload(self) -> dict[str, Any]:
        """Only attenuated public policies may cross a durable job boundary."""
        if self.is_admin or not self.public_context:
            raise ValueError('privileged policies cannot be serialized as public jobs')
        return {'version': 1, 'subject_id': self.subject_id, 'platform': self.platform,
                'chat_id': self.chat_id, 'scope_id': self.scope_id, 'is_admin': False,
                'public_context': True, 'allowed_tools': sorted(self.allowed_tools - {'cronjob_manage'}),
                'allowed_commands': sorted(self.allowed_commands)}

    @classmethod
    def from_payload(cls, payload: Any) -> RequestSecurityPolicy:
        if not isinstance(payload, dict) or type(payload.get('version')) is not int or payload['version'] != 1:
            raise ValueError('unsupported public request policy')
        if payload.get('is_admin') is not False or payload.get('public_context') is not True:
            raise ValueError('durable public policy must not escalate privileges')
        required = ('subject_id', 'platform', 'chat_id', 'scope_id')
        if any(not isinstance(payload.get(k), str) or not payload[k] for k in required):
            raise ValueError('public policy is missing its bound identity')
        if payload['platform'] != 'qqbot':
            raise ValueError('unsupported public policy platform')
        grants = {}
        for key in ('allowed_tools', 'allowed_commands'):
            value = payload.get(key)
            if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError('invalid public policy grants')
            grants[key] = frozenset(value)
        # Earlier public jobs could advertise clarify, but busy QQ visitors have
        # no requester-bound reply lane yet. Attenuate that legacy grant in memory
        # instead of exposing an unanswerable prompt or rewriting saved jobs.
        grants['allowed_tools'] = grants['allowed_tools'] - {'clarify'}
        if not grants['allowed_commands'].issubset(PUBLIC_COMMANDS):
            raise ValueError('public job cannot grant management commands')
        if 'cronjob_manage' in grants['allowed_tools']:
            raise ValueError('public job cannot create further jobs')
        return cls(**{key: payload[key] for key in required}, **grants)


_current_export_turn: ContextVar = ContextVar('guest_export_turn', default=None)
_current_policy: ContextVar[RequestSecurityPolicy | None] = ContextVar('request_security_policy', default=None)


def current_policy() -> RequestSecurityPolicy | None:
    return _current_policy.get()


@contextmanager
def policy_scope(policy: RequestSecurityPolicy | None) -> Iterator[None]:
    if policy is not None and not isinstance(policy, RequestSecurityPolicy):
        raise ValueError('invalid request policy object')
    token = _current_policy.set(policy)
    try:
        yield
    finally:
        _current_policy.reset(token)
