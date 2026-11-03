"""QQ final-delivery overrides, with optional memes outside the text obligation."""
from __future__ import annotations

import logging

from .delivery_binding import bind_delivery, turn_context
from .long_reply import FINAL_METADATA_KEY
from .memes import controller_for, mark_response

logger = logging.getLogger(__name__)


class MemeDeliveryMixin:
    def snapshot_native_turn(self, event):
        from .meme_native import snapshot
        return snapshot(self, event)

    def restore_native_turn(self, event, state):
        from .meme_native import restore
        restore(self, event, state)

    def bridge_native_turn(self, executed, waiting):
        from .meme_native import bridge
        bridge(self, executed, waiting)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult
        from .meme_expression import parse_expression
        if controller_for(self).settings.mode == 'expression' and parse_expression(content).judged:
            return SendResult(success=False, error='Expression directive requires final delivery')
        with bind_delivery(chat_id):
            preview = await self._long_reply_preview.maybe_send(self, chat_id, content, reply_to, metadata)
            if preview is not None:
                return preview
            return await self._send_text_impl(chat_id, content, reply_to, metadata)

    async def deliver_queued_response(self, response, event, session_key, metadata=None, *,
                                       text_already_delivered=False, deliver_media=True):
        from .meme_expression import parse_expression
        from .meme_expression_delivery import engine_for
        if controller_for(self).settings.mode != 'expression' or not parse_expression(response).judged:
            return None
        if text_already_delivered:
            return True  # QQ never streams expression directives; do not replay an acknowledged final.
        results = []
        try:
            extracted = await self._extract_response_content(response, event, session_key,
                is_ephemeral_response=not deliver_media)
            if extracted.text_content:
                result, _ = await self.send_final_ledgered(event, session_key, extracted.text_content,
                    metadata, reply_to=event.message_id)
                results.append(result)
                if not result.success:
                    return False
            if deliver_media:
                await self._deliver_attachments(event, extracted, metadata,
                    anything_sent=bool(results), record_delivery=results.append)
            # True means handled, not a fabricated platform receipt. A GIF-only
            # rejection/unknown is terminal; falling back would replay the directive.
            if parse_expression(response).marked and not extracted.text_content:
                return True
            return any(result.success for result in results)
        finally:
            delivery = getattr(event, '_qq_expression_delivery', None)
            if delivery is not None and delivery.snapshot is not None and not delivery.delivered:
                engine_for(self)._stop(delivery, 'error', 'queued_turn_ended_before_send')

    async def prepare_turn_notes(self, event, session_id, session_key, history):
        from .meme_expression_delivery import engine_for
        with self._media_delivery_scope(event.source):
            return await engine_for(self).prepare_turn_notes(self, event, session_id, session_key, history)

    async def _process_message_background(self, event, session_key):
        from .meme_expression_delivery import engine_for
        try:
            return await super()._process_message_background(event, session_key)
        finally:
            delivery = getattr(event, '_qq_expression_delivery', None)
            if delivery is not None and delivery.snapshot is not None and not delivery.delivered:
                with self._media_delivery_scope(event.source):
                    engine_for(self)._stop(delivery, 'error', 'turn_ended_before_send')

    async def _record_delivery_obligation(self, event, session_key, text_content,
                                          delivery_adapter, is_ephemeral_response):
        # A restarted process cannot restore an in-memory long-reply button token.
        if delivery_adapter._long_reply_preview.eligible(delivery_adapter, event.source.chat_id, text_content):
            return None
        return await super()._record_delivery_obligation(
            event, session_key, text_content, delivery_adapter, is_ephemeral_response)

    async def _extract_response_content(self, response, event, session_key, *, is_ephemeral_response):
        from .meme_expression import parse_expression
        from .meme_expression_delivery import engine_for
        expression = parse_expression(response) if controller_for(self).settings.mode == 'expression' else None
        extracted = await super()._extract_response_content(
            expression.text if expression else response, event, session_key, is_ephemeral_response=is_ephemeral_response)
        mark_response(event, extracted, is_ephemeral_response)
        if expression is not None and not expression.judged:
            from .memes import eligible
            if eligible(self, event, extracted.text_content or 'x', is_ephemeral_response,
                        controller_for(self).settings):
                # Coverage, not a gate: an eligible turn that never gave a verdict is measurable.
                logger.info('QQ expression: turn without verdict chat=%s session=%s',
                            event.source.chat_id, session_key)
        elif expression is not None and not expression.marked:
            logger.info('QQ expression: none verdict chat=%s session=%s',
                        event.source.chat_id, session_key)
        if expression is not None and expression.marked:
            if getattr(event, '_adapter_chained_response', False):
                # The outer event cannot authorize an image chosen by a different terminal turn.
                previous = getattr(event, '_qq_expression_delivery', None)
                if previous is not None and previous.snapshot is not None:
                    engine_for(self)._stop(previous, 'error', 'different_queued_origin')
                event._qq_expression_delivery = None
                return extracted
            adapter = self._final_delivery_adapter(event.source)
            with adapter._media_delivery_scope(event.source):
                engine_for(adapter).prepare(adapter, event, session_key, expression, extracted, is_ephemeral_response)
        return extracted

    async def send_final_ledgered(self, event, session_key, text_content, metadata, *,
                                  reply_to, is_ephemeral_response=False):
        metadata = {**(metadata or {}), FINAL_METADATA_KEY: not is_ephemeral_response}
        with bind_delivery(event.source.chat_id, turn_context(event, session_key)):
            result, delivery_adapter = await super().send_final_ledgered(
                event, session_key, text_content, metadata,
                reply_to=reply_to, is_ephemeral_response=is_ephemeral_response)
            expression_delivery = getattr(event, '_qq_expression_delivery', None)
            if expression_delivery is not None:
                expression_delivery.text_result = result
            if result.success and isinstance(result.message_id, str) and result.message_id.strip():
                try:
                    # A reconnect may have replaced self; only the actual recipient transport sends.
                    with delivery_adapter._media_delivery_scope(event.source):
                        await controller_for(delivery_adapter).after_text(
                            delivery_adapter, event, session_key, text_content, ephemeral=is_ephemeral_response)
                except Exception as exc:
                    # Decorations must never turn a successful text send into a replayable failure.
                    logger.warning('QQ optional meme unavailable: %s', type(exc).__name__)
            return result, delivery_adapter

    async def _deliver_attachments(self, event, extracted, metadata, *, anything_sent, record_delivery):
        from .meme_expression_delivery import engine_for
        with bind_delivery(event.source.chat_id, turn_context(event, self._source_session_key(event.source))):
            delivery = getattr(event, '_qq_expression_delivery', None)
            if delivery is not None:
                snapshot = delivery.attachment
                if snapshot:
                    extracted.media_files[:] = [p for p in extracted.media_files if p[0] != snapshot]
                adapter = self._final_delivery_adapter(event.source)
                with adapter._media_delivery_scope(event.source):
                    result = await engine_for(adapter).deliver(adapter, event, delivery)
                if result is not None:
                    record_delivery(result)
                # An expression-only final is a native attachment, not a dropped text reply.
                if not extracted.text_content and not (extracted.images or extracted.media_files or extracted.local_files):
                    return
            return await super()._deliver_attachments(
                event, extracted, metadata, anything_sent=anything_sent, record_delivery=record_delivery)
