"""Opt-in, transport-local long-reply previews. Clicking releases text, never an agent turn.

The bounded in-memory cache intentionally expires on reconnect/restart; it contains only
final group replies that would otherwise already have been sent to that same audience.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass

from gateway.platforms.base import SendResult
from gateway.platforms.helpers import strip_markdown

from .delivery_binding import bind_delivery, current_delivery
from .keyboards import (
    InlineKeyboard, KeyboardButton, KeyboardButtonAction, KeyboardButtonRenderData,
    KeyboardContent, KeyboardRow,
)
from .receipt_adapter import QQAPIRejection

logger = logging.getLogger(__name__)
PREFIX = "read-full:"
FINAL_METADATA_KEY = "_qq_long_reply_preview"
_TOKEN_RE = re.compile(r"read-full:([0-9a-f]{32})\Z")
_ESCAPE_RE = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|>~])")


def _number(options, key, default, minimum, maximum):
    value = options.get(key, default)
    return min(maximum, max(minimum, value)) if type(value) is int else default


@dataclass(frozen=True)
class PreviewSettings:
    enabled: bool = False
    groups: tuple[str, ...] = ()
    threshold: int = 800
    preview_chars: int = 180
    ttl_seconds: int = 3600

    @classmethod
    def from_extra(cls, extra):
        options = extra.get("long_reply_preview")
        if not isinstance(options, dict):
            return cls()
        groups = options.get("groups", [])
        return cls(
            enabled=options.get("enabled") is True,
            groups=tuple(g for g in groups if isinstance(g, str) and g) if isinstance(groups, list) else (),
            threshold=_number(options, "threshold", 800, 200, 60000),
            preview_chars=_number(options, "preview_chars", 180, 80, 500),
            ttl_seconds=_number(options, "ttl_seconds", 3600, 60, 86400),
        )


@dataclass
class _Card:
    chat_id: str
    chunks: tuple[str, ...]
    delivery: object
    expires_at: float
    next_part: int = 0
    state: str = "ready"


def _keyboard(token):
    button = KeyboardButton(
        id="read-full", group_id="read-full",
        render_data=KeyboardButtonRenderData(label="查看全文", visited_label="已请求全文"),
        # Server-side state prevents duplicates; allow another click after an explicit refusal.
        action=KeyboardButtonAction(type=1, data=PREFIX + token, click_limit=0),
    )
    return InlineKeyboard(content=KeyboardContent(rows=[KeyboardRow(buttons=[button])]))


def _preview(adapter, text, settings):
    plain = " ".join(strip_markdown(adapter._strip_reasoning_block(text)).split())
    excerpt = _ESCAPE_RE.sub(r"\\\1", plain[:settings.preview_chars].rstrip())
    suffix = "…" if len(plain) > settings.preview_chars else ""
    return f"{excerpt}{suffix}\n\n（长回复预览，共 {len(text)} 字符；点击下方按钮在本群发送全文。）"


class LongReplyPreview:
    MAX_CARDS = 128
    MAX_CACHED_CHARS = 1_000_000

    def __init__(self, extra):
        self.settings = PreviewSettings.from_extra(extra)
        self._cards: dict[str, _Card] = {}
        self._clock = time.monotonic

    def eligible(self, adapter, chat_id, content):
        settings = self.settings
        return (settings.enabled and chat_id in settings.groups and adapter._markdown_support
                and adapter._guess_chat_type(chat_id) == "group"
                and len(adapter._strip_speaker_tag_echo(content)) > settings.threshold)

    def _prune(self):
        now = self._clock()
        for token, card in list(self._cards.items()):
            if card.expires_at <= now and card.state != "sending":
                del self._cards[token]

    def _remember(self, card):
        self._prune()
        size = sum(len(chunk) for chunk in card.chunks)
        if size > self.MAX_CACHED_CHARS:
            return None
        while self._cards and (len(self._cards) >= self.MAX_CARDS or
                sum(len(c) for entry in self._cards.values() for c in entry.chunks) + size > self.MAX_CACHED_CHARS):
            disposable = next((key for key, entry in self._cards.items() if entry.state != "sending"), None)
            if disposable is None:
                return None
            del self._cards[disposable]
        token = secrets.token_hex(16)
        self._cards[token] = card
        return token

    async def maybe_send(self, adapter, chat_id, content, reply_to, metadata):
        """Return None to use the normal full-text path; never fall back after an unknown POST."""
        settings = self.settings
        if not ((metadata or {}).get(FINAL_METADATA_KEY) is True and self.eligible(adapter, chat_id, content)):
            return None
        text = adapter._strip_speaker_tag_echo(content)
        if not await adapter._ensure_connected():
            return adapter._NOT_CONNECTED
        chunks = tuple(adapter.truncate_message(adapter.format_message(text), adapter.MAX_MESSAGE_LENGTH))
        card = _Card(chat_id, chunks, current_delivery(chat_id), self._clock() + settings.ttl_seconds)
        token = self._remember(card)
        if token is None:
            return None
        try:
            result = await adapter._send_group_text(chat_id, _preview(adapter, text, settings),
                                                    reply_to, keyboard=_keyboard(token))
        except QQAPIRejection:
            # Known rejection: the preview never arrived, so ordinary text is safe.
            self._cards.pop(token, None)
            logger.warning("[%s] Preview keyboard refused; sending normal text", adapter._log_tag)
            return None
        except Exception as exc:
            # A lost ACK can mean QQ displayed the preview. Retain its token, do not resend.
            logger.warning("[%s] Preview POST outcome unknown: %s", adapter._log_tag, type(exc).__name__)
            return SendResult(success=False, error="QQ preview delivery unknown", retryable=False,
                              raw_response={"delivery_status": "unknown", "qq_logical_id": card.delivery.logical_id})
        if not result.success:
            return result
        if chat_id in adapter._group_context_cache:
            adapter._record_group_turn(chat_id, adapter._assistant_label, _preview(adapter, text, settings))
        return result

    def owns(self, event):
        return event.button_data.startswith(PREFIX)

    def _authorized_card(self, adapter, event):
        self._prune()
        match = _TOKEN_RE.fullmatch(event.button_data)
        card = self._cards.get(match[1]) if match else None
        if not (card and self.settings.enabled and event.type == 11 and event.scene == "group"
                and event.chat_type == 1 and event.group_openid == card.chat_id
                and card.chat_id in self.settings.groups and event.group_member_openid):
            return None
        if not adapter._is_group_allowed(card.chat_id, event.group_member_openid):
            return None
        if adapter._is_sender_authorized(event.group_member_openid, "group", card.chat_id) is not True:
            return None
        return card

    async def on_interaction(self, adapter, event, *, event_id=None):
        """ACK promptly, atomically claim the card, then release only its unsent chunks."""
        card = self._authorized_card(adapter, event)
        claim = bool(card and card.state == "ready")
        if claim:
            card.state = "sending"  # no await before claiming: concurrent clicks cannot double-send
        try:
            await adapter._acknowledge_interaction(event.id, code=0 if card and card.state != "unknown" else 1)
        except Exception as exc:
            if claim:
                card.state = "ready"
            logger.warning("[%s] Full-text interaction ACK failed: %s", adapter._log_tag, type(exc).__name__)
            return
        if not claim:
            return
        try:
            if not await adapter._ensure_connected():
                card.state = "ready"
                return
            with bind_delivery(card.chat_id, card.delivery):
                await self._release(adapter, card, event_id or event.id)
        except BaseException:
            card.state = "unknown"  # interruption can happen after QQ accepted a chunk
            raise

    async def _release(self, adapter, card, event_id):
        while card.next_part < len(card.chunks):
            body = adapter._build_text_body(card.chunks[card.next_part])
            body.update(event_id=event_id, msg_seq=card.next_part + 1)
            try:
                result = await adapter._post_message(adapter._messages_path("group", card.chat_id), body)
            except QQAPIRejection:
                card.state = "ready"  # explicit refusal; retry only this part on a fresh click
                logger.warning("[%s] Full-text part refused (part=%d)", adapter._log_tag, card.next_part)
                return
            except Exception as exc:
                card.state = "unknown"
                logger.warning("[%s] Full-text delivery unknown: %s", adapter._log_tag, type(exc).__name__)
                return
            if not result.success or not result.message_id:
                card.state = "unknown"
                return
            card.next_part += 1
        card.state = "done"
        card.chunks = ()  # retain only a small duplicate-click tombstone
        logger.info("[%s] Full-text reply delivered (%d parts)", adapter._log_tag, card.next_part)
