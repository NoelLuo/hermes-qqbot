"""Optional, code-gated GIF replies. No model output grants paths or destinations."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .meme_catalog import MemeCatalog
from .meme_state import MemeState
from .delivery_binding import current_delivery
from .meme_selector import MemeSelection, select_meme

logger = logging.getLogger(__name__)


def _ids(options, name):
    values = options.get(name, [])
    if not isinstance(values, list) or any(not isinstance(x, str) or not x or x == '*' for x in values):
        raise ValueError("Explicit meme audience IDs required")
    return frozenset(values)


def _number(options, name, default, minimum, maximum):
    value = options.get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Invalid meme setting: " + name)
    return value


@dataclass(frozen=True)
class MemeSettings:
    enabled: bool = False
    root: str = ''
    catalog: str = ''
    groups: frozenset[str] = frozenset()
    dm_users: frozenset[str] = frozenset()
    intimacy_users: frozenset[str] = frozenset()
    probability: float = .2
    cooldown: float = 180
    recent: int = 3
    daily_limit: int = 30
    confidence: float = .75
    selector_timeout: float = 6
    all_groups: bool = False
    mode: str = 'auto'

    @classmethod
    def from_extra(cls, extra):
        options = extra.get('memes', {})
        if not isinstance(options, dict) or options.get('enabled') is not True:
            return cls()
        try:
            root, catalog = options.get('root_dir'), options.get('catalog_path', '')
            if not isinstance(root, str) or not root or not isinstance(catalog, str):
                raise ValueError('Meme root required')
            all_groups = options.get('all_groups', False)
            if type(all_groups) is not bool:
                raise ValueError('Explicit all_groups boolean required')
            mode = options.get('mode', 'auto')
            if not isinstance(mode, str) or mode not in {'auto', 'expression'}:
                raise ValueError('Invalid meme mode')
            recent = _number(options, 'recent_exclude', 3, 0, 20)
            # 0 is the administrator's explicit "no daily cap"; any positive value still caps.
            limit = _number(options, 'max_per_chat_per_day', 30, 0, 100)
            if int(recent) != recent or int(limit) != limit:
                raise ValueError('Meme counts must be integers')
            return cls(True, root, catalog, _ids(options, 'groups'), _ids(options, 'dm_users'),
                       _ids(options, 'intimacy_users'), _number(options, 'probability', .2, 0, 1),
                       _number(options, 'cooldown_seconds', 180, 0, 86400), int(recent), int(limit),
                       _number(options, 'min_confidence', .75, 0, 1),
                       _number(options, 'selector_timeout_seconds', 6, .1, 10), all_groups, mode)
        except ValueError:
            logger.warning('QQ memes disabled: invalid settings')
            return cls()


@dataclass
class MemeTurn:
    message_id: str
    chat_id: str
    subject_id: str
    chat_type: str
    input_text: str
    prepared: bool = False
    attachments: bool = False


def capture_turn(event):
    """Called only by QQ's real ingress; never deserialize this from event.metadata."""
    raw, source = event.raw_message, event.source
    if not isinstance(raw, dict) or not isinstance(raw.get('content'), str):
        return
    if not event.message_id or raw.get('id') != event.message_id:
        return
    event._qq_meme_turn = MemeTurn(event.message_id, source.chat_id, source.user_id,
                                   source.chat_type, raw['content'][:2000])


def mark_response(event, extracted, ephemeral):
    turn = getattr(event, '_qq_meme_turn', None)
    if isinstance(turn, MemeTurn):
        turn.prepared = not ephemeral
        turn.attachments = bool(extracted.images or extracted.media_files or extracted.local_files)


def eligible_source(adapter, event, settings):
    turn, source = getattr(event, '_qq_meme_turn', None), event.source
    if not (settings.enabled and isinstance(turn, MemeTurn)
            and not event.internal and not event.inherit_channel_pin):
        return False
    if (not source.user_id or event.is_command()
            or adapter._strip_at_mention(turn.input_text).lstrip().startswith('/')
            or getattr(event, '_heartbeat_session_id', None)
            or getattr(event, 'ledger_message_id', None) not in (None, turn.message_id)
            or (event.message_id, source.chat_id, source.user_id, source.chat_type)
            != (turn.message_id, turn.chat_id, turn.subject_id, turn.chat_type)):
        return False
    if source.chat_type == 'group':
        return ((settings.all_groups or source.chat_id in settings.groups)
                and adapter._guess_chat_type(source.chat_id) == 'group')
    return (source.chat_type in {'dm', 'c2c'} and source.user_id in settings.dm_users
            and source.chat_id == source.user_id and adapter._guess_chat_type(source.chat_id) == 'c2c')


def eligible(adapter, event, text, ephemeral, settings):
    if not eligible_source(adapter, event, settings):
        return False
    turn = event._qq_meme_turn
    return bool(turn.prepared and not turn.attachments and text.strip() and not ephemeral
                and not adapter._long_reply_preview.eligible(adapter, event.source.chat_id, text))


class MemeController:
    def __init__(self, adapter):
        self.settings = MemeSettings.from_extra(adapter.config.extra or {})
        self.home = adapter._receipt_home
        self.draw = random.random
        self.clock = time.time
        self.selector = None  # Lazy: disabled bots never initialize auxiliary clients.
        transport = hashlib.sha256((str(self.home) + '\0' + adapter._app_id).encode()).hexdigest()
        self.state = MemeState(self.home, transport) if isinstance(self.home, Path) else None

    async def after_text(self, adapter, event, session_key, text, *, ephemeral=False):
        if self.settings.mode != 'auto' or not self.state or not eligible(adapter, event, text, ephemeral, self.settings):
            return
        from hermes_constants import get_hermes_home
        # This version declines multiplexed routes with a different runtime home.
        if get_hermes_home().resolve() != self.home.resolve():
            return
        delivery = current_delivery(event.source.chat_id)
        turn_id = delivery.logical_id
        if not turn_id.startswith('turn-') or delivery.session_key != session_key:
            return
        chat = event.source.chat_type + ':' + event.source.chat_id
        snapshot, attempted, claimed = None, False, False
        try:
            async with asyncio.timeout(15):
                claimed = self.state.claim(turn_id, chat, self.clock(), probability=self.settings.probability,
                    cooldown=self.settings.cooldown, daily_limit=self.settings.daily_limit)
                if not claimed:
                    return
                catalog_path = self.settings.catalog or str(self.home / 'qqbot' / 'memes' / 'catalog.json')
                catalog = MemeCatalog.load(self.settings.root, catalog_path)
                audiences = {'group'} if event.source.chat_type == 'group' else {'dm'}
                if event.source.chat_type != 'group' and event.source.user_id in self.settings.intimacy_users:
                    audiences.add('intimate_dm')
                candidates = catalog.candidates(frozenset(audiences), self.state.recent(chat, self.settings.recent))
                if not candidates:
                    self.state.finish(turn_id, 'none')
                    return
                if self.selector is None:
                    self.selector = select_meme
                plain = adapter._strip_reasoning_block(text)
                async with asyncio.timeout(self.settings.selector_timeout):
                    chosen = await self.selector(event._qq_meme_turn.input_text, plain[:3000], candidates,
                        timeout=self.settings.selector_timeout, min_confidence=self.settings.confidence)
                entry = (next((entry for entry in candidates if entry.id == chosen.meme_id), None)
                         if isinstance(chosen, MemeSelection) else None)
                if entry is None:
                    self.state.finish(turn_id, 'none')
                    return
                from agent.request_security import policy_scope
                from gateway.request_security import policy_for_adapter
                policy = policy_for_adapter(adapter, event.source)
                if policy is None or (policy.platform, policy.chat_id, policy.subject_id) != (
                        'qqbot', event.source.chat_id, event.source.user_id):
                    raise ValueError('Meme request policy unavailable')
                snapshot = catalog.snapshot(entry, self.home / 'cache' / 'qqbot' / 'meme_snapshots')
                with policy_scope(policy):
                    if not policy.grant_artifact(snapshot):
                        raise ValueError('Meme artifact grant failed')
                    if not self.state.choose(turn_id, chosen.fit, self.clock(),
                            probability=self.settings.probability, draw=self.draw):
                        logger.info('QQ meme: id=%s fit=%s outcome=miss', entry.id, chosen.fit)
                        return
                    self.state.attempting(turn_id, entry.id)
                    attempted = True
                    result = await adapter.send_image_file(chat_id=event.source.chat_id, image_path=str(snapshot),
                                                           reply_to=event.message_id)
                real_id = result.message_id if isinstance(result.message_id, str) and result.message_id.strip() else None
                outcome = 'sent' if result.success and real_id else 'unknown'
                self.state.finish(turn_id, outcome, real_id)
                logger.info('QQ meme: id=%s fit=%s outcome=%s', entry.id, chosen.fit, outcome)
        except asyncio.CancelledError:
            if claimed:
                self._finish_safely(turn_id, 'unknown' if attempted else 'none')
            raise
        except Exception as exc:
            if claimed:
                self._finish_safely(turn_id, 'unknown' if attempted else 'error')
            logger.warning('QQ meme skipped: %s', type(exc).__name__)
        finally:
            if snapshot is not None:
                try:
                    snapshot.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning('QQ meme snapshot cleanup failed: %s', type(exc).__name__)

    def _finish_safely(self, turn_id, state):
        try:
            self.state.finish(turn_id, state)
        except Exception as exc:
            # The durable deciding/attempting row still prevents immediate replay.
            logger.warning('QQ meme journal unavailable: %s', type(exc).__name__)


def controller_for(adapter):
    controller = getattr(adapter, '_meme_controller', None)
    if controller is None:
        controller = adapter._meme_controller = MemeController(adapter)
    return controller
