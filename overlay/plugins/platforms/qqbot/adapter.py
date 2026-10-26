"""QQ Bot platform adapter (Official QQ Bot API v2): WebSocket gateway for inbound
events, REST (``api.sgroup.qq.com``) for outbound messages and media uploads.

config.yaml ``platforms.qq.extra``: app_id / client_secret (or QQ_APP_ID /
QQ_CLIENT_SECRET), markdown_support, dm_policy + allow_from, group_policy +
group_allow_from (open | allowlist | disabled | pairing), and optional ``stt``
{provider, baseUrl, apiKey, model} (or QQ_STT_* env vars). Voice transcription
tries QQ's free ``asr_refer_text`` first, then the configured STT provider.
"""

from __future__ import annotations

from pm import install_hint
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    gateway_trust_env, BasePlatformAdapter, ExecApprovalPrompt, SendResult,
    _ssrf_redirect_guard, cache_document_from_bytes_async, cache_image_from_bytes_async,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import strip_markdown
from gateway.platforms.helpers import MessageDeduplicator, cancel_task
from gateway.platforms.access_policy_mixin import OwnAccessPolicyMixin
from gateway.platforms.media_cache import ext_for_mime

logger = logging.getLogger(__name__)


class QQCloseError(Exception):
    """Raised when the QQ WebSocket closes; carries code + reason for the reconnect loop."""

    def __init__(self, code, reason=""):
        self.code = int(code) if code else None
        self.reason = str(reason) if reason else ""
        super().__init__(f"WebSocket closed (code={self.code}, reason={self.reason})")


from .constants import (
    API_BASE, TOKEN_URL, GATEWAY_URL_PATH, DEFAULT_API_TIMEOUT, FILE_UPLOAD_TIMEOUT,
    CONNECT_TIMEOUT_SECONDS, RECONNECT_BACKOFF, MAX_RECONNECT_ATTEMPTS, RATE_LIMIT_DELAY,
    QUICK_DISCONNECT_THRESHOLD, MAX_QUICK_DISCONNECT_COUNT, MAX_MESSAGE_LENGTH,
    DEDUP_WINDOW_SECONDS, DEDUP_MAX_SIZE, MSG_TYPE_TEXT, MSG_TYPE_MARKDOWN, MSG_TYPE_MEDIA,
    MSG_TYPE_INPUT_NOTIFY, MEDIA_TYPE_IMAGE, MEDIA_TYPE_VIDEO, MEDIA_TYPE_VOICE, MEDIA_TYPE_FILE)
from .utils import coerce_list as _coerce_list, build_user_agent
from .markdown import normalize_qq_markdown
from .reply_gate import GroupReplyGate
from .group_images import QuietGroupImages
from .group_events import GroupEventDispatcher
from .chunked_upload import (
    ChunkedUploader, UploadDailyLimitExceededError, UploadFileTooLargeError)
from .keyboards import (
    ApprovalRequest, InlineKeyboard, InteractionEvent, build_approval_keyboard,
    build_update_prompt_keyboard, parse_approval_button_data, parse_interaction_event,
    parse_update_prompt_button_data)
from gateway.platforms._shared import get_scoped_secret as _resolve_qq_secret


def check_qq_requirements() -> bool:
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


_VOICE_EXTENSIONS = (".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".speex", ".flac")
_STT_PROVIDER_BASE_URLS = {
    "zai": "https://open.bigmodel.cn/api/coding/paas/v4",
    # Aliases that target direct REST APIs not modeled as first-class providers in PROVIDER_REGISTRY. Used
    # for ``auxiliary.<task>.provider`` so users can write the obvious name and have it resolve to a working
    # ``custom`` endpoint without needing to know our internal provider IDs. Why these specifically:
    # PROVIDER_REGISTRY has ``openai-codex`` (OAuth) and ``custom`` (manual base_url + OPENAI_API_KEY) but
    # no plain ``openai`` for direct API-key access. Users predictably type ``provider: openai`` and expect
    # it to use OPENAI_API_KEY against api.openai.com. Previously this silently fell back to the user's main
    # provider, sending OpenAI model names to e.g. DeepSeek and producing cryptic ``unknown variant
    # 'image_url'`` errors (issue #31179).
    "openai": "https://api.openai.com/v1",
    "glm": "https://open.bigmodel.cn/api/coding/paas/v4"}
_AUDIO_URL_EXTENSIONS = {".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}


class QQAdapter(OwnAccessPolicyMixin, BasePlatformAdapter):
    """QQ Bot adapter backed by the official QQ Bot WebSocket Gateway + REST API."""

    # QQ Bot API does not support editing sent messages.
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    ALLOW_ALL_ENV_PREFIX = "QQ"
    _TYPING_INPUT_SECONDS = 60  # input_notify duration reported to QQ
    _TYPING_DEBOUNCE_SECONDS = 50  # refresh before it expires

    # WS close codes that are unrecoverable → stop reconnecting.
    _FATAL_CLOSE_CODES = {
        4001: "invalid opcode", 4002: "invalid payload", 4010: "invalid shard",
        4011: "sharding required", 4012: "invalid API version", 4013: "invalid intent",
        4014: "intent not authorized", 4914: "offline/sandbox-only", 4915: "banned"}
    # WS close codes that invalidate the session → clear it and re-identify on
    # the next Hello. 4009 (connection timeout) is deliberately absent: it is
    # resumable per the QQ protocol and must keep session state.
    _SESSION_INVALID_CLOSE_CODES = {4006, 4007} | set(range(4900, 4914))

    @property
    def _log_tag(self) -> str:
        """Log prefix including app_id for multi-instance disambiguation."""
        app_id = getattr(self, "_app_id", None)
        return f"QQBot:{app_id}" if app_id else "QQBot"

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_responses.clear()

    def _mark_transport_disconnected(self) -> None:
        """Mark QQ WS down without stopping the reconnect loop (base's _running
        doubles as lifecycle flag; the listener must survive transient drops)."""
        if self.has_fatal_error:
            return
        self._write_runtime_status_safe(
            "disconnected", platform_state="disconnected", error_code=None, error_message=None)

    @property
    def is_connected(self) -> bool:
        """Return True only when the QQ WebSocket transport is usable."""
        return bool(self._running and self._ws and not self._ws.closed)

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.QQBOT)

        extra = config.extra or {}
        self._app_id = str(extra.get("app_id") or _resolve_qq_secret("QQ_APP_ID", "")).strip()
        self._client_secret = str(extra.get("client_secret") or _resolve_qq_secret("QQ_CLIENT_SECRET", "")).strip()
        self._markdown_support = bool(extra.get("markdown_support", True))
        self._dm_policy = str(extra.get("dm_policy", "pairing")).strip().lower()
        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
        self._group_policy = str(extra.get("group_policy", "pairing")).strip().lower()
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))

        self._group_reply_gate = GroupReplyGate(extra)
        self._group_images = QuietGroupImages()
        self._group_events = GroupEventDispatcher()
        from hermes_constants import get_hermes_home
        try:
            self._receipt_home = get_hermes_home()
        except RuntimeError:
            self._receipt_home = None
        options = extra.get("group_reply_gate", {})
        self._assistant_label = (self._group_reply_gate.settings.names[0]
                                 if isinstance(options, dict) and options.get("assistant_names") else "Hermes")
        self._bot_id: Optional[str] = None
        self._home_channel = _resolve_qq_secret("QQBOT_HOME_CHANNEL", "")
        self._group_context_cache: dict[str, list[tuple[str, str]]] = {}
        self._group_context_max = int(_resolve_qq_secret("QQBOT_GROUP_CONTEXT_MAX", "60") or 60)

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0  # seconds, updated by Hello
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._chat_type_map: dict[str, str] = {}  # chat_id → "c2c"|"group"|"guild"|"dm"
        self._pending_responses: dict[str, asyncio.Future] = {}  # request/response correlation
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_WINDOW_SECONDS)
        self._last_msg_id: dict[str, str] = {}  # last inbound message ID per chat (send_typing)
        self._typing_sent_at: dict[str, float] = {}  # typing debounce: chat_id → last send_typing ts
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        # Inline-keyboard interaction routing: invoked for every INTERACTION_CREATE
        # after the adapter ACKed it. Defaults to the approval/update-prompt
        # dispatcher; override via set_interaction_callback() (None drops clicks).
        self._interaction_callback: Optional[Callable[[InteractionEvent], Awaitable[None]]] = (
            self._default_interaction_dispatch)

    # ── Properties ──

    @property
    def name(self) -> str:
        return "QQBot"

    # ── Connection lifecycle ──

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Authenticate, obtain gateway URL, and open the WebSocket. ``is_reconnect``
        is accepted for interface conformance only (QQBot has no server-side update queue)."""
        for ok, code, what, hint in (
            (AIOHTTP_AVAILABLE, "qq_missing_dependency", "aiohttp not installed",
             f". Run: {install_hint('messaging')}"),
            (HTTPX_AVAILABLE, "qq_missing_dependency", "httpx not installed", ". Run: hermes pm repair"),
            (self._app_id and self._client_secret, "qq_missing_credentials",
             "QQ_APP_ID and QQ_CLIENT_SECRET are required", "")):
            if not ok:
                message = f"QQ startup failed: {what}"
                self._set_fatal_error(code, message, retryable=True)
                logger.warning("[%s] %s%s", self._log_tag, message, hint)
                return False

        if not self._acquire_platform_lock("qqbot-appid", self._app_id, "QQBot app ID"):
            return False

        try:
            # Tighter keepalive pool so idle CLOSE_WAIT sockets drain faster behind proxies.
            # See #18451.
            from gateway.platforms._http_client_limits import platform_httpx_limits
            from tools.url_safety import create_ssrf_safe_async_client
            self._http_client = create_ssrf_safe_async_client(
                timeout=30.0, follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]}, limits=platform_httpx_limits())

            await self._open_gateway_ws(log_url=True)
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            self._mark_connected()
            logger.info("[%s] Connected", self._log_tag)
            self._wire_plugin_handlers(None)
            return True
        except Exception as exc:
            message = f"QQ startup failed: {exc}"
            self._set_fatal_error("qq_connect_error", message, retryable=True)
            logger.error("[%s] %s", self._log_tag, message, exc_info=True)
            await self._cleanup()
            self._release_platform_lock()
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        await cancel_task(self._listen_task)
        await cancel_task(self._heartbeat_task)
        self._listen_task = self._heartbeat_task = None
        await self._cleanup()
        self._release_platform_lock()
        logger.info("[%s] Disconnected", self._log_tag)

    async def _close_ws(self) -> None:
        """Close the WebSocket + its aiohttp session (keeps _http_client alive)."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _cleanup(self) -> None:
        """Close WebSocket, HTTP session, and client; fail pending futures."""
        await self._close_ws()
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        self._fail_pending("Disconnected")

    # ── Token management ──

    async def _fetch_json(self, what: str, request: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
        """Run an httpx request and return its JSON; any failure → RuntimeError."""
        try:
            resp = await request()
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise RuntimeError(f"Failed to get QQ Bot {what}: {exc}") from exc

    def _token_fresh(self) -> bool:
        return bool(self._access_token) and time.time() < self._token_expires_at - 60

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if needed (with singleflight)."""
        if self._token_fresh():
            return self._access_token
        async with self._token_lock:
            if self._token_fresh():  # double-check after acquiring lock
                return self._access_token
            data = await self._fetch_json("access token", lambda: self._http_client.post(
                TOKEN_URL, json={"appId": self._app_id, "clientSecret": self._client_secret},
                timeout=DEFAULT_API_TIMEOUT))
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"QQ Bot token response missing access_token: {data}")
            expires_in = int(data.get("expires_in", 7200))
            self._access_token = token
            self._token_expires_at = time.time() + expires_in
            logger.info("[%s] Access token refreshed, expires in %ds", self._log_tag, expires_in)
            return self._access_token

    async def _get_gateway_url(self) -> str:
        token = await self._ensure_token()
        data = await self._fetch_json("gateway URL", lambda: self._http_client.get(
            f"{API_BASE}{GATEWAY_URL_PATH}",
            headers={"Authorization": f"QQBot {token}", "User-Agent": build_user_agent()},
            timeout=DEFAULT_API_TIMEOUT))
        url = data.get("url")
        if not url:
            raise RuntimeError(f"QQ Bot gateway response missing url: {data}")
        return url

    # ── WebSocket lifecycle ──

    async def _open_gateway_ws(self, *, log_url: bool = False) -> None:
        """Token → gateway URL → WebSocket (shared by connect and _reconnect)."""
        await self._ensure_token()
        gateway_url = await self._get_gateway_url()
        if log_url:
            logger.info("[%s] Gateway URL: %s", self._log_tag, gateway_url)
        await self._open_ws(gateway_url)

    async def _open_ws(self, gateway_url: str) -> None:
        await self._close_ws()
        # Local patch: try the proxy first, then fall back to a direct connection,
        # so the bot connects whether or not the user's proxy is running.
        proxy_vars = ("WSS_PROXY", "wss_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        ws_proxy = next((v for v in map(os.getenv, proxy_vars) if v), None) if gateway_trust_env() else None

        attempts = [("proxy", ws_proxy)] if ws_proxy else []
        attempts.append(("direct", None))

        last_error: Optional[Exception] = None
        for label, proxy in attempts:
            try:
                self._session = aiohttp.ClientSession(trust_env=False)
                self._ws = await self._session.ws_connect(
                    gateway_url, headers={"User-Agent": build_user_agent()},
                    timeout=CONNECT_TIMEOUT_SECONDS, proxy=proxy,
                )
                logger.info("[%s] WebSocket connected via %s to %s", self._log_tag, label, gateway_url)
                return
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = exc
                logger.debug("[%s] WebSocket %s attempt failed: %s", self._log_tag, label, exc)
                if self._session and not self._session.closed:
                    with contextlib.suppress(Exception):
                        await self._session.close()
                self._session = None
                self._ws = None
        raise RuntimeError(
            f"Failed to connect WebSocket to {gateway_url} (tried {len(attempts)} method(s))"
        ) from last_error

    async def _listen_loop(self) -> None:
        """Read WebSocket events and reconnect on errors. Close codes: 4004 → refresh
        token; 4006/4007/49xx → clear session and re-identify; 4008 → rate limited,
        back off; _FATAL_CLOSE_CODES → stop."""
        backoff_idx = 0
        connect_time = 0.0
        quick_disconnect_count = 0

        async def reconnect() -> None:
            nonlocal backoff_idx, quick_disconnect_count
            if await self._reconnect(backoff_idx):
                backoff_idx = quick_disconnect_count = 0
            else:
                backoff_idx += 1

        while self._running:
            try:
                connect_time = time.monotonic()
                await self._read_events()
                backoff_idx = quick_disconnect_count = 0
            except asyncio.CancelledError:
                return
            except QQCloseError as exc:
                if not self._running:
                    return
                code = exc.code
                logger.warning("[%s] WebSocket closed: code=%s reason=%s", self._log_tag, code, exc.reason)

                # Quick disconnect detection (permission issues, misconfiguration)
                duration = time.monotonic() - connect_time
                if duration < QUICK_DISCONNECT_THRESHOLD and connect_time > 0:
                    quick_disconnect_count += 1
                    logger.info(
                        "[%s] Quick disconnect (%.1fs), count: %d", self._log_tag, duration, quick_disconnect_count
                    )
                    if quick_disconnect_count >= MAX_QUICK_DISCONNECT_COUNT:
                        logger.error(
                            "[%s] Too many quick disconnects. "
                            "Check: 1) AppID/Secret correct 2) Bot permissions on QQ Open Platform",
                            self._log_tag)
                        self._set_fatal_error(
                            "qq_quick_disconnect", "Too many quick disconnects — check bot permissions", retryable=True
                        )
                        return
                else:
                    quick_disconnect_count = 0

                self._mark_transport_disconnected()
                self._fail_pending("Connection closed")

                desc = self._FATAL_CLOSE_CODES.get(code)
                if desc:
                    logger.error("[%s] Bot is %s. Check QQ Open Platform.", self._log_tag, desc)
                    self._set_fatal_error(f"qq_{desc}", f"Bot is {desc}", retryable=False)
                    return

                if code == 4008:
                    logger.info("[%s] Rate limited (4008), waiting %ds", self._log_tag, RATE_LIMIT_DELAY)
                    if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                        self._mark_disconnected()
                        return
                    await asyncio.sleep(RATE_LIMIT_DELAY)
                    await reconnect()
                    continue

                if code == 4004:
                    logger.info("[%s] Invalid token (4004), will refresh and reconnect", self._log_tag)
                    self._access_token = None
                    self._token_expires_at = 0.0

                if code in self._SESSION_INVALID_CLOSE_CODES:
                    logger.info("[%s] Session error (%d), clearing session for re-identify", self._log_tag, code)
                    self._session_id = None
                    self._last_seq = None

                await reconnect()
                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached (QQCloseError)", self._log_tag)
                    self._mark_disconnected()
                    return

            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self._log_tag, exc)
                self._mark_transport_disconnected()
                self._fail_pending("Connection interrupted")

                if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                    logger.error("[%s] Max reconnect attempts reached", self._log_tag)
                    self._mark_disconnected()
                    return
                await reconnect()

    async def _reconnect(self, backoff_idx: int) -> bool:
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info("[%s] Reconnecting in %ds (attempt %d)...", self._log_tag, delay, backoff_idx + 1)
        await asyncio.sleep(delay)

        self._heartbeat_interval = 30.0  # reset until Hello
        try:
            await self._open_gateway_ws()
            self._mark_connected()
            logger.info("[%s] Reconnected", self._log_tag)
            return True
        except Exception as exc:
            logger.warning("[%s] Reconnect failed: %s", self._log_tag, exc)
            return False

    async def _read_events(self) -> None:
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        if self._ws.closed:
            # Returning normally here would make _listen_loop treat it as a clean
            # read and retry with backoff reset → 100% CPU spin. Raise instead.
            raise RuntimeError("WebSocket closed")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    self._dispatch_payload(payload)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QQCloseError(msg.data, msg.extra)
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send op 1 heartbeats with the latest seq at 80% of the Hello interval."""
        with contextlib.suppress(asyncio.CancelledError):
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._ws.send_json({"op": 1, "d": self._last_seq})
                except Exception as exc:
                    logger.debug("[%s] Heartbeat failed: %s", self._log_tag, exc)

    async def _send_ws_auth(self, name: str, payload: dict[str, Any], sent_msg: str, *log_args) -> bool:
        """Send an Identify/Resume payload; returns False if the send raised."""
        try:
            if self._ws and not self._ws.closed:
                await self._ws.send_json(payload)
                logger.info("[%s] " + sent_msg, self._log_tag, *log_args)
            else:
                logger.warning("[%s] Cannot send %s: WebSocket not connected", self._log_tag, name)
        except Exception as exc:
            logger.error("[%s] Failed to send %s: %s", self._log_tag, name, exc)
            return False
        return True

    async def _send_identify(self) -> None:
        """Send op 2 Identify (reply to Hello); server answers with READY. Intents:
        C2C_GROUP_AT_MESSAGES | DIRECT_MESSAGE | INTERACTION."""
        token = await self._ensure_token()
        payload = {"op": 2, "d": {
            "token": f"QQBot {token}",
            "intents": (1 << 25) | (1 << 12) | (1 << 26),
            "shard": [0, 1],
            "properties": {"$os": "macOS", "$browser": "hermes-agent", "$device": "hermes-agent"}}}
        await self._send_ws_auth("Identify", payload, "Identify sent")

    async def _send_resume(self) -> None:
        """Send op 6 Resume after a reconnect; on failure clear session → Identify next Hello."""
        token = await self._ensure_token()
        payload = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._last_seq}}
        if not await self._send_ws_auth(
            "Resume", payload, "Resume sent (session_id=%s, seq=%s)", self._session_id, self._last_seq
        ):
            self._session_id = None
            self._last_seq = None

    @staticmethod
    def _create_task(coro):
        """Schedule a coroutine; returns None (no error) when no loop is running
        (tests call _dispatch_payload synchronously)."""
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return None

    def _close_ws_soon(self) -> None:
        """Close the WS so _read_events raises and _listen_loop reconnects (with Resume)."""
        if self._ws and not self._ws.closed:
            self._create_task(self._ws.close())

    def _dispatch_payload(self, payload: dict[str, Any]) -> None:
        """Route inbound WebSocket payloads (dispatch synchronously, spawn async handlers)."""
        op, t, s, d = payload.get("op"), payload.get("t"), payload.get("s"), payload.get("d")
        if isinstance(s, int) and (self._last_seq is None or s > self._last_seq):
            self._last_seq = s

        if op == 10:  # Hello — reply with Resume (have session) or Identify
            interval_ms = (d if isinstance(d, dict) else {}).get("heartbeat_interval", 30000)
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8  # 80% of server interval
            logger.debug(
                "[%s] Hello received, heartbeat_interval=%dms (sending every %.1fs)",
                self._log_tag, interval_ms, self._heartbeat_interval)
            resume = self._session_id and self._last_seq is not None
            self._create_task(self._send_resume() if resume else self._send_identify())
        elif op == 0 and t:  # Dispatch
            if t == "READY":
                if isinstance(d, dict):  # store session_id for resume
                    self._session_id = d.get("session_id")
                    user = d.get("user") if isinstance(d.get("user"), dict) else {}
                    self._bot_id = user.get("id")
                    logger.info("[%s] Ready, session_id=%s", self._log_tag, self._session_id)
            elif t == "RESUMED":
                logger.info("[%s] Session resumed", self._log_tag)
            elif t in self._INBOUND_HANDLERS:
                asyncio.create_task(self._on_message(t, d))
            elif t == "INTERACTION_CREATE":
                self._create_task(self._on_interaction(d))
            else:
                logger.debug("[%s] Unhandled dispatch: %s", self._log_tag, t)
        elif op == 11:  # Heartbeat ACK
            pass
        elif op == 7:  # Server Reconnect
            logger.info("[%s] Server requested reconnect (op 7)", self._log_tag)
            self._close_ws_soon()
        elif op == 9:  # Invalid Session — d=True resumable, d=False re-identify from scratch
            if d is not None and bool(d):
                logger.info("[%s] Invalid session (op 9, resumable)", self._log_tag)
            else:
                logger.info("[%s] Invalid session (op 9, not resumable), clearing session", self._log_tag)
                self._session_id = None
                self._last_seq = None
            self._close_ws_soon()
        else:
            logger.debug("[%s] Unknown op: %s", self._log_tag, op)

    # ── JSON helpers ──

    @staticmethod
    def _parse_json(raw: Any) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.warning("[QQBot] Failed to parse JSON: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _next_msg_seq(msg_id: str) -> int:
        """Generate a message sequence number in 0..65535 range."""
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    # ── Inbound message handling ──

    async def handle_message(self, event: MessageEvent) -> None:
        """Cache the last message ID per chat, then delegate to base."""
        if event.message_id and event.source.chat_id:
            self._last_msg_id[event.source.chat_id] = event.message_id
        await super().handle_message(event)

    async def _on_message(self, event_type: str, d: Any) -> None:
        if not isinstance(d, dict):
            return
        if event_type in {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}:
            await self._group_events.dispatch(self, event_type, d)
            return
        msg_id = str(d.get("id", ""))
        if not msg_id or self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate or missing message id: %s", self._log_tag, msg_id)
            return
        handler = self._INBOUND_HANDLERS.get(event_type)
        if handler:
            author = d.get("author") if isinstance(d.get("author"), dict) else {}
            await getattr(self, handler)(
                d, msg_id, str(d.get("content", "")).strip(), author, str(d.get("timestamp", "")))

    # ── Inline-keyboard interactions (INTERACTION_CREATE) ──

    def set_interaction_callback(self, callback: Optional[Callable[[InteractionEvent], Awaitable[None]]]) -> None:
        """Register (or clear) the callback invoked per ACKed INTERACTION_CREATE."""
        self._interaction_callback = callback

    async def _on_interaction(self, d: Any) -> None:
        """Parse INTERACTION_CREATE, ACK it promptly (else the client shows an error
        icon on the button), then dispatch to the registered callback."""
        if not isinstance(d, dict):
            return
        try:
            event = parse_interaction_event(d)
        except Exception as exc:
            logger.warning("[%s] Failed to parse INTERACTION_CREATE: %s", self._log_tag, exc)
            return
        if not event.id:
            logger.warning("[%s] INTERACTION_CREATE missing id, skipping ACK", self._log_tag)
            return

        try:
            await self._acknowledge_interaction(event.id)
        except Exception as exc:
            logger.warning("[%s] Failed to ACK interaction %s: %s", self._log_tag, event.id, exc)

        logger.info(
            "[%s] Interaction: scene=%s button_data=%r operator=%s",
            self._log_tag, event.scene, event.button_data, event.operator_openid)
        callback = self._interaction_callback
        if callback is None:
            logger.debug(
                "[%s] No interaction callback registered; dropping button click %r", self._log_tag, event.button_data
            )
            return
        try:
            await callback(event)
        except Exception as exc:
            logger.error("[%s] Interaction callback raised: %s", self._log_tag, exc, exc_info=True)

    async def _acknowledge_interaction(self, interaction_id: str, code: int = 0) -> None:
        """ACK a button interaction via ``PUT /interactions/{id}`` (code 0 = success)."""
        resp = await self._require_http_client().put(
            f"{API_BASE}/interactions/{interaction_id}",
            headers=await self._auth_headers(), json={"code": code}, timeout=DEFAULT_API_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"Interaction ACK failed [{resp.status_code}]: {resp.text[:200]}")

    # Button decision → ``choice`` for tools.approval.resolve_gateway_approval. The
    # 3-button layout folds "session" into "always"; ``/approve session`` still works.
    _APPROVAL_BUTTON_TO_CHOICE = {"allow-once": "once", "allow-always": "always", "deny": "deny"}

    @staticmethod
    def _parse_gateway_session_key(session_key: str) -> Optional[dict[str, str]]:
        """Parse ``agent:<namespace>:<platform>:<chat_type>:<chat_id>[:<user_id>]``.

        The namespace slot carries the multiplex profile ("main" for the
        default profile — see ``gateway.session._session_key_namespace``);
        it takes no part in any authorization decision, so any non-empty
        value is accepted and every later slot keeps its position.
        """
        parts = str(session_key or "").split(":")
        if len(parts) < 5 or parts[0] != "agent" or not parts[1]:
            return None
        parsed = {"platform": parts[2], "chat_type": parts[3], "chat_id": parts[4]}
        if len(parts) > 5:
            parsed["user_id"] = parts[5]
        return parsed

    def _is_authorized_interaction_for_session(self, event: InteractionEvent, session_key: str) -> bool:
        """Authorize approval/update interactions against session + operator."""
        parsed = self._parse_gateway_session_key(session_key)
        operator = str(event.operator_openid or "").strip()
        if not parsed or parsed.get("platform") != "qqbot" or not operator:
            return False

        chat_type = parsed.get("chat_type", "")
        chat_id = parsed.get("chat_id", "")
        if chat_type not in {"dm", "c2c", "group", "guild"}:
            return False
        # Approval keys come from build_source (chat_type="dm"); update-prompt keys from event.scene ("c2c").
        if chat_type in {"c2c", "dm"}:
            if not chat_id or operator != chat_id or event.scene not in {"c2c", "dm"}:
                return False
        if chat_type in {"group", "guild"}:
            event_chat = str(event.group_openid or event.guild_id or "").strip()
            if not event_chat or event_chat != chat_id:
                return False
        from gateway.request_security import policy_for_adapter
        source = self.build_source(chat_id=chat_id, chat_type=chat_type, user_id=operator)
        policy = policy_for_adapter(self, source)
        if policy is not None:
            return policy.is_admin
        if chat_type in {"c2c", "dm"}:
            return True
        if chat_type in {"group", "guild"}:
            session_user = str(parsed.get("user_id", "")).strip()
            return bool(session_user) and operator == session_user
        return False

    def _update_prompt_session_key(self, event: InteractionEvent, chat: str) -> str:
        """Session key an update-prompt click is authorized against, built by the ONE key builder so
        it carries the profile namespace (a hard-coded ``agent:main:`` prefix never matched a
        multiplexed secondary bot's lane). No participant: ``c2c`` authorizes on chat == operator."""
        return self._source_session_key(self.build_source(chat_id=chat, chat_type=event.scene))

    async def _default_interaction_dispatch(self, event: InteractionEvent) -> None:
        """Default interaction callback: ``approve:<session_key>:<decision>`` →
        tools.approval.resolve_gateway_approval; ``update_prompt:<answer>`` →
        ``~/.hermes/.update_response``; anything else is ignored at DEBUG."""
        button_data = event.button_data
        if not button_data:
            return
        request_id = ""
        if button_data.startswith("approve-request:"):
            if button_data.count(":") < 3:
                return
            _, request_id, tail = button_data.split(":", 2)
            button_data = "approve:" + tail
        approval = parse_approval_button_data(button_data)
        if approval is not None:
            session_key, decision = approval
            choice = self._APPROVAL_BUTTON_TO_CHOICE.get(decision)
            if choice is None:
                logger.warning("[%s] Unknown approval decision %r (session=%s)", self._log_tag, decision, session_key)
                return
            if not self._is_authorized_interaction_for_session(event, session_key):
                logger.warning(
                    "[%s] Rejected unauthorized approval click for session %s (operator=%s)",
                    self._log_tag, session_key, event.operator_openid)
                return
            from gateway.request_security import policy_for_adapter
            parsed = self._parse_gateway_session_key(session_key)
            source = self.build_source(chat_id=parsed["chat_id"], chat_type=parsed["chat_type"], user_id=event.operator_openid)
            if policy_for_adapter(self, source) is not None and not request_id:
                return  # Legacy cards must not resolve whichever request happens to be newest.
            try:
                from tools.approval import resolve_gateway_approval  # lazy: keep adapter light
                count = (resolve_gateway_approval(session_key, choice, request_id=request_id)
                         if request_id else resolve_gateway_approval(session_key, choice))
                logger.info(
                    "[%s] Button resolved %d approval(s) for session %s (choice=%s, operator=%s)",
                    self._log_tag, count, session_key, choice, event.operator_openid)
            except Exception as exc:
                logger.error("[%s] resolve_gateway_approval failed for session %s: %s", self._log_tag, session_key, exc)
            return

        update_token = ""
        if button_data.startswith("update-request:"):
            parts = button_data.split(":")
            if len(parts) != 3:
                return
            _, update_token, answer = parts
            button_data = "update_prompt:" + answer
        update_answer = parse_update_prompt_button_data(button_data)
        if update_answer is not None:
            chat = event.group_openid or event.guild_id or event.user_openid
            if not self._is_authorized_interaction_for_session(event, self._update_prompt_session_key(event, chat)):
                logger.warning(
                    "[%s] Rejected unauthorized update prompt click (operator=%s)", self._log_tag, event.operator_openid
                )
                return
            from gateway.request_security import policy_for_adapter
            source = self.build_source(chat_id=chat, chat_type=event.scene, user_id=event.operator_openid)
            if policy_for_adapter(self, source) is not None:
                cards = getattr(self, "_role_update_cards", {})
                if not update_token or cards.get(chat) != update_token:
                    return
                cards.pop(chat, None)
            self._write_update_response(update_answer, event.operator_openid)
            return

        logger.debug("[%s] Unrecognised button_data %r from interaction %s", self._log_tag, button_data, event.id)

    @staticmethod
    def _write_update_response(answer: str, operator: str = "") -> None:
        """Atomically (tmp + rename) write the update-prompt answer to
        ``.update_response``, polled by the detached ``hermes update --gateway`` watcher."""
        try:
            from hermes_constants import get_hermes_home
            response_path = get_hermes_home() / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer, encoding="utf-8")
            tmp.replace(response_path)
            logger.info("QQ update prompt answered %r by %s", answer, operator or "(unknown)")
        except Exception as exc:
            logger.error("Failed to write update response: %s", exc)

    async def _handle_c2c_message(self, d, msg_id, content, author, timestamp) -> None:
        user_openid = str(author.get("user_openid", ""))
        if not user_openid:
            logger.info("[%s] C2C message without user_openid — dropped", self._log_tag)
            return
        if not self._is_dm_intake_allowed(user_openid):
            logger.info(
                "[%s] C2C from %s dropped by dm_policy=%s (opt_in=%s)",
                self._log_tag, user_openid, self._dm_policy, self._open_dm_opted_in())
            return

        # Local patch: strip the '/' QQ's command panel prepends to non-ASCII entries.
        content = self._normalize_panel_command(content)
        attachments_raw = d.get("attachments")
        logger.info(
            "[%s] C2C message: id=%s content=%r attachments=%s",
            self._log_tag, msg_id, content[:50] if content else "",
            f"{len(attachments_raw) if isinstance(attachments_raw, list) else 0} items" if attachments_raw else "None",
        )
        if attachments_raw and isinstance(attachments_raw, list):
            for _i, _att in enumerate(attachments_raw):
                if isinstance(_att, dict):
                    logger.info(
                        "[%s] attachment[%d]: content_type=%s url=%s filename=%s",
                        self._log_tag, _i, _att.get("content_type", ""),
                        str(_att.get("url", ""))[:80], _att.get("filename", ""))

        await self._ingest(
            d, msg_id, content, attachments_raw, timestamp, verbose=True,
            chat_id=user_openid, qq_chat_type="c2c", user_id=user_openid, chat_type="dm")

    async def _handle_group_message(self, d, msg_id, content, author, timestamp) -> None:
        group_openid = str(d.get("group_openid", ""))
        member = str(author.get("member_openid", ""))
        if not group_openid or not self._is_group_allowed(group_openid, member):
            return

        from .group_identity import strip_bot_invocation
        bot_ids = {str(value) for value in (self.config.extra.get("bot_member_openid"),) if value}
        mentions = d.get("mentions")
        if isinstance(mentions, list):
            bot_ids.update(str(m["id"]) for m in mentions
                           if isinstance(m, dict) and m.get("is_you") is True and m.get("id"))
        text = self._normalize_panel_command(strip_bot_invocation(content, bot_ids))

        self._group_reply_gate.note_addressed(group_openid)

        # Local patch: a bare slash command must reach the gateway *untagged* —
        # the "[当前消息] [群聊-*]" prefix + injected context would otherwise
        # defeat MessageEvent.is_command() (text.startswith("/")).
        _cmd = text.strip()
        if _cmd.startswith("/") and not _cmd[1:].strip().startswith("/") and not d.get("attachments"):
            from gateway.platforms.base import resolve_channel_prompt
            self._chat_type_map[group_openid] = "group"
            event = MessageEvent(
                source=self.build_source(chat_id=group_openid, user_id=member, chat_type="group"),
                text=_cmd, message_type=MessageType.TEXT, raw_message=d,
                message_id=msg_id, timestamp=self._parse_qq_timestamp(timestamp),
                channel_prompt=resolve_channel_prompt(self.config.extra, group_openid))
            # Reset the nearby-message buffer too: otherwise the first turn
            # of the new session inherits pre-reset chat via context injection.
            # Clear only after the gateway accepted/handled the reset command.
            await self.handle_message(event)
            if _cmd.split(maxsplit=1)[0].lower() in {"/new", "/reset"}:
                self._group_context_cache.pop(group_openid, None)
            return

        # Local patch: inject the group's rolling transcript, then tag the
        # current message with the speaker's identity (管理员 vs 群友-*) so the
        # model can always tell who is talking — even for a bare @bot.
        context_lines = ([] if self._group_reply_gate.enabled(group_openid)
                         else self._render_group_context(group_openid))
        context_lines.append("[当前消息]")
        speaker = self._speaker_label(member, author)
        text_body = text.strip()
        if text_body:
            context_lines.append(f"[群聊-{speaker}] {text_body}")
        else:
            context_lines.append(f"[群聊-{speaker}] （空消息：仅 @ 了机器人，无文字内容）")
        self._record_group_turn(group_openid, speaker, text_body or "（空消息：仅 @ 了机器人）")

        await self._ingest(
            d, msg_id, "\n".join(context_lines), d.get("attachments"), timestamp,
            chat_id=group_openid, qq_chat_type="group", user_id=member, chat_type="group")

    async def _handle_plain_group_message(self, d, msg_id, content, author, timestamp) -> bool | None:
        """GROUP_MESSAGE_CREATE: plain (non-@) group chat. If the bot is @mentioned,
        treat it as an @message; otherwise cache it as group context so a later
        @bot still sees the preceding conversation."""
        group_openid = str(d.get("group_openid", ""))
        member = str(author.get("member_openid", ""))
        if not group_openid or not self._is_group_allowed(group_openid, member):
            return
        if self._is_at_bot(d):
            await self._handle_group_message(d, msg_id, content, author, timestamp)
            return
        if self._group_reply_gate.enabled(group_openid) or d.get('attachments'):
            from .group_history import handle_unmentioned
            try:
                with self._group_images.tracking(group_openid, d.get("attachments")):
                    return await handle_unmentioned(self, d, msg_id, content, author, timestamp)
            except Exception:
                logger.exception("[%s] Non-mention intake failed: group=%s message=%s",
                                 self._log_tag, group_openid, msg_id)
            return
        # Unconfigured groups do not retain passive chat, including memory windows.
        return

    async def _handle_guild_message(self, d, msg_id, content, author, timestamp) -> None:
        channel_id = str(d.get("channel_id", ""))
        if not channel_id:
            return
        # group_policy ACL — guild channels are group-like; without it any guild
        # member could bypass the allowlist.
        guild_id = str(d.get("guild_id", ""))
        author_id = str(author.get("id", ""))
        if not self._is_group_allowed(guild_id or channel_id, author_id):
            logger.debug("[%s] Guild message blocked by ACL: channel=%s user=%s", self._log_tag, channel_id, author_id)
            return

        member = d.get("member") if isinstance(d.get("member"), dict) else {}
        nick = str(member.get("nick", "")) or str(author.get("username", ""))
        await self._ingest(
            d, msg_id, content, d.get("attachments"), timestamp,
            chat_id=channel_id, qq_chat_type="guild", user_id=author_id, user_name=nick or None, chat_type="group")

    async def _handle_dm_message(self, d, msg_id, content, author, timestamp) -> None:
        guild_id = str(d.get("guild_id", ""))
        if not guild_id:
            return
        # dm_policy ACL — without it any guild member could bypass the allowlist via DM.
        author_id = str(author.get("id", ""))
        if not self._is_dm_intake_allowed(author_id):
            logger.debug("[%s] Guild DM blocked by ACL: guild=%s user=%s", self._log_tag, guild_id, author_id)
            return
        await self._ingest(
            d, msg_id, content, d.get("attachments"), timestamp,
            chat_id=guild_id, qq_chat_type="dm", user_id=author_id, chat_type="dm")

    async def prepare_group_turn_history(self, async_store, session_id, event):
        """Plugin-owned replay seam, independent of the loader namespace."""
        from .group_history import prepare_turn_history
        return await prepare_turn_history(async_store, session_id, event)

    # ── Group transcript + speaker identity (local patch) ──

    @staticmethod
    def _strip_speaker_tag_echo(text: str) -> str:
        """Local patch: strip a leading ``[群聊-管理员]``/``[群聊-群友-X]`` tag the model
        mirrors from the injected group-context format onto its own reply."""
        return re.sub(r"^[ \t]*\[群聊-(?:管理员|群友-[^\]]*)\][ \t]*", "", text or "", count=1)

    # The display wraps a reply as "💭 **推理：**\n```\n<reasoning>\n```\n\n<answer>"
    # when reasoning display is on (gateway/run_turn.py). Inner fences are escaped
    # to \`\`\` by escape_code_fences_for_display, so a non-greedy match to the first
    # closing fence is safe.
    _REASONING_BLOCK_RE = re.compile(
        r"^\s*💭\s*\*\*(?:推理|Reasoning)[:：]\*\*\r?\n```[^\n]*\r?\n.*?\r?\n```[ \t]*\r?\n+",
        re.DOTALL,
    )

    @classmethod
    def _strip_reasoning_block(cls, text: str) -> str:
        """Local patch: record only the answer in the group transcript — the reasoning
        block is display-only and would otherwise be re-injected as context next turn."""
        return cls._REASONING_BLOCK_RE.sub("", text or "", count=1)

    def _record_group_turn(self, group_openid: str, label: str, text: str) -> None:
        """Append one line to a group's rolling transcript (inbound or outbound)."""
        if not group_openid or not text or not text.strip():
            return
        cache = self._group_context_cache.setdefault(group_openid, [])
        cache.append((label, text.strip()))
        if len(cache) > self._group_context_max:
            del cache[: len(cache) - self._group_context_max]

    def _render_group_context(self, group_openid: str, consume: bool = True) -> list[str]:
        """Render the stored transcript as injectable lines; ``consume=True`` drains it."""
        cache = self._group_context_cache.get(group_openid) or []
        if not cache:
            return []
        lines = ["[群聊上下文-最近对话]"]
        for label, msg_text in cache:
            lines.append(f"[群聊上下文-{label}] {msg_text}")
        lines.append("[/群聊上下文]")
        lines.append("")
        if consume:
            # Drained — these entries have been delivered to the model.
            self._group_context_cache[group_openid] = []
        return lines

    def _is_at_bot(self, d: dict[str, Any]) -> bool:
        """Use a self-marked mention or explicitly configured group bot identity."""
        bot_id = str(self.config.extra.get("bot_member_openid") or "")
        mentions = d.get("mentions")
        if isinstance(mentions, list):
            for m in mentions:
                if isinstance(m, dict) and (m.get("is_you") is True
                        or (bot_id and str(m.get("id") or "") == bot_id)):
                    return True
        if bot_id:
            from .group_identity import strip_bot_invocation
            content = str(d.get("content") or "").strip()
            return strip_bot_invocation(content, {bot_id}) != content
        return False

    def _is_master(self, user_id: str, author: Optional[dict[str, Any]] = None) -> bool:
        """Only the canonical routing sender establishes administrator identity."""
        return bool(self._home_channel and user_id and user_id == self._home_channel)

    def _speaker_label(self, user_id: str, author: Optional[dict[str, Any]] = None) -> str:
        """Human-readable label for a group speaker (master vs 群友-<short>)."""
        if self._is_master(user_id, author):
            return "管理员"
        short = user_id[:6] if len(user_id) >= 6 else user_id
        return f"群友-{short}"

    @staticmethod
    def _normalize_panel_command(text: str) -> str:
        """Strip the '/' QQ's command panel prepends to non-ASCII panel entries.

        Hermes built-in and skill commands are printable-ASCII, so a leading
        slash followed by a non-ASCII token is always a panel entry — demote it
        to normal chat so the gateway doesn't answer "Unknown command".
        """
        stripped = text.lstrip()
        if not stripped.startswith("/") or len(stripped) < 2:
            return text
        first_token = stripped[1:].split(maxsplit=1)[0] if stripped[1:].split() else ""
        if first_token and all(0x21 <= ord(ch) < 0x7F for ch in first_token):
            return text  # ASCII command (/help, /model, /fast…) — leave for gateway
        return stripped[1:].rstrip() or text

    _INBOUND_HANDLERS = {
        "C2C_MESSAGE_CREATE": "_handle_c2c_message",
        "GROUP_AT_MESSAGE_CREATE": "_handle_group_message",
        "GROUP_MESSAGE_CREATE": "_handle_plain_group_message",
        "GUILD_MESSAGE_CREATE": "_handle_guild_message",
        "GUILD_AT_MESSAGE_CREATE": "_handle_guild_message",
        "DIRECT_MESSAGE_CREATE": "_handle_dm_message"}

    # ── Shared inbound pipeline (all four message kinds) ──

    @staticmethod
    def _append_block(text: str, block: str) -> str:
        """Append *block* to *text* after a blank line (or return block alone if text is blank)."""
        return (text + "\n\n" + block).strip() if text.strip() else block

    async def _ingest(
        self, d: dict[str, Any], msg_id: str, content: str, attachments: Any, timestamp: str, *,
        chat_id: str, qq_chat_type: str, verbose: bool = False,
        event_options: Optional[dict[str, Any]] = None, **source_kwargs: Any) -> None:
        """Shared inbound tail: fold attachment transcripts/file info and quoted context
        into the text, drop empty events, remember the QQ chat kind and dispatch."""
        att = await self._process_attachments(attachments)
        text = content
        voice_transcripts = att["voice_transcripts"]
        if voice_transcripts:
            text = self._append_block(text, "\n".join(voice_transcripts))
        if att["attachment_info"]:
            text = self._append_block(text, att["attachment_info"])
        image_urls, image_media_types = att["image_urls"], att["image_media_types"]
        if verbose:
            logger.info("[%s] After processing: images=%d, voice=%d", self._log_tag, len(image_urls), len(voice_transcripts))

        quoted = await self._process_quoted_context(d)
        text = self._merge_quote_into(text, quoted["quote_block"])
        if quoted["image_urls"]:
            image_urls = image_urls + quoted["image_urls"]
            image_media_types = image_media_types + quoted["image_media_types"]
        if not text.strip() and not image_urls:
            return

        self._chat_type_map[chat_id] = qq_chat_type
        event = MessageEvent(
            source=self.build_source(chat_id=chat_id,** source_kwargs), text=text,
            message_type=self._detect_message_type(image_urls, image_media_types), raw_message=d,
            message_id=msg_id, media_urls=image_urls, media_types=image_media_types,
            media_text_inlined=[False] * len(image_urls),
            timestamp=self._parse_qq_timestamp(timestamp),
            **(event_options or {}),
        )
        from .delivery_binding import quoted_message_id
        from gateway.inbound_attachment_proofs import record_attachment
        event._inbound_attachment_receipts = []
        for index, (path, mime) in enumerate(zip(att["image_urls"], att["image_media_types"])):
            receipt = record_attachment(event, path, mime)
            if receipt:
                event._inbound_attachment_receipts.append((receipt, index))
        event.reply_to_message_id = quoted_message_id(d)
        if qq_chat_type == "group":
            from gateway.platforms.base import resolve_channel_prompt
            event.channel_prompt = event.channel_prompt or resolve_channel_prompt(self.config.extra, chat_id)
        await self.handle_message(event)

    # ── Quoted-message handling ──

    async def _process_quoted_context(self, d: dict[str, Any]) -> dict[str, Any]:
        """Process the quoted message a user is replying to (``message_type == 103``;
        referenced content + attachments live in ``msg_elements``). Quoted attachments
        go through _process_attachments so quoted voice gets STT and quoted images are
        cached identically. Returns ``{"quote_block", "image_urls", "image_media_types"}``;
        quote_block is "" when nothing is quoted."""
        empty = {"quote_block": "", "image_urls": [], "image_media_types": []}
        try:
            is_quote = int(d.get("message_type", 0) or 0) == 103
        except (TypeError, ValueError):
            is_quote = False
        elements = d.get("msg_elements")
        if not is_quote or not isinstance(elements, list) or not elements:
            return empty

        elements = [e for e in elements if isinstance(e, dict)]
        quoted_text_parts = [t for t in (str(e.get("content", "")).strip() for e in elements) if t]
        all_attachments = [
            a for e in elements if isinstance(e.get("attachments"), list) for a in e["attachments"] if isinstance(a, dict)]
        att_result = await self._process_attachments(all_attachments)
        quoted_images = att_result.get("image_urls") or []

        lines: list[str] = [" ".join(quoted_text_parts)] if quoted_text_parts else []
        lines.extend(att_result.get("voice_transcripts") or [])
        if att_result.get("attachment_info"):
            lines.append(att_result["attachment_info"])
        if not lines and not quoted_images:
            return empty
        # Images-only quote still gets a marker so the LLM knows context was referenced.
        return {
            "quote_block": "[Quoted message]:\n" + "\n".join(lines) if lines else "[Quoted message]: (image)",
            "image_urls": quoted_images,
            "image_media_types": att_result.get("image_media_types") or []}

    @staticmethod
    def _merge_quote_into(text: str, quote_block: str) -> str:
        """Prepend ``quote_block`` to *text*, separated by a blank line."""
        if not quote_block:
            return text
        return f"{quote_block}\n\n{text}".strip() if text.strip() else quote_block

    # ── Attachment processing ──

    @staticmethod
    def _detect_message_type(media_urls: list, media_types: list):
        if not media_urls:
            return MessageType.TEXT
        if not media_types:
            return MessageType.PHOTO
        first_type = media_types[0].lower()
        if "audio" in first_type or "voice" in first_type or "silk" in first_type:
            return MessageType.VOICE
        if "video" in first_type:
            return MessageType.VIDEO
        if "image" in first_type or "photo" in first_type:
            return MessageType.PHOTO
        logger.debug("Unknown media content_type '%s', defaulting to TEXT", first_type)
        return MessageType.TEXT

    async def _process_attachments(self, attachments: Any) -> dict[str, Any]:
        """Process inbound attachments uniformly. Returns ``{"image_urls",
        "image_media_types", "voice_transcripts", "attachment_info"}`` (cached image
        paths + MIME types, "[Voice] ..." transcripts, text description of other files)."""
        image_urls: list[str] = []
        image_media_types: list[str] = []
        voice_transcripts: list[str] = []
        other_attachments: list[str] = []

        for att in attachments[:4] if isinstance(attachments, list) else ():
            if not isinstance(att, dict):
                continue
            ct = str(att.get("content_type", "")).strip().lower()
            url = str(att.get("url", "")).strip()
            filename = str(att.get("filename", ""))
            if not url:
                other_attachments.append("[附件不可用：平台事件未提供下载 URL]")
                continue
            if url.startswith("//"):
                url = f"https:{url}"
            logger.debug(
                "[%s] Processing attachment: content_type=%s, url=%s, filename=%s",
                self._log_tag, ct, url[:80], filename)

            if self._is_voice_content_type(ct, filename):
                asr_refer, wav_url = (self._opt_str(att.get(k)) for k in ("asr_refer_text", "voice_wav_url"))
                transcript = await self._stt_voice_attachment(
                    url, ct, filename, asr_refer_text=asr_refer, voice_wav_url=wav_url)
                if transcript:
                    voice_transcripts.append(f"[Voice] {transcript}")
                    logger.debug("[%s] Voice transcript: %s", self._log_tag, transcript)
                else:
                    logger.warning("[%s] Voice STT failed for %s", self._log_tag, url[:60])
                    voice_transcripts.append("[Voice] [语音识别失败]")
                continue

            is_image = ct.startswith("image/")
            try:
                cached_path = await self._download_and_cache(url, ct, filename, max_bytes=32 * 1024 * 1024)
            except Exception as exc:
                logger.debug("[%s] Failed to cache %s: %s", self._log_tag, "image" if is_image else "attachment", exc)
                continue
            if not cached_path:
                continue
            if not is_image:
                label = "video" if ct.startswith("video/") else "file"
                other_attachments.append(f"[{label}: {filename or ct}；附件引用将在本回合提供]")
                image_urls.append(cached_path)
                image_media_types.append(ct if '/' in ct else "application/octet-stream")
            elif os.path.isfile(cached_path):
                image_urls.append(cached_path)
                image_media_types.append(ct or "image/jpeg")
            else:
                logger.warning("[%s] Cached image path does not exist: %s", self._log_tag, cached_path)

        return {
            "image_urls": image_urls,
            "image_media_types": image_media_types,
            "voice_transcripts": voice_transcripts,
            "attachment_info": "\n".join(other_attachments)}

    @staticmethod
    def _opt_str(value: Any) -> Optional[str]:
        return (value.strip() if isinstance(value, str) else "") or None

    async def _download_and_cache(
        self, url: str, content_type: str, original_name: str = "", *, max_bytes: Optional[int] = None,
    ) -> Optional[str]:
        """Download a URL and cache it locally (``original_name`` falls back to the URL basename)."""
        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL: {url[:80]}")
        if not self._http_client:
            return None
        try:
            if max_bytes is None:
                resp = await self._http_client.get(url, timeout=30.0, headers=self._qq_media_headers())
                resp.raise_for_status()
                data = resp.content
            else:
                # Quiet background images must not buffer an arbitrarily large
                # response. Enforce the cap even without a Content-Length header.
                async with self._http_client.stream(
                    "GET", url, timeout=30.0, headers=self._qq_media_headers(),
                ) as resp:
                    resp.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("background image exceeds size limit")
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except Exception as exc:
            logger.debug("[%s] Download failed for %s: %s", self._log_tag, url[:80], exc)
            return None

        if content_type.startswith("image/"):
            # Historical qqbot mapping: trust mimetypes' guess (never the shared table), fall back to .jpg.
            ext = ext_for_mime(content_type, use_defaults=False, use_mimetypes=True, fallback=".jpg") or ".jpg"
            return await cache_image_from_bytes_async(data, ext)
        if content_type == "voice" or content_type.startswith("audio/"):
            # QQ voice is usually .amr/.silk — convert to .wav for STT engines.
            return await self._convert_audio_to_wav(data, url)
        filename = original_name or Path(urlparse(url).path).name or "qq_attachment"
        return await cache_document_from_bytes_async(data, filename)

    @staticmethod
    def _is_voice_content_type(content_type: str, filename: str) -> bool:
        ct = content_type.strip().lower()
        if ct == "voice" or ct.startswith("audio/"):
            return True
        # content_type="file" is an explicit upload: never route .wav/.mp3 files into STT.
        if ct == "file":
            return False
        return filename.strip().lower().endswith(_VOICE_EXTENSIONS)

    def _qq_media_headers(self) -> dict[str, str]:
        """Authorization header for QQ multimedia CDN downloads (required, else non-200)."""
        return {"Authorization": f"QQBot {self._access_token}"} if self._access_token else {}

    async def _stt_voice_attachment(
        self, url: str, content_type: str, filename: str, *, asr_refer_text: Optional[str] = None,
        voice_wav_url: Optional[str] = None) -> Optional[str]:
        """Transcribe a voice attachment. Priority: QQ's free ``asr_refer_text`` →
        STT on ``voice_wav_url`` (pre-converted WAV, no SILK decode) → STT on the
        original URL (SILK→WAV). Returns the transcript or None."""
        if asr_refer_text:
            logger.debug("[%s] STT: using QQ asr_refer_text: %r", self._log_tag, asr_refer_text[:100])
            return asr_refer_text

        is_pre_wav = bool(voice_wav_url)
        download_url = url
        if is_pre_wav:
            download_url = f"https:{voice_wav_url}" if voice_wav_url.startswith("//") else voice_wav_url
            logger.debug("[%s] STT: using voice_wav_url (pre-converted WAV)", self._log_tag)

        from tools.url_safety import is_safe_url
        if not is_safe_url(download_url):
            logger.warning("[QQ] STT blocked unsafe URL: %s", download_url[:80])
            return None

        try:
            if not self._http_client:
                logger.warning("[%s] STT: no HTTP client", self._log_tag)
                return None
            download_headers = self._qq_media_headers()  # QQ CDN requires Authorization
            logger.debug(
                "[%s] STT: downloading voice from %s (pre_wav=%s, headers=%s)",
                self._log_tag, download_url[:80], is_pre_wav, bool(download_headers))
            resp = await self._http_client.get(
                download_url, timeout=30.0, headers=download_headers, follow_redirects=True)
            resp.raise_for_status()
            audio_data = resp.content
            logger.debug(
                "[%s] STT: downloaded %d bytes, content_type=%s",
                self._log_tag, len(audio_data), resp.headers.get("content-type", "unknown"))
            if len(audio_data) < 10:
                logger.warning("[%s] STT: downloaded data too small (%d bytes), skipping", self._log_tag, len(audio_data))
                return None

            if is_pre_wav:
                wav_path = self._write_temp(audio_data, ".wav")
                logger.debug("[%s] STT: using pre-converted WAV directly (%d bytes)", self._log_tag, len(audio_data))
            else:
                logger.debug("[%s] STT: converting to wav, filename=%r", self._log_tag, filename)
                wav_path = await self._convert_audio_to_wav_file(audio_data, filename)
                if not wav_path or not Path(wav_path).exists():
                    logger.warning("[%s] STT: ffmpeg conversion produced no output", self._log_tag)
                    return None

            logger.debug("[%s] STT: calling ASR on %s", self._log_tag, wav_path)
            try:
                transcript = await self._call_stt(wav_path)
            finally:
                self._unlink_quiet(wav_path)
            if transcript:
                logger.debug("[%s] STT success: %r", self._log_tag, transcript[:100])
            else:
                logger.warning("[%s] STT: ASR returned empty transcript", self._log_tag)
            return transcript
        except (OSError, httpx.HTTPStatusError, httpx.TransportError) as exc:
            logger.warning("[%s] STT failed for voice attachment: %s: %s", self._log_tag, type(exc).__name__, exc)
            return None

    @staticmethod
    def _write_temp(data: bytes, suffix: str) -> str:
        """Write *data* to a persistent NamedTemporaryFile and return its path."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            return tmp.name

    @staticmethod
    def _unlink_quiet(path: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)

    @staticmethod
    def _wav_ok(wav_path: str) -> bool:
        """True when *wav_path* exists and holds more than a bare 44-byte header."""
        return Path(wav_path).exists() and Path(wav_path).stat().st_size > 44

    @classmethod
    def _temp_pair(cls, audio_data: bytes, ext: str) -> tuple[str, str]:
        """Write *audio_data* to a temp ``<x>{ext}`` and return ``(src_path, sibling .wav path)``."""
        src_path = cls._write_temp(audio_data, ext)
        return src_path, src_path.rsplit(".", 1)[0] + ".wav"

    async def _convert_audio_to_wav_file(self, audio_data: bytes, filename: str) -> Optional[str]:
        """Convert audio bytes to a temp .wav: pilk (SILK, which ffmpeg can't decode)
        → ffmpeg → raw-PCM last resort. Returns the wav path or None."""
        ext = Path(filename).suffix.lower() or self._guess_ext_from_data(audio_data)
        logger.info(
            "[%s] STT: audio_data size=%d, ext=%r, first_20_bytes=%r",
            self._log_tag, len(audio_data), ext, audio_data[:20])
        src_path, wav_path = self._temp_pair(audio_data, ext)
        result = (
            await self._convert_silk_to_wav(src_path, wav_path)
            or await self._convert_ffmpeg_to_wav(src_path, wav_path)
            or await self._convert_raw_to_wav(audio_data, wav_path))
        self._unlink_quiet(src_path)
        return result

    _MAGIC_EXTS = (
        (b"#!SILK", ".silk"), (b"\x02!", ".silk"), (b"RIFF", ".wav"), (b"fLaC", ".flac"),
        (b"\xff\xfb", ".mp3"), (b"\xff\xf3", ".mp3"), (b"\xff\xf2", ".mp3"),
        (b"\x30\x26\xb2\x75", ".ogg"), (b"\x4f\x67\x67\x53", ".ogg"))

    @classmethod
    def _guess_ext_from_data(cls, data: bytes) -> str:
        """Guess file extension from magic bytes (unknown → .amr, QQ's most common)."""
        return next((ext for magic, ext in cls._MAGIC_EXTS if data.startswith(magic)), ".amr")

    @classmethod
    def _looks_like_silk(cls, data: bytes) -> bool:
        return cls._guess_ext_from_data(data) == ".silk"

    async def _convert_silk_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        """Convert to WAV with pilk: as-is first, then copied to .silk (pilk checks the extension)."""
        try:
            import pilk
        except ImportError:
            logger.warning("[%s] pilk not installed — cannot decode SILK audio. Run: "
                           f"{install_hint('silk')}", self._log_tag)
            return None

        silk_path = src_path.rsplit(".", 1)[0] + ".silk"
        try:
            for path, label, how in ((src_path, "", "direct"), (silk_path, " (as .silk)", ".silk")):
                try:
                    if path == silk_path:
                        import shutil

                        shutil.copy2(src_path, silk_path)
                    pilk.silk_to_wav(path, wav_path, rate=16000)
                    if self._wav_ok(wav_path):
                        logger.debug(
                            "[%s] pilk converted %s%s to wav (%d bytes)",
                            self._log_tag, Path(src_path).name, label, Path(wav_path).stat().st_size)
                        return wav_path
                except Exception as exc:
                    logger.debug("[%s] pilk %s conversion failed: %s", self._log_tag, how, exc)
        finally:
            self._unlink_quiet(silk_path)
        return None

    async def _convert_raw_to_wav(self, audio_data: bytes, wav_path: str) -> Optional[str]:
        """Last resort: wrap bytes as raw PCM 16-bit mono 16kHz WAV (garbage if not
        PCM, but the ASR engine returns empty instead of crashing)."""
        try:
            import wave

            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)
            return wav_path
        except Exception as exc:
            logger.debug("[%s] raw PCM fallback failed: %s", self._log_tag, exc)
            return None

    async def _convert_ffmpeg_to_wav(self, src_path: str, wav_path: str) -> Optional[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", wav_path,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr = await proc.stderr.read() if proc.stderr else b""
                logger.warning(
                    "[%s] ffmpeg failed for %s: %s",
                    self._log_tag, Path(src_path).name, stderr[:200].decode(errors="replace"))
                return None
        except (TimeoutError, FileNotFoundError) as exc:
            logger.warning("[%s] ffmpeg conversion error: %s", self._log_tag, exc)
            return None

        if not self._wav_ok(wav_path):
            logger.warning("[%s] ffmpeg produced no/small output for %s", self._log_tag, Path(src_path).name)
            return None
        logger.debug(
            "[%s] ffmpeg converted %s to wav (%d bytes)",
            self._log_tag, Path(src_path).name, Path(wav_path).stat().st_size)
        return wav_path

    def _resolve_stt_config(self) -> Optional[dict[str, Any]]:
        """Resolve STT backend: ``extra["stt"]`` config first, then ``QQ_STT_*`` env
        vars; None when unconfigured (QQ's built-in ASR still works). ``timeout`` (seconds,
        default 60) follows the shared STT client default so a self-hosted model's cold start
        is not cut off at 30s (#112939)."""
        from tools.transcription_common import DEFAULT_STT_TIMEOUT, _config_number  # lazy: keep adapter light
        stt_cfg = (self.config.extra or {}).get("stt")
        if isinstance(stt_cfg, dict) and stt_cfg.get("enabled") is not False:
            base_url = stt_cfg.get("baseUrl") or stt_cfg.get("base_url", "")
            api_key = stt_cfg.get("apiKey") or stt_cfg.get("api_key", "")
            model = stt_cfg.get("model", "")
            timeout = _config_number(stt_cfg, "timeout", DEFAULT_STT_TIMEOUT)
            if base_url and api_key:
                return {"base_url": base_url.rstrip("/"), "api_key": api_key, "model": model or "whisper-1",
                        "timeout": timeout}
            if api_key:  # provider-only config
                provider = stt_cfg.get("provider", "zai")
                base_url = _STT_PROVIDER_BASE_URLS.get(provider, "")
                if base_url:
                    default_model = "glm-asr" if provider in {"zai", "glm"} else "whisper-1"
                    return {"base_url": base_url, "api_key": api_key, "model": model or default_model,
                            "timeout": timeout}

        qq_stt_key = _resolve_qq_secret("QQ_STT_API_KEY", "")
        if qq_stt_key:
            base_url = _resolve_qq_secret("QQ_STT_BASE_URL", _STT_PROVIDER_BASE_URLS["zai"])
            model = _resolve_qq_secret("QQ_STT_MODEL", "glm-asr")
            return {"base_url": base_url.rstrip("/"), "api_key": qq_stt_key, "model": model,
                    "timeout": DEFAULT_STT_TIMEOUT}
        return None

    async def _call_stt(self, wav_path: str) -> Optional[str]:
        """Transcribe a wav via an OpenAI-compatible STT API; None if unconfigured/failed."""
        stt_cfg = self._resolve_stt_config()
        if not stt_cfg:
            logger.warning("[%s] STT not configured (no stt config or QQ_STT_API_KEY)", self._log_tag)
            return None

        base_url, api_key, model = stt_cfg["base_url"], stt_cfg["api_key"], stt_cfg["model"]
        try:
            with open(wav_path, "rb") as f:  # noqa: ASYNC230 -- file handle is streamed to the upload; a local open() is non-blocking in practice
                resp = await self._http_client.post(
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (Path(wav_path).name, f, "audio/wav")},
                    data={"model": model},
                    timeout=stt_cfg["timeout"])
            resp.raise_for_status()
            result = resp.json()
            # Zhipu/GLM: {"choices": [{"message": {"content": ...}}]}; OpenAI/Whisper: {"text": ...}
            choices = result.get("choices", [])
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            return content.strip() or result.get("text", "").strip() or None
        except (OSError, httpx.HTTPStatusError) as exc:
            logger.warning("[%s] STT API call failed (model=%s, base=%s): %s", self._log_tag, model, base_url[:50], exc)
            return None

    async def _convert_audio_to_wav(self, audio_data: bytes, source_url: str) -> Optional[str]:
        """Convert audio bytes to .wav (pilk for SILK, else ffmpeg) and cache the result;
        on conversion failure the original bytes are cached as ``qq_voice<ext>``."""
        ext = Path(urlparse(source_url).path).suffix.lower()
        if ext not in _AUDIO_URL_EXTENSIONS:
            ext = self._guess_ext_from_data(audio_data)
        src_path, wav_path = self._temp_pair(audio_data, ext)
        is_silk = ext == ".silk" or self._looks_like_silk(audio_data)
        convert = self._convert_silk_to_wav if is_silk else self._convert_ffmpeg_to_wav
        try:
            if not await convert(src_path, wav_path):
                logger.warning("[%s] audio conversion failed for %s (format=%s)", self._log_tag, source_url[:60], ext)
                return await cache_document_from_bytes_async(audio_data, f"qq_voice{ext}")
        except Exception:
            return await cache_document_from_bytes_async(audio_data, f"qq_voice{ext}")
        finally:
            self._unlink_quiet(src_path)

        try:
            wav_data = Path(wav_path).read_bytes()
            os.unlink(wav_path)
            return await cache_document_from_bytes_async(wav_data, "qq_voice.wav")
        except Exception as exc:
            logger.debug("[%s] Failed to read converted wav: %s", self._log_tag, exc)
            return None

    # ── Outbound messaging — REST API ──

    def _require_http_client(self) -> httpx.AsyncClient:
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized — not connected?")
        return self._http_client

    async def _api_request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None, timeout: float = DEFAULT_API_TIMEOUT,
    ) -> dict[str, Any]:
        client = self._require_http_client()
        headers = await self._auth_headers()
        try:
            resp = await client.request(method, f"{API_BASE}{path}", headers=headers, json=body, timeout=timeout)
            if method.upper() == "DELETE" and resp.status_code == 200 and not resp.content:
                return {}
            if resp.status_code >= 400:
                from .receipt_adapter import QQAPIRejection
                raise QQAPIRejection(f"QQ Bot API error [{resp.status_code}]")
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("QQ Bot API returned a non-object response")
            if data.get("code") not in (None, 0, "0"):
                from .receipt_adapter import QQAPIRejection
                raise QQAPIRejection("QQ Bot API returned a business error")
            return data
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"QQ Bot API timeout [{path}]: {exc}") from exc

    async def _auth_headers(self) -> dict[str, str]:
        """JSON REST headers with a fresh bot token."""
        token = await self._ensure_token()
        return {"Authorization": f"QQBot {token}", "Content-Type": "application/json", "User-Agent": build_user_agent()}

    async def _upload_media(
        self, target_type: str, target_id: str, file_type: int, url: Optional[str] = None,
        file_data: Optional[str] = None, srv_send_msg: bool = False, file_name: Optional[str] = None,
    ) -> dict[str, Any]:
        path = self._rest_path(target_type, target_id, "files")
        body: dict[str, Any] = {"file_type": file_type, "srv_send_msg": srv_send_msg}
        if url:
            body["url"] = url
        elif file_data:
            body["file_data"] = file_data
        if file_type == MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name
        for attempt in range(3):  # retry transient upload failures
            try:
                return await self._api_request("POST", path, body, timeout=FILE_UPLOAD_TIMEOUT)
            except RuntimeError as exc:
                if attempt == 2 or any(kw in str(exc) for kw in ("400", "401", "Invalid", "timeout", "Timeout")):
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))

    _RECONNECT_WAIT_SECONDS = 15.0  # max wait for reconnection before giving up on send
    _RECONNECT_POLL_INTERVAL = 0.5  # is_connected poll interval while waiting

    async def _wait_for_reconnection(self) -> bool:
        """Poll is_connected for up to _RECONNECT_WAIT_SECONDS — covers the race where
        send() lands between a disconnect and _listen_loop's reconnect."""
        logger.info(
            "[%s] Not connected — waiting for reconnection (up to %.0fs)", self._log_tag, self._RECONNECT_WAIT_SECONDS
        )
        waited = 0.0
        while waited < self._RECONNECT_WAIT_SECONDS:
            await asyncio.sleep(self._RECONNECT_POLL_INTERVAL)
            waited += self._RECONNECT_POLL_INTERVAL
            if self.is_connected:
                logger.info("[%s] Reconnected after %.1fs", self._log_tag, waited)
                return True
        logger.warning("[%s] Still not connected after %.0fs", self._log_tag, self._RECONNECT_WAIT_SECONDS)
        return False

    @property
    def _NOT_CONNECTED(self) -> SendResult:
        return SendResult(success=False, error="Not connected", retryable=True)

    async def _ensure_connected(self) -> bool:
        """True when connected now or after waiting for the listener to reconnect."""
        return self.is_connected or await self._wait_for_reconnection()

    async def send_final_ledgered(self, event, session_key, text_content, metadata, *,
                                  reply_to, is_ephemeral_response=False):
        from .delivery_binding import bind_delivery, turn_context
        with bind_delivery(event.source.chat_id, turn_context(event, session_key)):
            return await super().send_final_ledgered(
                event, session_key, text_content, metadata,
                reply_to=reply_to, is_ephemeral_response=is_ephemeral_response)

    async def _deliver_attachments(self, event, extracted, metadata, *, anything_sent, record_delivery):
        from .delivery_binding import bind_delivery, turn_context
        with bind_delivery(event.source.chat_id, turn_context(event, self._source_session_key(event.source))):
            return await super()._deliver_attachments(
                event, extracted, metadata, anything_sent=anything_sent, record_delivery=record_delivery)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        from .delivery_binding import bind_delivery
        with bind_delivery(chat_id):
            return await self._send_text_impl(chat_id, content, reply_to, metadata)

    async def _send_text_impl(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send text/markdown: format, split via truncate_message(), retry transient failures."""
        del metadata
        # Local patch: never let the model's mirrored "[群聊-管理员]" tag reach the wire.
        content = self._strip_speaker_tag_echo(content)
        if not await self._ensure_connected():
            return self._NOT_CONNECTED
        if not content or not content.strip():
            return SendResult(success=True)

        chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
        last_result = SendResult(success=False, error="No chunks")
        returned_ids = []
        for chunk in chunks:
            last_result = await self._send_chunk(chat_id, chunk, reply_to)
            if not last_result.success:
                break
            if last_result.message_id:
                returned_ids.append(last_result.message_id)
            reply_to = None  # only reply_to the first chunk
        last_result.continuation_message_ids = tuple(returned_ids[:-1] if last_result.message_id else returned_ids)
        # Local patch: record our own reply in the group's rolling transcript, so
        # the next @message in this group sees the full exchange rather than a
        # cold start. QQ never pushes plain group messages, so our outbound turns
        # are the only bridge between two consecutive @messages.
        if last_result.success and chat_id in self._group_context_cache:
            # Drop the display-only reasoning block, then keep the entry short.
            recorded = self._strip_reasoning_block(self.format_message(content)).strip()
            if len(recorded) > 300:
                recorded = recorded[:300] + "…"
            if recorded:
                self._record_group_turn(chat_id, self._assistant_label, recorded)
        return last_result

    _PERMANENT_SEND_ERRORS = ("invalid", "forbidden", "not found")

    async def _send_chunk(self, chat_id: str, content: str, reply_to: Optional[str] = None) -> SendResult:
        sender = self._text_sender(self._guess_chat_type(chat_id))
        if sender is None:
            return SendResult(success=False, error=f"Unknown chat type for {chat_id}")
        try:
            return await sender(chat_id, content, reply_to)
        except Exception as exc:
            from .delivery_binding import current_delivery
            from .receipt_adapter import QQAPIRejection
            status = "error" if isinstance(exc, QQAPIRejection) else "unknown"
            return SendResult(success=False, error=f"QQ send {status}: {type(exc).__name__}", retryable=False,
                              raw_response={"qq_logical_id": current_delivery(chat_id).logical_id,
                                            "delivery_status": status})

    @staticmethod
    def _rest_path(chat_type: str, target_id: str, endpoint: str) -> str:
        """``/v2/{users|groups}/{id}/{endpoint}`` for a c2c user or a group."""
        kind = "users" if chat_type == "c2c" else "groups"
        return f"/v2/{kind}/{target_id}/{endpoint}"

    @classmethod
    def _messages_path(cls, chat_type: str, target_id: str) -> str:
        return cls._rest_path(chat_type, target_id, "messages")

    async def _post_message(self, path: str, body: dict[str, Any]) -> SendResult:
        """POST a message body and wrap the response as a successful SendResult."""
        from .receipt_adapter import recorded_post, receipt_metadata
        _, receipt = await recorded_post(self, API_BASE, path, body)
        return SendResult(success=True, message_id=receipt.message_id,
                          raw_response={"qq_receipt": receipt_metadata(receipt)})

    async def recall_own_group_message(self, *, logical_id: str, group_id: str,
                                       message_id: str, retry_unknown: bool = False):
        """Trusted-call API only; caller authorizes the action before invoking."""
        from .receipt_adapter import recall_message
        return await recall_message(self, API_BASE, logical_id=logical_id, group_id=group_id,
                                    message_id=message_id, retry_unknown=retry_unknown)

    async def _send_text_to(
        self, chat_type: str, target_id: str, content: str, reply_to: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None) -> SendResult:
        """Send text (optionally with an inline keyboard) to a c2c user or group."""
        self._next_msg_seq(reply_to or target_id)
        body = self._build_text_body(content, reply_to)
        if reply_to:
            body["msg_id"] = reply_to
        if keyboard is not None:
            body["keyboard"] = keyboard.to_dict()
        return await self._post_message(self._messages_path(chat_type, target_id), body)

    async def _send_c2c_text(self, openid, content, reply_to=None, keyboard=None) -> SendResult:
        return await self._send_text_to("c2c", openid, content, reply_to, keyboard)

    async def _send_group_text(self, group_openid, content, reply_to=None, keyboard=None) -> SendResult:
        return await self._send_text_to("group", group_openid, content, reply_to, keyboard)

    _TEXT_SENDERS = {"c2c": "_send_c2c_text", "group": "_send_group_text", "guild": "_send_guild_text"}

    def _text_sender(self, chat_type: str, *, keyboard_ok: bool = False):
        """Bound text sender for *chat_type* (None if unsupported); guild lacks keyboards."""
        if keyboard_ok and chat_type == "guild":
            return None
        name = self._TEXT_SENDERS.get(chat_type)
        return getattr(self, name) if name else None

    async def _send_guild_text(self, channel_id: str, content: str, reply_to: Optional[str] = None) -> SendResult:
        body: dict[str, Any] = {"content": content[: self.MAX_MESSAGE_LENGTH]}
        if reply_to:
            body["msg_id"] = reply_to
        return await self._post_message(f"/channels/{channel_id}/messages", body)

    # ── Inline-keyboard outbound helpers (approval / update-prompt flows) ──

    async def send_with_keyboard(
        self, chat_id: str, content: str, keyboard: InlineKeyboard, reply_to: Optional[str] = None,
    ) -> SendResult:
        """Send ONE text message with an inline keyboard (no chunking — splitting
        would orphan the buttons; keep bodies short). Guild chats are unsupported."""
        if not await self._ensure_connected():
            return self._NOT_CONNECTED
        chat_type = self._guess_chat_type(chat_id)
        sender = self._text_sender(chat_type, keyboard_ok=True)
        if sender is None:
            return SendResult(
                success=False, error=f"Inline keyboards not supported for chat_type {chat_type!r}", retryable=False)
        truncated = self.format_message(content)[: self.MAX_MESSAGE_LENGTH]
        try:
            return await sender(chat_id, truncated, reply_to, keyboard=keyboard)
        except Exception as exc:
            logger.error("[%s] send_with_keyboard failed: %s", self._log_tag, exc)
            return SendResult(success=False, error=str(exc) or type(exc).__name__)

    async def send_approval_request(
        self, chat_id: str, req: ApprovalRequest, reply_to: Optional[str] = None) -> SendResult:
        """Send a 3-button approval request (allow-once / allow-always / deny);
        clicks come back as INTERACTION_CREATE decoded by parse_approval_button_data."""
        from .keyboards import build_approval_text
        keyboard = build_approval_keyboard(req.session_key, allow_permanent=getattr(req, "allow_permanent", True),
                                           request_id=req.request_id)
        return await self.send_with_keyboard(chat_id, build_approval_text(req), keyboard, reply_to=reply_to)

    # Cross-adapter gateway contract: gateway/run.py detects send_exec_approval /
    # send_update_prompt on the adapter class for button-based approval/update UX.

    _APPROVAL_TIMEOUT_SECONDS = 300  # matches gateway's default gateway_timeout

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Keyboard-card approval (called while the agent blocks on approval); clicks resolve via
        _default_interaction_dispatch. QQ's 3-button keyboard has no session tier and no thread /
        DM targeting, so only the ``always`` choice and the raw command/reason are used."""
        description = prompt.description
        from gateway.request_security import policy_for_adapter
        parsed = self._parse_gateway_session_key(prompt.session_key) or {}
        source = self.build_source(chat_id=prompt.chat_id, chat_type=parsed.get("chat_type", ""))
        restricted = policy_for_adapter(self, source) is not None
        # Identity must come from the original request, never a snapshot of the current queue.
        request_id = prompt.request_id
        if restricted and not request_id:
            return SendResult(success=False, error="Approval request identity unavailable")
        if prompt.smart_denied:
            description += " Owner override applies to this one operation only."
        req = ApprovalRequest(
            session_key=prompt.session_key, title="Execute this command?", description=description,
            command_preview=prompt.command, timeout_sec=self._APPROVAL_TIMEOUT_SECONDS,
            request_id=request_id,
            allow_permanent="always" in prompt.choices)
        # QQ requires a msg_id for passive replies; the last inbound id is the natural one.
        return await self.send_approval_request(
            prompt.chat_id, req, reply_to=self._last_msg_id.get(prompt.chat_id))

    async def send_update_prompt(
        self, chat_id: str, prompt: str, default: str = "", session_key: str = "",
        metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Yes/No update-confirmation prompt; button clicks (``update_prompt:y|n``)
        are written to ``~/.hermes/.update_response`` by the interaction callback."""
        del session_key, metadata  # present for contract parity only.
        default_hint = f" (default: {default})" if default else ""
        content = f"☤ **Update Needs Your Input**\n\n{prompt}{default_hint}"
        keyboard = build_update_prompt_keyboard()
        from gateway.request_security import policy_for_adapter
        source = self.build_source(chat_id=chat_id, chat_type=self._guess_chat_type(chat_id))
        if policy_for_adapter(self, source) is not None:
            import secrets
            token = secrets.token_hex(16)
            cards = getattr(self, "_role_update_cards", None)
            if cards is None:
                cards = self._role_update_cards = {}
            cards[chat_id] = token
            for row in keyboard.content.rows:
                for button in row.buttons:
                    answer = parse_update_prompt_button_data(button.action.data)
                    button.action.data = f"update-request:{token}:{answer}"
        return await self.send_with_keyboard(
            chat_id, content, keyboard, reply_to=self._last_msg_id.get(chat_id)
        )

    def _build_text_body(self, content: str, reply_to: Optional[str] = None) -> dict[str, Any]:
        msg_seq = self._next_msg_seq(reply_to or "default")
        text = content[: self.MAX_MESSAGE_LENGTH]
        if self._markdown_support:
            return {"markdown": {"content": text}, "msg_type": MSG_TYPE_MARKDOWN, "msg_seq": msg_seq}
        body: dict[str, Any] = {"content": text, "msg_type": MSG_TYPE_TEXT, "msg_seq": msg_seq}
        if reply_to:
            body["message_reference"] = {"message_id": reply_to}
        return body

    # ── Native media sending ──

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Send an image natively via QQ Bot API upload; URL sources fall back to text."""
        del metadata
        result = await self._send_media(chat_id, image_url, MEDIA_TYPE_IMAGE, "image", caption, reply_to)
        if result.success or not self._is_url(image_url):
            return result
        logger.warning("[%s] Image send failed, falling back to text: %s", self._log_tag, result.error)
        fallback = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback, reply_to=reply_to)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, image_path, MEDIA_TYPE_IMAGE, "image", caption, reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, audio_path, MEDIA_TYPE_VOICE, "voice", caption, reply_to)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, video_path, MEDIA_TYPE_VIDEO, "video", caption, reply_to)

    async def send_document(
        self, chat_id, file_path, caption=None, file_name=None, reply_to=None, **kwargs
    ) -> SendResult:
        return await self._send_media(
            chat_id, file_path, MEDIA_TYPE_FILE, "file", caption, reply_to, file_name=file_name)

    async def _send_media(
        self, chat_id: str, media_source: str, file_type: int, kind: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, file_name: Optional[str] = None) -> SendResult:
        """Upload media and send as a native message. HTTP(S) URLs → single ``POST
        .../files`` with ``url=`` (QQ fetches it). Local files → chunked upload
        (prepare / PUT parts / complete), up to the platform's ~100 MB per-file limit."""
        from agent.request_security import current_policy
        policy = current_policy()
        if policy is None:
            from gateway.request_security import policy_for_adapter
            policy = policy_for_adapter(self, self.build_source(
                chat_id=chat_id, chat_type=self._guess_chat_type(chat_id)))
        if policy is not None:
            if not policy.is_admin and (policy.platform != "qqbot" or policy.chat_id != chat_id):
                return SendResult(success=False, error="Request policy denied attachment destination")
            if not policy.allows_media(media_source):
                return SendResult(success=False, error="Request policy denied attachment")
        if not await self._ensure_connected():
            return self._NOT_CONNECTED
        chat_type = self._guess_chat_type(chat_id)
        if chat_type == "guild":
            return SendResult(success=False, error="Guild media send not supported via this path")

        try:
            if self._is_url(media_source):
                resolved_name = file_name or Path(urlparse(media_source).path).name or "media"
                upload = await self._upload_media(
                    chat_type, chat_id, file_type, url=media_source, srv_send_msg=False,
                    file_name=resolved_name if file_type == MEDIA_TYPE_FILE else None)
            else:
                upload = await self._upload_local_file(chat_type, chat_id, media_source, file_type, file_name)

            file_info = upload.get("file_info") or (upload.get("data", {}) or {}).get("file_info")
            if not file_info:
                return SendResult(success=False, error=f"Upload returned no file_info: {upload}")
            body: dict[str, Any] = {
                "msg_type": MSG_TYPE_MEDIA, "media": {"file_info": file_info}, "msg_seq": self._next_msg_seq(chat_id)}
            if caption:
                body["content"] = caption[: self.MAX_MESSAGE_LENGTH]
            if reply_to:
                body["msg_id"] = reply_to
            return await self._post_message(self._messages_path(chat_type, chat_id), body)
        except UploadDailyLimitExceededError as exc:
            # Non-retryable quota hit; give the model actionable text.
            logger.warning("[%s] Daily upload limit exceeded for %s (%s)", self._log_tag, exc.file_name, exc.file_size_human)
            return SendResult(
                success=False, retryable=False,
                error=f"QQ daily upload limit exceeded for {exc.file_name!r} ({exc.file_size_human}). Retry tomorrow.")
        except UploadFileTooLargeError as exc:
            logger.warning(
                "[%s] File too large: %s (%s, platform limit %s)",
                self._log_tag, exc.file_name, exc.file_size_human, exc.limit_human)
            return SendResult(
                success=False, retryable=False,
                error=f"{exc.file_name!r} ({exc.file_size_human}) exceeds the QQ per-file upload limit ({exc.limit_human}).")
        except Exception as exc:
            logger.error("[%s] Media send failed: %s", self._log_tag, exc)
            return SendResult(success=False, error=str(exc) or type(exc).__name__)

    async def _upload_local_file(
        self, chat_type: str, chat_id: str, media_source: str, file_type: int, file_name: Optional[str],
    ) -> dict[str, Any]:
        """Chunked-upload a local file; returns the complete response whose ``file_info`` goes
        into the RichMedia body. Raises UploadDailyLimitExceededError / UploadFileTooLargeError
        from the uploader, ValueError for placeholder paths like ``<path>``, FileNotFoundError."""
        client = self._require_http_client()
        local_path = Path(media_source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()
        if not local_path.exists() or not local_path.is_file():
            if media_source.startswith("<") or len(media_source) < 3:
                raise ValueError(f"Invalid media source (looks like a placeholder): {media_source!r}")
            raise FileNotFoundError(f"Media file not found: {local_path}")

        uploader = ChunkedUploader(api_request=self._api_request, http_put=client.put, log_tag=self._log_tag)
        return await uploader.upload(
            chat_type=chat_type, target_id=chat_id, file_path=str(local_path), file_type=file_type,
            file_name=file_name or local_path.name)

    # ── Typing indicator ──

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """C2C-only input notify, debounced to ~50s (API shows a 60s indicator);
        needs the last inbound msg_id from ``_last_msg_id``."""
        msg_id = self._last_msg_id.get(chat_id)
        now = time.time()
        if (
            not self.is_connected or self._guess_chat_type(chat_id) != "c2c" or not msg_id
            or now - self._typing_sent_at.get(chat_id, 0.0) < self._TYPING_DEBOUNCE_SECONDS
        ):
            return
        try:
            body = {
                "msg_type": MSG_TYPE_INPUT_NOTIFY,
                "msg_id": msg_id,
                "input_notify": {"input_type": 1, "input_second": self._TYPING_INPUT_SECONDS},
                "msg_seq": self._next_msg_seq(chat_id)}
            await self._api_request("POST", f"/v2/users/{chat_id}/messages", body)
            self._typing_sent_at[chat_id] = now
        except Exception as exc:
            logger.debug("[%s] send_typing failed: %s", self._log_tag, exc)

    # ── Format / chat info / helpers ──

    def format_message(self, content: str) -> str:
        """Repair QQ bold punctuation boundaries, or strip markup in plain-text mode."""
        return normalize_qq_markdown(content) if self._markdown_support else strip_markdown(content)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        chat_type = self._guess_chat_type(chat_id)
        return {"name": chat_id, "type": "group" if chat_type in {"group", "guild"} else "dm"}

    @staticmethod
    def _is_url(source: str) -> bool:
        return urlparse(str(source)).scheme in {"http", "https"}

    def _guess_chat_type(self, chat_id: str) -> str:
        """Determine chat type from stored inbound metadata, fallback to 'c2c'."""
        return self._chat_type_map.get(chat_id, "c2c")

    @staticmethod
    def _strip_at_mention(content: str) -> str:
        # Local patch: QQ @-messages arrive as "<@openid> text" (or "<@!openid>"),
        # sometimes "@name text", and can carry several leading mentions
        # ("@a @b /new"). Strip every leading mention token so a bare "/command"
        # survives for the gateway's is_command() check.
        stripped = content.strip()
        pattern = re.compile(r"^(?:<@!?\S+>|@\S+)\s*")
        while True:
            new = pattern.sub("", stripped, count=1)
            if new == stripped:
                break
            stripped = new
        return stripped

    def _entry_matches(self, entries: list[str], target: str) -> bool:
        normalized_target = str(target).strip().lower()
        return any(str(e).strip().lower() in ("*", normalized_target) for e in entries)

    def _parse_qq_timestamp(self, raw: str) -> datetime:
        """Parse a QQ timestamp — ISO 8601 string (current) or integer ms (legacy)."""
        if raw:
            with contextlib.suppress(ValueError, TypeError):
                return datetime.fromisoformat(raw)
            with contextlib.suppress(ValueError, TypeError):
                return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
        return datetime.now(tz=UTC)
