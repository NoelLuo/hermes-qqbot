"""Quiet attachment persistence and same-author followup eligibility."""
from __future__ import annotations

import asyncio

from gateway.inbound_attachment_proofs import own_pending_attachments, record_attachment


async def cache_quiet_attachments(adapter, event, attachments, images):
    files = [item for item in attachments if isinstance(item, dict)
             and not str(item.get('content_type', '')).startswith('image/')
             and not adapter._is_voice_content_type(str(item.get('content_type', '')),
                                                    str(item.get('filename', '')))] if isinstance(attachments, list) else []
    try:
        async with asyncio.timeout(45), adapter._group_images._downloads:
            cached = await adapter._process_attachments(files)
    except TimeoutError:
        cached = {'image_urls': [], 'image_media_types': [], 'attachment_info': '[附件不可用：缓存超时]'}
    pairs = [(image['path'], image['mime']) for image in images if image.get('status') == 'cached']
    pairs += list(zip(cached['image_urls'], cached['image_media_types']))
    for path, mime in pairs[:4]:
        await asyncio.to_thread(record_attachment, event, path, mime)
    event._quiet_attachment_status = cached['attachment_info']


async def attach_own_pending(rows, event):
    from .group_history import is_observed

    pending = set()
    for row in rows:
        if is_observed(row):
            if row.get('message_id'):
                pending.add(row['message_id'])
        elif row.get('role') == 'user':
            pending.clear()
    own = await asyncio.to_thread(own_pending_attachments, event, pending)
    receipts = list(getattr(event, '_inbound_attachment_receipts', ()))
    for receipt, path, mime, message_id in own:
        if len(receipts) >= 4:
            break
        if path not in event.media_urls:
            event.media_urls.append(path)
            event.media_types.append(mime)
            if event.media_text_inlined:
                event.media_text_inlined.append(False)
        index = event.media_urls.index(path)
        receipts.append((receipt, index))
    event._inbound_attachment_receipts = receipts
