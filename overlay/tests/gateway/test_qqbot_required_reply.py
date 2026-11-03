"""Trusted mixed mentions retain a reply obligation through the main-model call."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import gateway.run  # Bootstrap before the real-home guard activates.
from gateway.config import PlatformConfig
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.qqbot.adapter import QQAdapter


@pytest.mark.asyncio
async def test_mixed_mentions_have_trusted_reply_context():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={"group_policy": "open"}))
    adapter.handle_message = AsyncMock()
    data = {"group_openid": "group", "author": {"member_openid": "member"},
            "mentions": [{"id": "other"}, {"id": "self", "is_you": True}]}
    await adapter._handle_plain_group_message(
        data, "mixed", "<@other> <@self> 帮我查一下彩虹六号市场开了吗", data["author"], "")
    event = adapter.handle_message.await_args.args[0]
    assert event.reply_expected is True
    assert "同时" in event.channel_context
    assert "<@other>" in event.text


def test_main_model_recovery_is_bounded_and_keeps_prefix():
    from agent.required_reply import required_reply, recover_required_reply
    from gateway.response_filters import is_intentional_silence_response
    messages = [{"role": "user", "content": "old"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "current"}]
    prefix = [dict(row) for row in messages]
    with required_reply(is_intentional_silence_response):
        assert recover_required_reply("[SILENT]", messages)
        assert messages[:3] == prefix
        assert [r["role"] for r in messages[-2:]] == ["assistant", "user"]
        assert "回答" in messages[-1]["content"]
        assert not recover_required_reply("[SILENT]", messages)
    assert not recover_required_reply("[SILENT]", messages)
    with required_reply(None):
        assert not recover_required_reply("[SILENT]", messages)
    with required_reply(is_intentional_silence_response):
        assert not recover_required_reply("a normal answer", messages)


@pytest.mark.parametrize("expected", [True, False, None])
def test_gateway_binds_obligation_only_for_addressed_turn(expected):
    from agent.required_reply import recover_required_reply
    from gateway.turn_context import TurnContext
    ctx = TurnContext()
    ctx.reply_expected = expected
    ctx.source = SimpleNamespace(user_id="member", user_name=None, is_bot=False)
    worker = object.__new__(TurnRunner)
    worker._ctx = ctx
    worker._native_image_run_message = lambda: "question"
    observed = []
    def run(message, **kwargs):
        observed.append(recover_required_reply("[SILENT]", []))
        return {"final_response": "answer"}
    worker._run_conversation_with_approval(SimpleNamespace(run_conversation=run), [], None, None, None)
    assert observed == [expected is True]
    assert not recover_required_reply("[SILENT]", [])
