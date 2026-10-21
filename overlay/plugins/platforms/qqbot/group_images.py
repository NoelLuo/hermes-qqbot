"""Cache quiet QQ images without inference; attach them only at a turn boundary."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from gateway.message_timestamps import coerce_message_timestamp, format_message_timestamp

logger = logging.getLogger(__name__)
IMAGE_METADATA_KEY = "qq_context_images"
MAX_CONTEXT_IMAGES = 4
IMAGE_WINDOW_SECONDS = 600
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_attachments(attachments: Any) -> list[dict]:
    if not isinstance(attachments, list):
        return []
    return [a for a in attachments if isinstance(a, dict)
            and str(a.get("content_type", "")).lower().startswith("image/")]


def _image_identity(path: str) -> tuple[str, str]:
    # Metadata is persisted locally, but must never become an arbitrary-file-read
    # primitive if a row was edited or restored with a forged cache path.
    from gateway.platforms.base import get_image_cache_dir

    resolved = Path(path).resolve()
    if not resolved.is_relative_to(get_image_cache_dir().resolve()):
        raise ValueError("image outside the active profile's image cache")
    if not resolved.is_file() or not 0 < resolved.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("image missing or exceeds the background-image size limit")
    return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest()


class QuietGroupImages:
    def __init__(self):
        self._pending: dict[str, set[asyncio.Task]] = {}
        self._downloads = asyncio.Semaphore(2)

    @contextmanager
    def tracking(self, group: str, attachments: Any):
        task = asyncio.current_task()
        tracked = task is not None and isinstance(attachments, list) and bool(attachments)
        if tracked:
            self._pending.setdefault(group, set()).add(task)
        try:
            yield
        finally:
            if tracked:
                pending = self._pending.get(group, set())
                pending.discard(task)
                if not pending:
                    self._pending.pop(group, None)

    async def wait_pending(self, group: str) -> bool:
        tasks = self._pending.get(group, set()) - {asyncio.current_task()}
        if tasks:
            # An image and the following question may arrive concurrently. Wait
            # for the observed row to commit, not merely for its download to end.
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                logger.warning("QQ nearby images still pending at turn boundary: group=%s", group)
                return False
        return True

    async def cache(self, adapter: Any, attachments: Any) -> list[dict]:
        records = []
        for attachment in image_attachments(attachments)[:MAX_CONTEXT_IMAGES]:
            mime = str(attachment["content_type"]).split(";", 1)[0].lower()
            record = {"mime": mime, "status": "unavailable"}
            try:
                async with asyncio.timeout(15), self._downloads:
                    url = str(attachment.get("url") or "")
                    if url.startswith("//"):
                        url = "https:" + url
                    cached = await adapter._download_and_cache(
                        url, mime, str(attachment.get("filename") or ""), max_bytes=MAX_IMAGE_BYTES)
                    if cached:
                        path, digest = await asyncio.to_thread(_image_identity, cached)
                        record.update(path=path, sha256=digest, status="cached")
            except Exception as exc:
                logger.warning("QQ quiet image cache failed: %s", type(exc).__name__)
            records.append(record)
        return records


def _pending_image_rows(rows: list[dict], reply_to: str | None) -> list[dict]:
    from .group_history import is_observed

    pending = []
    quoted = []
    for row in rows:
        if is_observed(row):
            pending.append(row)
            if reply_to and row.get("message_id") == reply_to:
                quoted.append(row)
        elif row.get("role") == "user":
            pending.clear()
    # Explicit references are allowed to revisit an already consumed image;
    # automatic nearby context is limited to the current unconsumed window.
    return quoted + [row for row in reversed(pending) if row not in quoted]


async def attach_nearby_images(rows: list[dict], event: Any) -> None:
    """Populate real event media (not just path strings in a prompt)."""
    from gateway.platforms.event import MessageType

    trigger_time = coerce_message_timestamp(event.timestamp)
    if trigger_time is None or not math.isfinite(trigger_time):
        return
    # Existing current/quoted images have priority, including byte-identical
    # images cached under different filenames by two QQ messages.
    seen_paths = set(event.media_urls)
    seen_hashes = set()
    for path, mime in zip(event.media_urls, event.media_types):
        if mime.startswith("image/"):
            try:
                _, digest = await asyncio.to_thread(_image_identity, path)
                seen_hashes.add(digest)
            except (OSError, ValueError):
                pass  # Existing attachments keep their normal validation path.
    descriptions = []
    added = 0
    for row in _pending_image_rows(rows, event.reply_to_message_id):
        timestamp = coerce_message_timestamp(row.get("timestamp"))
        if timestamp is None or not 0 <= trigger_time - timestamp <= IMAGE_WINDOW_SECONDS:
            continue
        metadata = row.get("display_metadata") or {}
        images = metadata.get(IMAGE_METADATA_KEY, []) if isinstance(metadata, dict) else []
        for image in images if isinstance(images, list) else []:
            if not isinstance(image, dict):
                continue
            caption = str(row.get("content") or "").splitlines()[0][:300]
            origin = f"消息 {row.get('message_id', '?')} {format_message_timestamp(row.get('timestamp'))} {caption}"
            try:
                if image.get("status") != "cached":
                    raise ValueError("image was not cached")
                path, digest = await asyncio.to_thread(_image_identity, str(image.get("path") or ""))
                if digest != image.get("sha256"):
                    raise ValueError("cached image contents changed")
                mime = str(image.get("mime") or "")
                if not mime.startswith("image/"):
                    raise ValueError("invalid image type")
            except (OSError, ValueError):
                descriptions.append(f"[附近图片不可用，不能据此识图] {origin}")
                continue
            if path in seen_paths or digest in seen_hashes:
                continue
            if added >= MAX_CONTEXT_IMAGES:
                break
            seen_paths.add(path)
            seen_hashes.add(digest)
            event.media_urls.append(path)
            event.media_types.append(mime)
            if event.media_text_inlined:
                event.media_text_inlined.append(False)
            added += 1
            image_number = sum(mime.startswith("image/") for mime in event.media_types)
            descriptions.append(f"[附近图片 {added}，本回合第 {image_number} 张图片，仅作背景] {origin}")
        if added >= MAX_CONTEXT_IMAGES:
            break
    if added and event.message_type == MessageType.TEXT:
        event.message_type = MessageType.PHOTO
    if descriptions:
        # The caption maps exactly to the appended media order. Never attribute
        # a quiet member's picture to the author of the current trigger.
        block = "[周围图片来源：不是当前提问者新发的图片]\n" + "\n".join(descriptions[:12])
        event.channel_context = "\n\n".join(part for part in (event.channel_context, block) if part)
