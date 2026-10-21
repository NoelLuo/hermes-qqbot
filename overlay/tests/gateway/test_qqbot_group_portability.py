"""Generic naming, explicit bot identity, and opt-in privacy at real QQ ingress."""
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.group_identity import strip_bot_invocation


@pytest.mark.parametrize("text,ids,expected", [
    ("<@bot> /help", {"bot"}, "/help"),
    ("<@!bot> <@member> question", {"bot"}, "<@member> question"),
    ("<@bot> <@bot> question", {"bot"}, "<@bot> question"),
    ("<@member> question", {"bot"}, "<@member> question"),
    ("@Alice question", {"bot"}, "@Alice question"),
    ("<@unknown> question", set(), "<@unknown> question"),
    ("<@bot>", {"bot"}, ""),
])
def test_only_one_verified_bot_invocation_is_removed(text, ids, expected):
    assert strip_bot_invocation(text, ids) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected", [
    ("<@bot> /help", "/help"),
    ("<@bot> <@member> question", "<@member> question"),
    ("@Alice question", "@Alice question"),
    ("<@unknown> question", "<@unknown> question"),
])
async def test_group_ingress_preserves_member_mentions(text, expected):
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "group_policy": "open", "bot_member_openid": "bot"}))
    adapter.handle_message = AsyncMock()
    await adapter._handle_group_message({"group_openid": "test"}, "m1", text,
                                        {"member_openid": "member"}, "")
    event = adapter.handle_message.call_args.args[0]
    if expected.startswith("/"):
        assert event.is_command() and event.text == expected
    else:
        assert event.text.endswith(expected)
        assert event.source.user_id == "member"


def test_deployed_variant_preserves_canonical_owner_and_configurable_label():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "group_reply_gate": {"assistant_names": ["ExampleAssistant"]}}))
    # The published review variant has no local owner mapping. Deployment keeps
    # the existing mapping but never grants it through alternate author fields.
    adapter._home_channel = "owner"
    assert adapter._speaker_label("owner") == "管理员"
    assert adapter._speaker_label("member", {"username": "operator"}) == "群友-member"
    assert adapter._speaker_label("member", {"user_openid": "owner"}) == "群友-member"
    assert adapter._assistant_label == "ExampleAssistant"
    assert not adapter._group_reply_gate.settings.groups


@pytest.mark.asyncio
async def test_unconfigured_group_has_no_background_retention():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"group_policy": "open"}))
    adapter.handle_message = AsyncMock()
    await adapter._handle_plain_group_message({"group_openid": "test"}, "quiet", "hello",
                                              {"member_openid": "member"}, "")
    assert not adapter._group_context_cache
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_self_marked_event_uses_its_group_id_not_ready_id():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"group_policy": "open"}))
    adapter._bot_id = "different-ready-namespace"
    adapter.handle_message = AsyncMock()
    data = {"group_openid": "test", "mentions": [{"id": "group-bot", "is_you": True}]}
    await adapter._handle_group_message(data, "m1", "<@group-bot> /help",
                                        {"member_openid": "member"}, "")
    assert adapter.handle_message.call_args.args[0].text == "/help"
