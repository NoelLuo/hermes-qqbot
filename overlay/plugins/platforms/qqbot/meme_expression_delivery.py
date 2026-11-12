"""QQ-owned expression lifecycle, independent of the assistant's text obligation."""
from __future__ import annotations

import asyncio
import logging

from .meme_catalog import MemeCatalog
from .meme_expression import (
    RECENT_SENT_WINDOW, ExpressionContext, ExpressionDelivery, capability_note, turn_choice_note,
)
from .meme_expression_state import ExpressionJournal
from .memes import controller_for, eligible, eligible_source
from .delivery_binding import turn_context

logger = logging.getLogger(__name__)


def _same_home(controller):
    from hermes_constants import get_hermes_home
    return controller.home is not None and get_hermes_home().resolve() == controller.home.resolve()


def _audiences(event, settings):
    if event.source.chat_type == 'group':
        return frozenset({'group'})
    return frozenset({'dm', 'intimate_dm'} if event.source.user_id in settings.intimacy_users else {'dm'})


def _chat(event):
    return event.source.chat_type + ':' + event.source.chat_id


def _catalog(controller):
    settings = controller.settings
    path = settings.catalog or str(controller.home / 'qqbot/memes/catalog.json')
    return MemeCatalog.load(settings.root, path)


def _notes_with_fact(context, fact):
    notes = list(context.notes)
    if fact:
        notes.insert(len(notes) if context.entries else 0, fact)
    return notes


class ExpressionEngine:
    def __init__(self, controller):
        self.controller = controller
        self.journal = ExpressionJournal(controller.state) if controller.state is not None else None

    async def prepare_turn_notes(self, adapter, event, session_id, session_key, history):
        ctrl, settings = self.controller, self.controller.settings
        if (settings.mode != 'expression' or not settings.enabled or not self.journal
                or not isinstance(session_id, str) or not session_id or not _same_home(ctrl)
                or not eligible_source(adapter, event, settings)):
            return []
        # Receipts stay live even when the same event reuses its choice snapshot.
        fact = self.journal.recent_note(_chat(event), session_id)
        previous = getattr(event, '_qq_expression_context', None)
        if (isinstance(previous, ExpressionContext) and previous.session_id == session_id
                and previous.session_key == session_key and previous.home == ctrl.home
                and previous.transport == ctrl.state.transport and previous.notes):
            # Preparation reentry keeps one prompt snapshot; sending still rechecks live gates.
            return _notes_with_fact(previous, fact)
        try:
            audiences = _audiences(event, settings)
            entries = tuple(entry for entry in _catalog(ctrl).entries
                            if entry.enabled and entry.reviewed and entry.audiences & audiences)
        except Exception as exc:
            logger.warning('QQ expression catalog unavailable: %s', type(exc).__name__)
            entries = ()
        if not entries:
            notes = [
                '[本轮QQ表情可用性] status=unavailable；本轮在末行写 [[qq_meme_none]]，正常回复文字，不发送新图。']
        else:
            note = capability_note(entries)
            # The one-time catalog rides a NEW user sidecar, never rewrites a cached system prefix.
            # Exact full-text matching is safe: a pasted identical catalog carries no extra authority.
            present = any(note in str(row.get('api_content') or row.get('content') or '')
                          for row in history if isinstance(row, dict))
            notes = [] if present else [note]
            availability = self.journal.availability(_chat(event), ctrl.clock(), settings)
            # One scoped snapshot keeps the hard exclusions and soft usage hints consistent.
            recent_sent = ctrl.state.recent(_chat(event), max(settings.recent, RECENT_SENT_WINDOW))
            excluded = recent_sent[:settings.recent]
            pure_availability = self.journal.availability(_chat(event), ctrl.clock(), settings, sole_reply=True)
            notes.append(turn_choice_note(entries, availability, excluded, recent_sent=recent_sent,
                                          pure_availability=pure_availability))
        event._qq_expression_context = ExpressionContext(
            session_id, session_key, ctrl.home, ctrl.state.transport, entries, tuple(notes))
        return _notes_with_fact(event._qq_expression_context, fact)

    def prepare(self, adapter, event, session_key, expression, extracted, ephemeral):
        """Resolve an already-selected ID to exact granted bytes, before any network I/O."""
        from gateway.platforms.base import SendResult
        ctrl, context = self.controller, getattr(event, '_qq_expression_context', None)
        if not expression.marked:
            return
        if (not isinstance(context, ExpressionContext) or context.session_key != session_key or not self.journal
                or not _same_home(ctrl) or context.home != ctrl.home or context.transport != ctrl.state.transport):
            return
        delivery = ExpressionDelivery(expression, context, turn_context(event, session_key).logical_id, _chat(event))
        previous = getattr(event, '_qq_expression_delivery', None)
        if previous is not None and previous.turn_id == delivery.turn_id:
            if previous.expression != expression and not previous.delivered:
                self._stop(previous, 'error', 'reentrant_choice_changed')
                previous.delivered = True
            elif previous.attachment:
                extracted.media_files.append((previous.attachment, False))
            return
        event._qq_expression_delivery = delivery
        entry = next((e for e in context.entries if e.id == expression.meme_id), None)
        identifier, description = (entry.id, entry.description) if entry else ('', '')
        try:
            if not self.journal.add(delivery.turn_id, delivery.chat, context.session_id,
                                    identifier, description, ctrl.clock()):
                delivery.delivered = True  # handled before; never reconstruct/replay its image
                delivery.result = SendResult(success=False, error='Expression already handled')
                return
            self._prepare_selected(adapter, event, session_key, delivery, entry, extracted, ephemeral)
        except Exception as exc:
            self._stop(delivery, 'error', 'preflight_failed')
            logger.warning('QQ expression preflight failed: %s', type(exc).__name__)

    def _prepare_selected(self, adapter, event, session_key, delivery, entry, extracted, ephemeral):
        from agent.request_security import policy_scope
        from gateway.request_security import policy_for_adapter
        ctrl, settings, plan = self.controller, self.controller.settings, delivery.expression
        if (settings.mode != 'expression' or not _same_home(ctrl)
                or delivery.context.home != ctrl.home or delivery.context.transport != ctrl.state.transport
                or not eligible(adapter, event, plan.text or 'GIF', ephemeral, settings)):
            self._stop(delivery, 'blocked', 'ineligible')
            return
        if plan.error or entry is None:
            self._stop(delivery, 'blocked', plan.error or 'invalid_selection')
            return
        if plan.text and entry.id in ctrl.state.recent(delivery.chat, settings.recent):
            self._stop(delivery, 'blocked', 'recent')
            return
        available = self.journal.availability(delivery.chat, ctrl.clock(), settings, sole_reply=not plan.text)
        if available != 'available':
            self._stop(delivery, 'blocked', available)
            return

        policy = policy_for_adapter(adapter, event.source)
        if policy is None or (policy.platform, policy.chat_id, policy.subject_id) != (
                'qqbot', event.source.chat_id, event.source.user_id):
            self._stop(delivery, 'error', 'policy_unavailable')
            return
        catalog = _catalog(ctrl)
        delivery.snapshot = catalog.snapshot(entry, ctrl.home / 'cache/qqbot/meme_snapshots')
        with policy_scope(policy):
            if not policy.grant_artifact(delivery.snapshot):
                raise ValueError('Expression snapshot grant failed')

        # A real native attachment keeps a GIF-only reply out of the empty-text path.
        # The QQ delivery mixin removes it from generic batching and sends a pure GIF itself.
        delivery.attachment = str(delivery.snapshot)
        extracted.media_files.append((delivery.attachment, False))

    def _stop(self, delivery, outcome, reason):
        # Disk/locking failure in expression bookkeeping cannot eat the text reply.
        actions = (
            lambda: self.controller.state.finish(delivery.turn_id, 'error' if outcome == 'error' else 'none'),
            lambda: self.journal.finish(delivery.turn_id, outcome, reason),
        )
        for action in actions:
            try:
                action()
            except Exception as exc:
                logger.warning('QQ expression stop journal unavailable: %s', type(exc).__name__)
        cleanup(delivery)

    def _record_outcome(self, delivery, outcome, message_id=None):
        try:
            self.journal.end_send(delivery.turn_id, outcome, message_id)
        except Exception as exc:
            # The durable pre-I/O "attempting" remains UNCONFIRMED; never retry the network.
            logger.warning('QQ expression outcome journal unavailable: %s', type(exc).__name__)

    async def deliver(self, adapter, event, delivery):
        from gateway.platforms.base import SendResult
        from gateway.request_security import policy_for_adapter
        from agent.request_security import policy_scope
        ctrl = self.controller
        if delivery.delivered or delivery.snapshot is None:
            return getattr(delivery, 'result', None)
        delivery.delivered = True
        attempted = False
        try:
            if (not _same_home(ctrl) or delivery.context.transport != ctrl.state.transport
                    or not eligible_source(adapter, event, ctrl.settings)):
                self._stop(delivery, 'blocked', 'source_changed')
                return SendResult(success=False, error='Expression source changed')
            text_result = delivery.text_result
            if delivery.expression.text and not (text_result and text_result.success
                    and isinstance(text_result.message_id, str) and text_result.message_id.strip()):
                self._stop(delivery, 'blocked', 'text_not_confirmed')
                return SendResult(success=False, error='Text was not confirmed; GIF not sent')
            async with asyncio.timeout(15):
                # Text delivery can take arbitrarily long. Admit only NOW: no stale ready
                # reservation may outlive the busy window and leapfrog another conversation.
                sole_reply = not delivery.expression.text
                available = self.journal.availability(delivery.chat, ctrl.clock(), ctrl.settings,
                                                      sole_reply=sole_reply)
                if not sole_reply and delivery.expression.meme_id in ctrl.state.recent(delivery.chat, ctrl.settings.recent):
                    available = 'recent'
                if available != 'available':
                    self._stop(delivery, 'blocked', available)
                    return SendResult(success=False, error='Expression limit changed before send')
                if not ctrl.state.claim(delivery.turn_id, delivery.chat, ctrl.clock(),
                        probability=ctrl.settings.probability, cooldown=ctrl.settings.cooldown,
                        daily_limit=ctrl.settings.daily_limit, sole_reply=sole_reply):
                    self._stop(delivery, 'blocked', 'already_claimed')
                    return SendResult(success=False, error='Expression not admitted')
                # Probability controls attached decoration, never the sole reply.
                # Both marker spellings obey the same content-based rule.
                if not ctrl.state.choose(delivery.turn_id,
                        'natural' if delivery.expression.text else 'strong', ctrl.clock(),
                        probability=ctrl.settings.probability, draw=ctrl.draw):
                    self._stop(delivery, 'not_selected', 'optional_prd')
                    return None
                if not self.journal.start_send(delivery.turn_id):
                    return SendResult(success=False, error='Expression already attempted')
                attempted = True
                adapter.pause_typing_for_chat(event.source.chat_id)
                with policy_scope(policy_for_adapter(adapter, event.source)):
                    result = await adapter.send_image_file(chat_id=event.source.chat_id,
                        image_path=str(delivery.snapshot), reply_to=event.message_id)
                identifier = result.message_id if isinstance(result.message_id, str) and result.message_id.strip() else None
                outcome = 'sent' if result.success and identifier else 'unknown'
                self._record_outcome(delivery, outcome, identifier)
                delivery.result = result if outcome == 'sent' else SendResult(
                    success=False, error='Expression delivery unconfirmed')
                return delivery.result
        except asyncio.CancelledError:
            if attempted:
                self._record_outcome(delivery, 'unknown')
            else:
                self._stop(delivery, 'error', 'cancelled_before_send')
            raise
        except Exception as exc:
            if attempted:
                self._record_outcome(delivery, 'unknown')
            else:
                self._stop(delivery, 'error', 'send_preflight_failed')
            logger.warning('QQ expression send failed: %s', type(exc).__name__)
            return SendResult(success=False, error='Expression delivery unconfirmed' if attempted else 'Expression not sent')
        finally:
            cleanup(delivery)


def cleanup(delivery):
    if delivery is not None and delivery.snapshot is not None:
        try:
            delivery.snapshot.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning('QQ expression snapshot cleanup failed: %s', type(exc).__name__)
        delivery.snapshot = None


def engine_for(adapter):
    controller = controller_for(adapter)
    engine = getattr(controller, '_expression_engine', None)
    if engine is None:
        engine = controller._expression_engine = ExpressionEngine(controller)
    return engine
