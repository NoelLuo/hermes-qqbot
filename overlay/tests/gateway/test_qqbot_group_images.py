"""Quiet image intake through real caching, SQLite, restart and native image assembly."""
import asyncio
import base64
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.image_routing import build_native_content_parts
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.event import MessageEvent
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.group_history import prepare_turn_history
from plugins.platforms.qqbot.group_images import IMAGE_METADATA_KEY, attach_nearby_images
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionStore

NOW = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)


def png(color=20):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0] + [color, 0, 0] * 2) * 2)) + chunk(b"IEND", b""))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig(group_sessions_per_user=False))
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "group_policy": "open", "group_reply_without_mention": ["test"],
    }))
    adapter.set_session_store(store)
    adapter.set_authorization_check(lambda user, kind, group: user != "blocked")
    adapter._group_reply_gate._judge = AsyncMock(return_value=False)
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path == "/99.png":
            return httpx.Response(404)
        return httpx.Response(200, content=png(int(request.url.path.strip("/").split(".")[0])),
                              headers={"content-type": "image/png"})

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    yield adapter, store, requests
    store._db.close()


def attachment(color):
    # Public numeric host passes the real SSRF validator without DNS. MockTransport
    # is the only network seam; no request is sent to this address.
    return {"content_type": "image/png", "url": f"https://1.1.1.1/{color}.png"}


async def receive(adapter, mid="photo", colors=(20,), when=NOW, user="alice"):
    data = {"group_openid": "test", "attachments": [attachment(c) for c in colors]}
    await adapter._handle_plain_group_message(data, mid, "", {"member_openid": user}, when.isoformat())


def source(adapter, user="bob"):
    return adapter.build_source(chat_id="test", user_id=user, chat_type="group")


def event(adapter, when=NOW + timedelta(seconds=30)):
    return MessageEvent(source=source(adapter), text="这一步为什么成立？", message_id="trigger", timestamp=when)


@pytest.mark.asyncio
async def test_cached_photo_survives_restart_and_enters_native_turn_once(rig):
    adapter, store, requests = rig
    await receive(adapter)
    adapter._group_reply_gate._judge.assert_not_awaited()
    assert len(requests) == 1
    sid = store.get_or_create_session(source(adapter)).session_id
    rows = store.load_transcript(sid, repair_alternation=False)
    record = rows[0]["display_metadata"][IMAGE_METADATA_KEY][0]
    assert record["status"] == "cached"
    assert Path(record["path"]).read_bytes() == png()

    restarted = SessionStore(store.sessions_dir, store.config)
    try:
        assert restarted.get_or_create_session(source(adapter)).session_id == sid
        captured = []

        async def capture(current):
            clean = await prepare_turn_history(AsyncSessionStore(restarted), sid, current)
            captured.append((current, clean))

        adapter.handle_message = capture
        await adapter._handle_group_message(
            {"group_openid": "test"}, "trigger", "<@bot> 这一步为什么成立？",
            {"member_openid": "bob"}, (NOW + timedelta(seconds=30)).isoformat())
        current, clean = captured[0]
        assert not clean and len(current.media_urls) == 1
        assert "群友-alice" in current.channel_context and "消息 photo" in current.channel_context
        assert "不是当前提问者" in current.channel_context
        assert current.source.user_id == "bob"
        images, *_ = GatewayRunner._classify_inbound_media(current, False)
        parts, skipped = build_native_content_parts(current.channel_context + current.text, images)
        assert not skipped
        image_parts = [p for p in parts if p.get("type") == "image_url"]
        assert len(image_parts) == 1
        encoded = image_parts[0]["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(encoded) == png()
        # The subsequent addressed input consumes the nearby-media window, just
        # like text. Old observed rows stay on disk, not repeatedly reattached.
        restarted.append_to_transcript(sid, {"role": "user", "content": "正式问题", "message_id": "trigger"})
        next_event = event(adapter)
        await prepare_turn_history(AsyncSessionStore(restarted), sid, next_event)
        assert not next_event.media_urls
        # An explicit reply can intentionally revisit that consumed image.
        next_event.reply_to_message_id = "photo"
        await attach_nearby_images(rows, next_event)
        assert len(next_event.media_urls) == 1
    finally:
        restarted._db.close()
        await adapter._http_client.aclose()


@pytest.mark.asyncio
async def test_image_selection_cap_dedup_age_auth_and_unavailable(rig, tmp_path):
    adapter, store, requests = rig
    await receive(adapter, user="blocked")
    assert not requests and not store._entries
    await receive(adapter, "old", (10,), NOW - timedelta(minutes=11))
    await receive(adapter, "first", (20, 30, 40))
    await receive(adapter, "second", (20, 50, 60))  # A duplicate cached under another filename.
    sid = store.get_or_create_session(source(adapter)).session_id
    rows = store.load_transcript(sid, repair_alternation=False)
    current = event(adapter)
    await attach_nearby_images(rows, current)
    assert len(current.media_urls) == 4
    contents = [Path(p).read_bytes() for p in current.media_urls]
    assert len(set(contents)) == 4 and png(10) not in contents

    # Existing current/quoted image wins over a byte-identical nearby picture.
    duplicate = event(adapter)
    duplicate.media_urls = [rows[-1]["display_metadata"][IMAGE_METADATA_KEY][0]["path"]]
    duplicate.media_types = ["image/png"]
    await attach_nearby_images(rows[-1:], duplicate)
    assert sum(Path(p).read_bytes() == png(20) for p in duplicate.media_urls) == 1

    # Missing/modified files are a visible limitation, never invented image content.
    last_row = rows[-1]
    for record in last_row["display_metadata"][IMAGE_METADATA_KEY]:
        Path(record["path"]).unlink()
    missing = event(adapter)
    await attach_nearby_images([last_row], missing)
    assert not missing.media_urls and "图片不可用" in missing.channel_context
    forged = dict(last_row)
    forged["display_metadata"] = {IMAGE_METADATA_KEY: [{
        "path": str(tmp_path / "outside-cache.png"), "mime": "image/png", "status": "cached", "sha256": "fake"}]}
    unsafe = event(adapter)
    await attach_nearby_images([forged], unsafe)
    assert not unsafe.media_urls and "图片不可用" in unsafe.channel_context
    # Enforce the background cap while streaming, before bytes reach the cache.
    assert await adapter._download_and_cache(attachment(20)["url"], "image/png", max_bytes=8) is None
    await receive(adapter, "failed-download", (99,))
    failed_row = store.load_transcript(sid, repair_alternation=False)[-1]
    assert failed_row["display_metadata"][IMAGE_METADATA_KEY][0]["status"] == "unavailable"
    unavailable = event(adapter)
    await attach_nearby_images([failed_row], unavailable)
    assert not unavailable.media_urls and "图片不可用" in unavailable.channel_context
    adapter._group_reply_gate._judge.assert_not_awaited()
    await adapter._http_client.aclose()


@pytest.mark.asyncio
async def test_concurrent_photo_is_committed_before_following_turn(rig):
    adapter, store, _ = rig
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    await adapter._http_client.aclose()
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    photo_task = asyncio.create_task(receive(adapter))
    await entered.wait()
    sid = store.get_or_create_session(source(adapter)).session_id
    current = event(adapter)
    turn_task = asyncio.create_task(prepare_turn_history(AsyncSessionStore(store), sid, current))
    release.set()
    await asyncio.wait_for(asyncio.gather(photo_task, turn_task), 5)
    assert len(current.media_urls) == 1
    assert not adapter._group_images._pending
    adapter._group_reply_gate._judge.assert_not_awaited()
    await adapter._http_client.aclose()
