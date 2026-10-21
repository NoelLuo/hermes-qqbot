"""Runtime-only producer-to-transport handoff; never reconstruct grants from transcript text."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from agent.request_security import RequestSecurityPolicy, _artifact_identity, _artifact_key, _current_export_turn
from hermes_constants import get_hermes_home


@dataclass
class ExportTurn:
    policy: RequestSecurityPolicy
    message_id: str
    source_key: tuple
    home: str
    consumed: bool = False


_PENDING_TEXT = '文件已生成，正在尝试发送附件。'
_DELIVERY_CLAIM = re.compile(
    r'(?:已经|已|成功).{0,16}(?:发送|发出|送达|返回|收到)'
    r'|(?:发送|送达).{0,6}(?:成功|完成)'
    r'|\b(?:sent|delivered|received|returned)\b.{0,60}\b(?:file|attachment|screenshot|image)'
    r'|\b(?:file|attachment|screenshot|image)\b.{0,60}\b(?:sent|delivered|received|returned)\b', re.IGNORECASE)


def pending_export_text(response):
    """Keep useful analysis; omit directives and premature delivery-status lines.

    This is display normalization, not a receipt: only transport results settle delivery.
    """
    lines = [line for line in str(response or '').splitlines()
             if line.strip() != _PENDING_TEXT and 'MEDIA:' not in line
             and not _DELIVERY_CLAIM.search(line)]
    body = '\n'.join(lines).strip()
    return _PENDING_TEXT + ('\n\n' + body if body else '')


def _source_key(source):
    return tuple(getattr(source, key, None) for key in (
        'platform', 'chat_id', 'user_id', 'profile', 'thread_id'))


def begin_export_turn(policy, source, message_id):
    """Called at real execution, so a reused actor/source cannot inherit last turn's exports."""
    if (type(policy) is not RequestSecurityPolicy or not policy.public_context
            or policy.platform != 'qqbot'
            or getattr(source, '_request_security_policy', None) is not policy
            or (policy.platform, policy.chat_id, policy.subject_id) != (
                getattr(getattr(source, 'platform', None), 'value', None),
                getattr(source, 'chat_id', None), getattr(source, 'user_id', None))):
        return
    turn = ExportTurn(policy, str(message_id or ''), _source_key(source),
                      _artifact_key(get_hermes_home()))
    with policy._exports.lock:
        with policy._artifacts._lock:
            for path in policy._exports.records:
                policy._artifacts._files.pop(path, None)
        policy._exports.turn = turn
        policy._exports.records.clear()
    source._guest_export_turn = turn
    return _current_export_turn.set(turn)


@contextmanager
def export_turn_scope(policy, source, message_id):
    token = begin_export_turn(policy, source, message_id)
    try:
        yield
    finally:
        if token is not None:
            _current_export_turn.reset(token)


def bridge_export_turn(executed, waiting):
    """Only the admission owner calls this with its exact original waiting event."""
    turn = getattr(executed.source, '_guest_export_turn', None)
    if (type(turn) is not ExportTurn or turn.message_id != str(executed.message_id or '')
            or turn.message_id != str(waiting.message_id or '')
            or turn.source_key != _source_key(waiting.source)):
        return False
    previous = getattr(waiting.source, '_request_security_policy', None)
    if type(previous) is not RequestSecurityPolicy or previous.cache_key() != turn.policy.cache_key():
        return False
    waiting.source._guest_export_turn = turn
    # Live object only, no wire/durable payload or actor-indexed "last policy" registry.
    waiting.source._request_security_policy = turn.policy
    return True


def take_export_response(response, event):
    """Claim once, validate identity AND bytes; model directives never create records."""
    event._guest_export_delivery_required = False
    turn = getattr(event.source, '_guest_export_turn', None)
    denied = [] if type(turn) is ExportTurn else None
    if (type(turn) is not ExportTurn or turn.message_id != str(event.message_id or '')
            or turn.source_key != _source_key(event.source)
            or turn.home != _artifact_key(get_hermes_home())):
        return ('附件交付请求不匹配，未发送。', []) if denied is not None else (response, None)
    policy = turn.policy
    if getattr(event.source, '_request_security_policy', None) is not policy:
        return ('附件交付请求不匹配，未发送。', []) if denied is not None else (response, None)
    with policy._exports.lock:
        if policy._exports.turn is not turn or turn.consumed:
            return '附件交付请求已失效，未重复发送。', []
        turn.consumed = True
        records = dict(policy._exports.records)
    valid = []
    for path, (identity, digest) in records.items():
        try:
            if (_artifact_identity(path) == (path, identity) and policy._artifacts.allows(path)
                    and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest):
                valid.append(path)
        except OSError:
            continue
    if not records:
        return response, None
    event._guest_export_delivery_required = True
    event._guest_export_validation_failed = len(valid) != len(records)
    # Do not relay a model's premature "delivered" claim before the platform API has run.
    text = (pending_export_text(response) if valid else '文件附件校验失败，未发送。')
    if len(valid) != len(records) and valid:
        text += ' 部分附件校验失败，未发送。'
    return text, valid


def check_export_delivery(event, results):
    """A text ACK must never mask an attachment rejection in the actual lifecycle."""
    if not getattr(event, '_guest_export_delivery_required', False):
        return
    if (getattr(event, '_guest_export_validation_failed', False) or not results
            or any(not getattr(result, 'success', False) for result in results)):
        raise RuntimeError('file_attachment_delivery_failed: 文件附件发送失败，未确认送达。')
