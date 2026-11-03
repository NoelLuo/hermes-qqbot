"""Long replies are opt-in previews; real adapter callbacks release the original text."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner  # bootstrap before the real-home guard
from plugins.platforms.qqbot.adapter import API_BASE, QQAdapter
from plugins.platforms.qqbot.long_reply import LongReplyPreview
from plugins.platforms.qqbot.receipt_adapter import receipt_journal


GROUP = "trial-group"
OPTIONS = {"enabled": True, "groups": [GROUP], "threshold": 800, "preview_chars": 180}
LONG_TEXT = "**结论：**这是应当保留的原文。\n\n" + "详细说明，不应在点击前发送。" * 100


@pytest.fixture
def bot():
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        "app_id": "long-reply-test", "group_policy": "open", "markdown_support": True,
        "long_reply_preview": dict(OPTIONS),
    }))
    adapter._auth_headers = AsyncMock(return_value={})
    adapter._ensure_connected = AsyncMock(return_value=True)
    adapter._chat_type_map[GROUP] = "group"
    adapter.set_authorization_check(lambda *_: True)
    return adapter


async def final_reply(bot, text=LONG_TEXT, group=GROUP, **kwargs):
    source = bot.build_source(chat_id=group, user_id="requester", chat_type="group")
    event = MessageEvent(source=source, message_id="question-id", text="question")
    key = bot._source_session_key(source)
    result, owner = await bot.send_final_ledgered(event, key, text, {}, reply_to=event.message_id, **kwargs)
    assert owner is bot
    return result, key


def raw_click(token, *, group=GROUP, operator="reader", interaction_id="click-id"):
    return {"id": interaction_id, "chat_type": 1, "type": 11, "scene": "group",
            "group_openid": group, "group_member_openid": operator,
            "data": {"type": 11, "resolved": {"button_id": "read-full", "button_data": token}}}


@pytest.fixture
def transport(bot):
    requests = []

    def request(req):
        requests.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"id": f"qq-receipt-{len(requests)}"})

    return requests, httpx.MockTransport(request)


@pytest.mark.asyncio
async def test_final_shows_preview_and_button_without_leaking_full_text(bot, transport):
    requests, wire = transport
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        result, key = await final_reply(bot)
    assert result.success and result.message_id
    assert len(requests) == 1
    method, path, body = requests[0]
    assert method == "POST" and path == f"/v2/groups/{GROUP}/messages"
    assert body["msg_type"] == 2
    preview = body["markdown"]["content"]
    assert "结论" in preview and len(preview) < 400
    assert bot.format_message(LONG_TEXT) not in preview
    button = body["keyboard"]["content"]["rows"][0]["buttons"][0]
    assert button["render_data"]["label"] == "查看全文"
    assert button["action"]["type"] == 1 and button["action"]["permission"]["type"] == 2
    assert button["action"]["click_limit"] == 0  # retries must not be disabled by the button itself
    assert button["action"]["data"].startswith("read-full:")
    rows = receipt_journal(bot, API_BASE).lookup_source(chat_id=GROUP, source_session_key=key,
                                                      source_message_id="question-id")
    assert [row.message_id for row in rows] == [result.message_id]


@pytest.mark.asyncio
async def test_ws_click_acks_then_sends_original_once_using_outer_event_id(bot, transport):
    requests, wire = transport
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        await final_reply(bot)
        token = requests[0][2]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]
        tasks = []
        bot._create_task = lambda coro: tasks.append(asyncio.create_task(coro))
        bot._dispatch_payload({"op": 0, "t": "INTERACTION_CREATE", "id": "outer-event-id",
                               "d": raw_click(token)})
        await asyncio.gather(*tasks)
        assert requests[1] == ("PUT", "/interactions/click-id", {"code": 0})
        assert requests[2][2]["markdown"]["content"] == bot.format_message(LONG_TEXT)
        assert requests[2][2]["event_id"] == "outer-event-id"
        assert "msg_id" not in requests[2][2] and "keyboard" not in requests[2][2]
        await bot._on_interaction(raw_click(token, interaction_id="second-click"))
    assert len([r for r in requests if r[0] == "POST"]) == 2
    assert requests[-1] == ("PUT", "/interactions/second-click", {"code": 0})


def button_token(requests):
    return requests[0][2]["keyboard"]["content"]["rows"][0]["buttons"][0]["action"]["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["short", "boundary", "other-group", "c2c", "interim", "ephemeral",
                                  "disabled", "no-groups", "plain-text"])
async def test_non_trial_and_control_paths_send_normal_text(bot, transport, mode):
    requests, wire = transport
    text, group, kwargs = LONG_TEXT, GROUP, {}
    if mode == "short":
        text = "普通短回复"
    elif mode == "boundary":
        text = "字" * 800
    elif mode == "other-group":
        group = "another-group"
        bot._chat_type_map[group] = "group"
    elif mode == "c2c":
        bot._chat_type_map[GROUP] = "c2c"
    elif mode == "ephemeral":
        kwargs["is_ephemeral_response"] = True
    elif mode in {"disabled", "no-groups"}:
        options = dict(OPTIONS)
        options.update({"enabled": False} if mode == "disabled" else {"groups": []})
        bot._long_reply_preview = LongReplyPreview({"long_reply_preview": options})
    elif mode == "plain-text":
        bot._markdown_support = False
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        if mode == "interim":
            result = await bot.send(group, text)
        else:
            result, _ = await final_reply(bot, text=text, group=group, **kwargs)
    assert result.success
    assert all("keyboard" not in body for _, _, body in requests)
    body = requests[0][2]
    assert body.get("content", body.get("markdown", {}).get("content")) == bot.format_message(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["other-group", "no-operator", "c2c", "not-button", "malformed",
                                  "group-denied", "sender-denied", "expired", "restarted"])
async def test_invalid_or_expired_click_does_not_disclose_text(bot, transport, mode):
    requests, wire = transport
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        await final_reply(bot)
        raw = raw_click(button_token(requests))
        if mode == "other-group":
            raw["group_openid"] = "other-group"
        elif mode == "no-operator":
            raw["group_member_openid"] = ""
        elif mode == "c2c":
            raw["chat_type"], raw["user_openid"] = 2, "reader"
        elif mode == "not-button":
            raw["data"]["type"] = 12
        elif mode == "malformed":
            raw["data"]["resolved"]["button_data"] = "read-full:../../private-file"
        elif mode == "group-denied":
            bot._is_group_allowed = lambda *_: False
        elif mode == "sender-denied":
            bot._is_sender_authorized = lambda *_: False
        elif mode == "expired":
            clock = bot._long_reply_preview._clock()
            bot._long_reply_preview._clock = lambda: clock + 3601
        elif mode == "restarted":
            bot._long_reply_preview = LongReplyPreview({"long_reply_preview": OPTIONS})
        await bot._on_interaction(raw)
    assert len(requests) == 2
    assert requests[-1] == ("PUT", "/interactions/click-id", {"code": 1})


@pytest.mark.asyncio
async def test_concurrent_readers_cannot_duplicate_full_reply(bot):
    requests, started, release = [], asyncio.Event(), asyncio.Event()

    async def request(req):
        body = json.loads(req.content)
        requests.append((req.method, req.url.path, body))
        if req.method == "POST" and "event_id" in body:
            started.set()
            await release.wait()
        return httpx.Response(200, json={"id": f"receipt-{len(requests)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        await final_reply(bot)
        token = button_token(requests)
        first = asyncio.create_task(bot._on_interaction(raw_click(token)))
        await asyncio.wait_for(started.wait(), timeout=5)
        await bot._on_interaction(raw_click(token, operator="second-reader", interaction_id="parallel-click"))
        release.set()
        await first
    assert [r[0] for r in requests] == ["POST", "PUT", "POST", "PUT"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["preview", "full", "missing-full-id"])
async def test_unknown_delivery_never_automatically_retries_or_falls_back(bot, stage):
    requests = []

    def request(req):
        body = json.loads(req.content)
        requests.append((req.method, req.url.path, body))
        if req.method == "POST" and ((stage == "preview" and "keyboard" in body)
                                     or (stage == "full" and "event_id" in body)):
            raise httpx.ReadTimeout("test lost response")
        if stage == "missing-full-id" and "event_id" in body:
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"id": f"receipt-{len(requests)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result, _ = await final_reply(bot)
        if stage == "preview":
            assert not result.success and result.retryable is False
        else:
            token = button_token(requests)
            await bot._on_interaction(raw_click(token))
            await bot._on_interaction(raw_click(token, interaction_id="again"))
            assert requests[-1][2] == {"code": 1}
    assert len([r for r in requests if r[0] == "POST"]) == (1 if stage == "preview" else 2)


@pytest.mark.asyncio
async def test_refused_keyboard_falls_back_without_losing_original(bot):
    requests = []

    def request(req):
        body = json.loads(req.content)
        requests.append(body)
        if "keyboard" in body:
            return httpx.Response(403, json={"code": 403})
        return httpx.Response(200, json={"id": "fallback-text-id"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result, _ = await final_reply(bot)
        assert result.success and result.message_id == "fallback-text-id"
    assert len(requests) == 2
    assert requests[1]["markdown"]["content"] == bot.format_message(LONG_TEXT)
    assert not bot._long_reply_preview._cards


@pytest.mark.asyncio
async def test_full_text_chunks_keep_receipts_and_resume_after_explicit_refusal(bot):
    requests, delivered, refused = [], [], False
    bot.MAX_MESSAGE_LENGTH = 500

    def request(req):
        nonlocal refused
        body = json.loads(req.content)
        requests.append((req.method, req.url.path, body))
        if req.method == "POST" and "event_id" in body:
            if body["msg_seq"] == 2 and not refused:
                refused = True
                return httpx.Response(429, json={"code": 429})
            delivered.append(body["markdown"]["content"])
        return httpx.Response(200, json={"id": f"receipt-{len(requests)}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result, key = await final_reply(bot)
        token = button_token(requests)
        await bot._on_interaction(raw_click(token))
        await bot._on_interaction(raw_click(token, interaction_id="retry-click"))
    expected = bot.truncate_message(bot.format_message(LONG_TEXT), bot.MAX_MESSAGE_LENGTH)
    assert delivered == expected
    posts = [body for method, _, body in requests if method == "POST" and "event_id" in body]
    assert [body["msg_seq"] for body in posts] == [1, 2, *range(2, len(expected) + 1)]
    assert all(body["event_id"] == "retry-click" for body in posts[2:])
    rows = receipt_journal(bot, API_BASE).lookup_source(chat_id=GROUP, source_session_key=key,
                                                      source_message_id="question-id")
    assert [row.delivery_status for row in rows] == ["known", "known", "error", *(["known"] * (len(expected) - 1))]
    assert rows[0].message_id == result.message_id
    assert len({row.logical_id for row in rows}) == 1


@pytest.mark.asyncio
async def test_cache_capacity_evicts_old_card_but_new_card_works(bot, transport):
    requests, wire = transport
    bot._long_reply_preview.MAX_CARDS = 1
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        await final_reply(bot)
        old_token = button_token(requests)
        await final_reply(bot)
        new_token = button_token(requests[1:])
        await bot._on_interaction(raw_click(old_token))
        await bot._on_interaction(raw_click(new_token, interaction_id="new-click"))
    assert requests[2][2] == {"code": 1}
    assert requests[-1][2]["markdown"]["content"] == bot.format_message(LONG_TEXT)


@pytest.mark.asyncio
async def test_low_cache_budget_sends_full_reply_instead_of_unusable_button(bot, transport):
    requests, wire = transport
    bot._long_reply_preview.MAX_CACHED_CHARS = 50
    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        result, _ = await final_reply(bot)
    assert result.success and len(requests) == 1
    assert "keyboard" not in requests[0][2]
    assert requests[0][2]["markdown"]["content"] == bot.format_message(LONG_TEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown"])
async def test_generic_recovery_cannot_release_full_text_without_a_click(bot, tmp_path, monkeypatch, outcome):
    from gateway import delivery_ledger
    ledger_path = tmp_path / "recovery.sqlite3"
    monkeypatch.setattr(delivery_ledger, "_db_path", lambda: ledger_path)
    monkeypatch.setattr(delivery_ledger, "ledger_enabled", lambda: True)
    def request(req):
        if outcome == "unknown" and "keyboard" in json.loads(req.content):
            raise httpx.ReadTimeout("test lost preview response")
        return httpx.Response(200, json={"id": "real-test-receipt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        bot._http_client = client
        result, _ = await final_reply(bot)
        assert result.success is (outcome == "success")
        assert not ledger_path.exists()
        assert delivery_ledger.sweep_recoverable(deliverable_platforms={"qqbot"}) == []
        # Ordinary replies still retain the host's usual durable recovery behavior.
        await final_reply(bot, text="普通短回复")
    with delivery_ledger._connect() as conn:
        rows = conn.execute("SELECT content,state FROM delivery_obligations").fetchall()
    assert rows == [("普通短回复", "delivered")]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["missing", "raises", "non-boolean"])
async def test_unknown_authorization_fails_closed_through_real_check(bot, transport, authorization):
    requests, wire = transport

    def broken_check(*_):
        raise RuntimeError("test authorization unavailable")

    async with httpx.AsyncClient(transport=wire) as client:
        bot._http_client = client
        await final_reply(bot)
        checks = {"missing": None, "raises": broken_check, "non-boolean": lambda *_: "allowed"}
        bot.set_authorization_check(checks[authorization])
        await bot._on_interaction(raw_click(button_token(requests)))
    assert len(requests) == 2
    assert requests[-1] == ("PUT", "/interactions/click-id", {"code": 1})
