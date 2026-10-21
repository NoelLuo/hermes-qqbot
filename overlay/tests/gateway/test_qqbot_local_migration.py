# OpenID-looking constants below are synthetic parser fixtures, not real QQ identities.
"""Local QQ extensions must survive bundled-platform migration."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.register import register


@pytest.fixture
def adapter():
    bot = QQAdapter(PlatformConfig(enabled=True, extra={"group_policy": "open", "bot_member_openid": "bot"}))
    bot._home_channel = "master"
    bot.handle_message = AsyncMock()
    return bot


def test_raw_qq_target_declared_explicit():
    ctx = SimpleNamespace(register_platform=Mock())
    register(ctx)
    parser = ctx.register_platform.call_args.kwargs["target_ref_syntax_fn"]
    target = "ABCDEF0123456789ABCDEF0123456789"
    assert parser(" " + target + " ") == (target, None)
    assert parser(" ") is None


def test_incremental_context_drain(adapter):
    adapter._record_group_turn("g", "群友-A", "one")
    assert "one" in "\n".join(adapter._render_group_context("g", consume=False))
    assert "one" in "\n".join(adapter._render_group_context("g"))
    assert adapter._render_group_context("g") == []
    adapter._record_group_turn("g", "群友-B", "two")
    assert "one" not in "\n".join(adapter._render_group_context("g"))


@pytest.mark.asyncio
@pytest.mark.parametrize("command,clears", [("/new", True), ("/reset", True), ("/help", False)])
async def test_reset_clears_only_current_group(adapter, command, clears):
    adapter._record_group_turn("g", "群友-A", "old")
    adapter._record_group_turn("other", "群友-B", "keep")
    await adapter._handle_group_message({"group_openid": "g"}, "id", "<@bot> " + command,
                                        {"member_openid": "member"}, "")
    assert adapter.handle_message.call_args.args[0].text == command
    assert bool(adapter._group_context_cache.get("g")) is not clears
    assert adapter._group_context_cache["other"]


@pytest.mark.asyncio
async def test_empty_mention_always_has_identity(adapter):
    await adapter._handle_group_message({"group_openid": "g"}, "id", "<@!bot>",
                                        {"member_openid": "member", "id": "master"}, "")
    text = adapter.handle_message.call_args.args[0].text
    assert "[群聊-群友-member]" in text and "空消息" in text
    assert "[群聊-管理员]" not in text


@pytest.mark.parametrize("text,expected", [("/中文 面板", "中文 面板"), ("/help", "/help"),
                                           ("/model model-id", "/model model-id")])
def test_panel_commands(adapter, text, expected):
    assert adapter._normalize_panel_command(text) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("trust_env", [True, False])
async def test_websocket_proxy_failure_really_falls_back_direct(adapter, monkeypatch, trust_env):
    calls, sessions = [], []
    monkeypatch.setattr("plugins.platforms.qqbot.adapter.gateway_trust_env", lambda: trust_env)
    for key in ("WSS_PROXY", "wss_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WSS_PROXY", "http://127.0.0.1:9999")
    class Session:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            self.closed = False
            sessions.append(self)
        async def close(self):
            self.closed = True
        async def ws_connect(self, url, **kwargs):
            calls.append(kwargs["proxy"])
            if kwargs["proxy"]:
                raise OSError("proxy unavailable")
            return SimpleNamespace(closed=False)
    monkeypatch.setattr("plugins.platforms.qqbot.adapter.aiohttp.ClientSession", Session)
    await adapter._open_ws("wss://example.invalid")
    assert calls == (["http://127.0.0.1:9999", None] if trust_env else [None])
    assert not sessions[-1].closed
    if trust_env:
        assert sessions[0].closed


@pytest.mark.asyncio
async def test_replay_seam_uses_sibling_module(adapter, monkeypatch):
    seam = AsyncMock(return_value=[{"role": "user", "content": "history"}])
    monkeypatch.setattr("plugins.platforms.qqbot.group_history.prepare_turn_history", seam)
    store, event = object(), object()
    result = await adapter.prepare_group_turn_history(store, "sid", event)
    seam.assert_awaited_once_with(store, "sid", event)
    assert result[0]["content"] == "history"


@pytest.mark.asyncio
async def test_send_strips_echo_and_records_only_answer(adapter):
    adapter._ensure_connected = AsyncMock(return_value=True)
    from gateway.platforms.base import SendResult
    adapter._send_chunk = AsyncMock(return_value=SendResult(success=True))
    adapter._group_context_cache["g"] = []
    content = "[群聊-管理员] 💭 **推理：**\n```\nprivate thought\n```\n\nanswer"
    await adapter.send("g", content)
    assert "[群聊-管理员]" not in adapter._send_chunk.call_args.args[1]
    assert adapter._group_context_cache["g"] == [("Hermes", "answer")]
