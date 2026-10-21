"""Non-mention admission is opt-in, bounded and fails closed, without tools."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.qqbot.reply_gate import GroupReplyGate, ReplyGateSettings


def gate(**options):
    return GroupReplyGate({
        "group_reply_without_mention": ["test-group"],
        "group_reply_gate": {"decision_interval_seconds": 0, "cooldown_seconds": 0, **options},
    })


@pytest.mark.asyncio
async def test_gate_contract(monkeypatch):
    from agent import auxiliary_client

    call = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"reply": true}', tool_calls=None))]))
    monkeypatch.setattr(auxiliary_client, "async_call_llm", call)
    g = gate(assistant_names=["Hermes"], context_messages=2)
    assert not await g.should_reply("other-group", "Hermes在吗", [])
    call.assert_not_awaited()
    assert await g.should_reply("test-group", "Hermes在吗", ["one", "two", "three"])
    kw = call.call_args.kwargs
    assert kw["task"] == "qq_group_reply"
    assert "tools" not in kw
    import json
    payload = json.loads(kw["messages"][1]["content"])
    assert payload["recent_messages"] == ["two", "three"]
    assert payload["assistant_names"] == ["Hermes"]
    for content in ['{"reply": false}', '{"reply": "true"}', 'true',
                    'YES', '```json\n{"reply":true}\n```', '{}', '']:
        call.return_value.choices[0].message.content = content
        assert not await g.should_reply("test-group", "hello", [])
    call.return_value.choices[0].message.content = '{"reply":true}'
    call.return_value.choices[0].message.tool_calls = [object()]
    assert not await g.should_reply("test-group", "hello", [])
    call.side_effect = RuntimeError("offline")
    assert not await g.should_reply("test-group", "hello", [])


@pytest.mark.asyncio
async def test_burst_cooldown_cancellation_and_addressed_supersession(monkeypatch):
    g = gate(cooldown_seconds=30)
    entered, release = asyncio.Event(), asyncio.Event()

    async def judge(*args):
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(g, "_judge", judge)
    first = asyncio.create_task(g.should_reply("test-group", "question", []))
    await entered.wait()
    assert not await g.should_reply("test-group", "burst", [])
    g.note_addressed("test-group")
    release.set()
    assert not await first  # A direct @ arrived while the gate was thinking.
    assert not await g.should_reply("test-group", "follow-up during cooldown", [])

    g = gate()
    entered.clear()
    release.clear()
    monkeypatch.setattr(g, "_judge", judge)
    task = asyncio.create_task(g.should_reply("test-group", "question", []))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not g._states["test-group"].evaluating
    release.set()
    assert await g.should_reply("test-group", "next", [])

    assert not ReplyGateSettings.from_extra({"group_reply_without_mention": True}).groups
    assert not ReplyGateSettings.from_extra({"group_reply_without_mention": "*"}).groups
    settings = ReplyGateSettings.from_extra({"group_reply_gate": {
        "timeout_seconds": float("nan"), "context_messages": -10}})
    assert settings.timeout > 0 and settings.context_messages == 0
