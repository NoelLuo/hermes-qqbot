"""QQ adapter integration: journal before POST, normalize explicit recall outcomes."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict

from .delivery_binding import current_delivery
from .delivery_receipts import GroupRecallService, OutgoingReceiptJournal, RecallHTTPResult


class QQAPIRejection(RuntimeError):
    """An explicit HTTP/API refusal, not an ambiguous network failure."""


def receipt_journal(adapter, api_base: str) -> OutgoingReceiptJournal:
    root = adapter._receipt_home
    if root is None:
        raise RuntimeError('QQ receipt storage has no owning profile root')
    owner = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    transport = hashlib.sha256((api_base + '\0' + adapter._app_id).encode()).hexdigest()
    return OutgoingReceiptJournal(root, owner=owner, transport_owner=transport)


async def recorded_post(adapter, api_base: str, path: str, body: dict):
    prefixes = (('/v2/groups/', 'group'), ('/v2/users/', 'c2c'), ('/channels/', 'guild'),
                ('/dms/', 'guild'))
    route = next(((prefix, kind) for prefix, kind in prefixes
                  if path.startswith(prefix) and path.endswith('/messages')), None)
    if route is None:
        raise ValueError('unsupported QQ message receipt route')
    prefix, kind = route
    chat_id = path[len(prefix):-len('/messages')]
    context = current_delivery(chat_id)
    journal = await asyncio.to_thread(receipt_journal, adapter, api_base)
    logical_id = await asyncio.to_thread(journal.begin_send, chat_id=chat_id, chat_type=kind,
        logical_id=context.logical_id, source_session_key=context.session_key,
        source_message_id=context.source_message_id, source_quote_id=context.source_quote_id)
    part = await asyncio.to_thread(journal.append_part, logical_id, chat_id=chat_id, success=None)
    try:
        data = await adapter._api_request('POST', path, body)
    except QQAPIRejection:
        await asyncio.to_thread(journal.finish_part, logical_id, chat_id=chat_id,
                                part_index=part.part_index, success=False)
        raise
    # Other exceptions/cancellation leave the already durable unknown record.
    receipt = await asyncio.to_thread(journal.finish_part, logical_id, chat_id=chat_id,
        part_index=part.part_index, returned_message_id=data.get('id'), success=True)
    return data, receipt


async def recall_message(adapter, api_base: str, *, logical_id: str, group_id: str,
                         message_id: str, retry_unknown: bool = False):
    journal = await asyncio.to_thread(receipt_journal, adapter, api_base)

    async def delete_transport(method, path):
        client = adapter._require_http_client()
        response = await client.request(method, api_base + path,
                                        headers=await adapter._auth_headers(), timeout=30.0)
        if response.status_code != 200:
            return RecallHTTPResult(response.status_code, acknowledged=False)
        if not response.content:
            return RecallHTTPResult(200, acknowledged=True)
        try:
            data = response.json()
        except ValueError:
            return RecallHTTPResult(200)
        if not isinstance(data, dict):
            return RecallHTTPResult(200)
        return RecallHTTPResult(200, acknowledged=data.get('code') in (None, 0, '0'))

    service = GroupRecallService(journal, owner=journal.owner,
        transport_owner=journal.transport_owner, delete_transport=delete_transport)
    return await service.recall(logical_id, group_id=group_id, message_id=message_id,
                                retry_unknown=retry_unknown)


def receipt_metadata(receipt) -> dict:
    # The journal and this public metadata contain identifiers/status only, not bodies.
    return asdict(receipt)
