"""Task-local linkage of a logical reply to its separate platform send receipts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import uuid


@dataclass(frozen=True)
class DeliveryContext:
    chat_id: str
    logical_id: str
    session_key: str | None = None
    source_message_id: str | None = None
    source_quote_id: str | None = None


_current: ContextVar[DeliveryContext | None] = ContextVar('qq_delivery_context', default=None)


def turn_context(event, session_key: str) -> DeliveryContext:
    source_id = getattr(event, 'ledger_message_id', None) or event.message_id or None
    if source_id:
        digest = hashlib.sha256(json.dumps([session_key, source_id], ensure_ascii=True).encode()).hexdigest()
        logical_id = 'turn-' + digest
    else:
        logical_id = 'local-' + uuid.uuid4().hex
    return DeliveryContext(event.source.chat_id, logical_id, session_key, source_id,
                           getattr(event, 'reply_to_message_id', None))


def quoted_message_id(data: dict) -> str | None:
    reference = data.get('message_reference')
    value = reference.get('message_id') if isinstance(reference, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def current_delivery(chat_id: str) -> DeliveryContext:
    current = _current.get()
    if current is not None and current.chat_id == chat_id:
        return current
    return DeliveryContext(chat_id, 'local-' + uuid.uuid4().hex)


@contextmanager
def bind_delivery(chat_id: str, context: DeliveryContext | None = None):
    selected = context if isinstance(context, DeliveryContext) and context.chat_id == chat_id else current_delivery(chat_id)
    token = _current.set(selected)
    try:
        yield selected
    finally:
        _current.reset(token)
