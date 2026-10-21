"""Real SQLite intake/replay tests: quiet QQ chat never becomes an agent request."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.group_history import OBSERVED_DISPLAY_KIND, prepare_replay, prepare_turn_history
from gateway.run import GatewayRunner
from gateway.session import AsyncSessionStore, SessionStore


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig(group_sessions_per_user=False))
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "group_policy": "open", "group_reply_without_mention": ["test-group"],
        "group_reply_gate": {"cooldown_seconds": 0, "decision_interval_seconds": 0},
    }))
    adapter.set_session_store(store)
    adapter.set_authorization_check(lambda user, kind, chat: user != "blocked")
    adapter._home_channel = "master"
    adapter.send_typing = AsyncMock()
    yield adapter, store
    store._db.close()


async def incoming(adapter, mid, text, user="member", group="test-group", **data):
    payload = {"id": mid, "group_openid": group, "author": {"member_openid": user}, **data}
    await adapter._handle_plain_group_message(payload, mid, text, payload["author"], "2026-10-16T05:00:00Z")


@pytest.mark.asyncio
async def test_durable_quiet_busy_replay_and_single_trigger(rig, monkeypatch):
    adapter, store = rig
    judge = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter._group_reply_gate, "_judge", judge)
    captured = []
    done = asyncio.Event()

    async def handler(event):
        captured.append(event)
        entry = store.get_or_create_session(event.source)
        # The real runner reloads raw rows, then separates background BEFORE
        # replay repair. Use those same imports and the real persistent store.
        history = await prepare_turn_history(AsyncSessionStore(store), entry.session_id, event)
        context = event.channel_context
        captured.append((history, context))
        runner = object.__new__(GatewayRunner)
        runner.config = store.config
        event.channel_context = context
        rendered = runner._prefix_inbound_sender_context(event, event.source, event.text)
        store.append_to_transcript(entry.session_id, {
            "role": "user", "content": rendered,
            "message_id": event.message_id})
        done.set()
        return None

    adapter.set_message_handler(handler)
    await incoming(adapter, "quiet-1", "今天中午吃面")
    assert not captured
    source = adapter.build_source(chat_id="test-group", user_id="member", chat_type="group")
    sid = store.get_or_create_session(source).session_id
    rows = store.load_transcript(sid, repair_alternation=False)
    assert len(rows) == 1 and rows[0]["observed"] and rows[0]["message_id"] == "quiet-1"
    assert rows[0]["display_kind"] == OBSERVED_DISPLAY_KIND
    assert "[群聊-群友-member]" in rows[0]["content"]
    await incoming(adapter, "quiet-1", "今天中午吃面")
    assert len(store.load_transcript(sid, repair_alternation=False)) == 1

    # Quiet chatter in the middle of a REAL durable tool sequence must neither
    # interrupt the active turn nor cause repair to prune that call/result pair.
    store.append_to_transcript(sid, {"role": "user", "content": "已开始的任务"})
    store.append_to_transcript(sid, {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]})
    key = adapter._source_session_key(source)
    adapter._active_sessions[key] = object()
    await incoming(adapter, "quiet-2", "其他人随便聊天")
    adapter._active_sessions.pop(key)
    assert judge.await_count == 1  # Busy messages are record-only, without a gate call.
    store.append_to_transcript(sid, {"role": "tool", "content": "done", "tool_call_id": "call-1"})
    store.append_to_transcript(sid, {"role": "assistant", "content": "任务完成"})
    clean, pending = prepare_replay(store.load_transcript(sid, repair_alternation=False))
    assert any(r.get("tool_call_id") == "call-1" for r in clean)
    assert any(r.get("tool_calls") for r in clean)
    assert "其他人随便聊天" in pending and "今天中午吃面" not in pending
    assert not any(r.get("observed") for r in clean)

    judge.return_value = True
    await incoming(adapter, "trigger", "Hermes，解释一下这个结果")
    await asyncio.wait_for(done.wait(), 5)
    event = captured[0]
    assert event.allow_gateway_control is False and event.reply_expected is False
    assert event.source.user_id == "member"
    assert "[群聊-群友-member]" in event.text
    rows = store.load_transcript(sid, repair_alternation=False)
    assert sum(r.get("message_id") == "trigger" for r in rows) == 1
    assert not any(r.get("observed") for r in rows if r.get("message_id") == "trigger")
    _, pending = prepare_replay(rows)
    assert pending is None  # Consumed context is not replayed on every turn.


@pytest.mark.asyncio
async def test_opt_in_auth_commands_and_restart(rig, monkeypatch):
    adapter, store = rig
    judge = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter._group_reply_gate, "_judge", judge)
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "handle_message", dispatch)
    await incoming(adapter, "blocked", "Hermes在吗", user="blocked")
    await incoming(adapter, "not-enabled", "Hermes在吗", group="other-group")
    judge.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert len(store._entries) == 0

    await incoming(adapter, "slash", "/approve")
    judge.assert_not_awaited()
    dispatch.assert_not_awaited()
    source = adapter.build_source(chat_id="test-group", user_id="member", chat_type="group")
    sid = store.get_or_create_session(source).session_id
    # A fresh SessionStore, without the legacy in-memory window, still sees it.
    restarted = SessionStore(store.sessions_dir, store.config)
    try:
        recovered = restarted.get_or_create_session(source)
        assert recovered.session_id == sid
        rows = restarted.load_transcript(sid, repair_alternation=False)
        clean, pending = prepare_replay(rows)
        assert not clean and "/approve" in pending
    finally:
        restarted._db.close()

    # @ handling remains unconditional (including commands) and supersedes gates.
    adapter._is_at_bot = lambda d: True
    await incoming(adapter, "at-help", "/help")
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0].text == "/help"
    assert dispatch.call_args.args[0].allow_gateway_control is True
