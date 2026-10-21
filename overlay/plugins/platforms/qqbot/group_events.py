"""Classify group delivery before deduplication; serialize upgrades per source event."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from weakref import WeakValueDictionary

from gateway.platforms.event import MessageEvent

from .group_history import is_observed


class GroupEventDispatcher:
    def __init__(self):
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()

    async def dispatch(self, adapter, event_type: str, data: dict) -> None:
        mid = str(data.get('id') or '')
        group = str(data.get('group_openid') or '')
        author = data.get('author') if isinstance(data.get('author'), dict) else {}
        member = str(author.get('member_openid') or '')
        if not mid or not group or not member or not adapter._is_group_allowed(group, member):
            return
        authorized = adapter._is_sender_authorized(member, 'group', group)
        if authorized is False:
            return
        addressed = event_type == 'GROUP_AT_MESSAGE_CREATE' or adapter._is_at_bot(data)
        attachments = data.get('attachments')
        has_attachments = isinstance(attachments, list) and any(isinstance(item, dict) for item in attachments[:4])
        if not addressed and not adapter._group_reply_gate.enabled(group) and not has_attachments:
            return
        source = adapter.build_source(chat_id=group, user_id=member, chat_type='group')
        event = MessageEvent(source=source, message_id=mid, text=str(data.get('content') or ''))
        if adapter._drop_unresolved(event):
            return
        key = adapter._source_session_key(source)
        lock_key = (key, mid)
        lock = self._locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            runner = getattr(adapter, 'gateway_runner', None)
            scope = runner._async_profile_scope_for_source(source) if runner is not None else nullcontext()
            async with scope:
                await self._deliver(adapter, event_type, data, author, source, mid, key, addressed, authorized)

    async def _deliver(self, adapter, event_type, data, author, source, mid, key, addressed, authorized):
        active_key = f'{key}:{mid}:addressed'
        passive_key = f'{key}:{mid}:observed'
        if adapter._dedup.contains(active_key):
            return
        store = getattr(adapter, '_session_store', None)
        # Reading external history must not become an authorization bypass when
        # the runner has not supplied an affirmative sender check.
        if store is not None and authorized is True:
            entry = await asyncio.to_thread(store.get_or_create_session, source, touch_activity=False)
            rows = await asyncio.to_thread(store.load_transcript, entry.session_id, repair_alternation=False)
            matching = [r for r in rows if r.get('message_id') == mid and r.get('role') == 'user']
            if any(not is_observed(row) for row in matching):
                adapter._dedup.is_duplicate(active_key)
                return
            if matching and not addressed:
                return
        claim = active_key if addressed else passive_key
        if adapter._dedup.is_duplicate(claim):
            return
        try:
            handler = getattr(adapter, adapter._INBOUND_HANDLERS[event_type])
            selected = await handler(data, mid, str(data.get('content') or '').strip(),
                                     author, str(data.get('timestamp') or ''))
        except BaseException:
            # A durable user row, if already accepted, will still prevent retry.
            adapter._dedup.discard(claim)
            raise
        if selected is True:
            adapter._dedup.is_duplicate(active_key)
