"""Bounded, turn-local recovery for a host-declared reply obligation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable

from agent.message_metadata import append_message

# The host supplies its control-token predicate; the agent need not know a platform.
_state: ContextVar[dict | None] = ContextVar("required_reply", default=None)


@contextmanager
def required_reply(silence_check: Callable[[str], bool] | None):
    token = _state.set({"check": silence_check, "retried": False} if silence_check else None)
    try:
        yield
    finally:
        _state.reset(token)


def recover_required_reply(response: str, messages: list[dict]) -> bool:
    state = _state.get()
    if state is None or state["retried"] or not state["check"](response):
        return False
    state["retried"] = True
    # Match existing empty-response scaffolding: an assistant row before the
    # recovery hint keeps roles legal; both disappear before the final flush.
    append_message(messages, {"role": "assistant", "content": response,
                              "_empty_recovery_synthetic": True})
    append_message(messages, {"role": "user", "content": (
        "[回复恢复] 当前回合由平台确认需要回答，本机器人也是被询问的对象。"
        "请继续处理原请求并给出实质回答；需要查证时使用工具，不要返回静默标记。"
        "已完成的工具操作不要重复执行；无法完成时如实说明限制。"),
        "_empty_recovery_synthetic": True})
    return True
