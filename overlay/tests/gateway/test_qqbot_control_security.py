"""QQ opt-in control gates reject before mutating busy sessions or pending approvals."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.request_security import policy_from_extra
from gateway.run import GatewayRunner

from gateway.session import SessionSource
from plugins.platforms.qqbot.adapter import QQAdapter
from plugins.platforms.qqbot.keyboards import ApprovalRequest, InteractionEvent
from tools import approval, slash_confirm


EXTRA = {"role_policy": {"enabled": True}, "group_allow_admin_from": ["admin"],
         "allow_admin_from": ["admin"]}
KEY = "agent:main:qqbot:group:room"


def event(text="hello", user="guest", kind=MessageType.TEXT):
    return MessageEvent(text=text, message_type=kind, source=SessionSource(
        platform=Platform.QQBOT, chat_id="room", chat_type="group", user_id=user))


def adapter(enabled=True):
    obj = QQAdapter.__new__(QQAdapter)
    obj.config = SimpleNamespace(extra=EXTRA if enabled else {})
    obj.platform = Platform.QQBOT

    obj._chat_type_map = {"room": "group"}
    obj._last_msg_id = {}
    obj.send = AsyncMock(return_value=SendResult(success=True))
    return obj


def runner():
    obj = GatewayRunner.__new__(GatewayRunner)
    obj.config = SimpleNamespace(platforms={Platform.QQBOT: SimpleNamespace(extra=EXTRA)})
    obj._peek_session_state = Mock(return_value=SimpleNamespace(turn=SimpleNamespace(event=event(user="admin"))))
    return obj


@pytest.mark.parametrize("command", ["stop", "new", "reset", "approve", "deny", "yolo", "update",
    "model", "restart", "memory", "busy", "steer", "queue", "unknown"])
def test_guest_command_gate_is_closed_even_with_extra_allowlist(command):
    extra = dict(EXTRA, group_user_allowed_commands=[command])
    policy = policy_from_extra(extra, event().source)
    assert not policy.allows_command(command)
    assert policy.allows_command("help") and policy.allows_command("whoami")
    assert policy_from_extra(extra, event(user="admin").source).allows_command(command)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["stop", "new", "reset"])
async def test_refused_busy_reset_does_not_cancel_drain_or_replace_guard(command):
    obj = adapter()
    guard = object()
    obj._active_sessions = {KEY: guard}
    obj._pending_messages = {KEY: event("existing", "admin")}
    obj.cancel_session_processing = AsyncMock()
    obj._dispatch_inline_reply = AsyncMock()
    obj._drain_pending_after_session_command = AsyncMock()
    obj._discard_text_debounce = Mock()
    await obj._handle_message_while_active(event("/" + command), KEY)
    assert obj._active_sessions[KEY] is guard
    assert obj._pending_messages[KEY].text == "existing"
    obj.cancel_session_processing.assert_not_awaited()
    obj._dispatch_inline_reply.assert_not_awaited()
    obj._drain_pending_after_session_command.assert_not_awaited()
    obj._discard_text_debounce.assert_not_called()


@pytest.mark.asyncio
async def test_guest_busy_text_does_not_clarify_queue_or_steer():
    obj = adapter()
    obj._busy_session_handler = AsyncMock()
    obj._pending_messages = {}
    await obj._handle_message_while_active(event("yes"), KEY)
    assert not obj._pending_messages
    obj._busy_session_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["approve", "deny"])
async def test_direct_approval_handler_rejects_before_pending_access(monkeypatch, verb):
    obj = runner()
    obj._blocking_approval_or_stale = Mock(side_effect=AssertionError("must not inspect pending"))
    resolve = Mock()
    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    assert await getattr(obj, f"_handle_{verb}_command")(event("/" + verb))
    resolve.assert_not_called()
    obj._blocking_approval_or_stale.assert_not_called()


@pytest.mark.asyncio
async def test_natural_slash_confirmation_does_not_resolve_for_guest(monkeypatch):
    obj = runner()
    monkeypatch.setattr(slash_confirm, "get_pending", lambda key: {"confirm_id": "confirmation"})
    monkeypatch.setattr(approval, "has_blocking_approval", lambda key: False)
    resolve = AsyncMock()
    monkeypatch.setattr(slash_confirm, "resolve", resolve)
    assert await obj._hm_slash_confirm_reply(event("yes"), KEY)
    resolve.assert_not_awaited()


def test_busy_actor_isolation_keeps_sender_and_legacy_semantics():
    obj = runner()
    assert obj._request_busy_isolation(event(), KEY) == "reject"
    assert obj._request_busy_isolation(event(user="admin"), KEY) is None
    obj._peek_session_state.return_value.turn.event = event(user="other")
    assert obj._request_busy_isolation(event(user="admin"), KEY) == "queue"
    obj.config.platforms[Platform.QQBOT].extra = {}
    assert obj._request_busy_isolation(event(), KEY) is None


@pytest.mark.asyncio
async def test_cross_sender_media_never_merges_into_admin_event():
    obj = runner()
    transport = adapter()
    original = event("owner", "admin", MessageType.PHOTO)
    transport._pending_messages = {KEY: original}
    obj._delivery_adapter_for = lambda source: transport
    obj._queue_depth = lambda *args, **kwargs: 1
    obj._enqueue_fifo = Mock()
    incoming = event("guest", kind=MessageType.PHOTO)
    obj._queue_or_replace_pending_event(KEY, incoming)
    assert original.text == "owner"
    assert original.source.user_id == "admin"
    obj._enqueue_fifo.assert_called_once_with(KEY, incoming, transport)


@pytest.mark.asyncio
@pytest.mark.parametrize("user,chat,bound,expected", [
    ("guest", "room", True, False), ("admin", "other", True, False),
    ("admin", "room", False, False), ("admin", "room", True, True)])
async def test_bound_group_buttons_require_admin_room_and_request(monkeypatch, user, chat, bound, expected):
    obj = adapter()
    prefix = "approve-request:req-old:" if bound else "approve:"
    click = InteractionEvent(scene="group", group_openid=chat, group_member_openid=user,
        button_data=f"{prefix}{KEY}:allow-once")
    resolve = Mock(return_value=0)
    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    await obj._default_interaction_dispatch(click)
    if expected:
        resolve.assert_called_once_with(KEY, "once", request_id="req-old")
    else:
        resolve.assert_not_called()


@pytest.mark.asyncio
async def test_generated_card_carries_request_identity(monkeypatch):
    obj = adapter()
    obj.send_with_keyboard = AsyncMock(return_value=SendResult(success=True))
    await obj.send_approval_request("room", ApprovalRequest(KEY, "test", request_id="request-one"))
    keyboard = obj.send_with_keyboard.call_args.args[2]
    assert all(b.action.data.startswith("approve-request:request-one:")
               for b in keyboard.content.rows[0].buttons)


@pytest.mark.asyncio
async def test_update_card_replay_does_not_answer_next_request():
    obj = adapter()
    obj.send_with_keyboard = AsyncMock(return_value=SendResult(success=True))
    obj._write_update_response = Mock()
    await obj.send_update_prompt("room", "first")
    old = obj.send_with_keyboard.call_args.args[2].content.rows[0].buttons[0].action.data
    await obj.send_update_prompt("room", "second")
    new = obj.send_with_keyboard.call_args.args[2].content.rows[0].buttons[0].action.data
    click = InteractionEvent(scene="group", group_openid="room", group_member_openid="admin", button_data=old)
    await obj._default_interaction_dispatch(click)
    obj._write_update_response.assert_not_called()
    click.button_data = new
    await obj._default_interaction_dispatch(click)
    obj._write_update_response.assert_called_once_with("y", "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("verb,args", [("approve", ""), ("deny", ""), ("approve", "all"),
    ("approve", "all request:A"), ("approve", "request:A request:B")])
async def test_admin_unbound_approval_does_not_inspect_or_resolve(monkeypatch, verb, args):
    obj = runner()
    obj._blocking_approval_or_stale = Mock(side_effect=AssertionError("unbound pending access"))
    resolve = Mock()
    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    assert await getattr(obj, f"_handle_{verb}_command")(event(f"/{verb} {args}", "admin"))
    resolve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["approve", "deny"])
async def test_admin_exact_request_target_reaches_atomic_resolver(monkeypatch, verb):
    obj = runner()
    obj._blocking_approval_or_stale = Mock(return_value=(KEY, None))
    resolve = Mock(return_value=0)
    monkeypatch.setattr(approval, "resolve_gateway_approval", resolve)
    await getattr(obj, f"_handle_{verb}_command")(event(f"/{verb} request:Req-Old", "admin"))
    assert resolve.call_args.kwargs["request_id"] == "Req-Old"
    assert "resolve_all" not in resolve.call_args.kwargs


@pytest.mark.asyncio
async def test_exec_approval_uses_original_request_id_not_current_pending(monkeypatch):
    obj = adapter()
    obj.send_approval_request = AsyncMock(return_value=SendResult(success=True))
    monkeypatch.setattr(approval, "list_gateway_approvals", Mock(side_effect=AssertionError("no queue snapshots")))
    await obj.send_exec_approval(chat_id="room", command="example", session_key=KEY, request_id="Request-A")
    assert obj.send_approval_request.call_args.args[1].request_id == "Request-A"
    obj.send_approval_request.reset_mock()
    result = await obj.send_exec_approval(chat_id="room", command="example", session_key=KEY)
    assert not result.success
    obj.send_approval_request.assert_not_awaited()