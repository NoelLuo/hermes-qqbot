"""Retain QQ's real-ingress expression stamp; never infer it from user text/metadata."""
from dataclasses import asdict

from .meme_expression import ExpressionContext, ExpressionDelivery
from .memes import MemeTurn, controller_for


_SOURCE_FIELDS = ('chat_id', 'user_id', 'chat_type', 'profile', 'thread_id', 'scope_id')
_TURN_FIELDS = ('message_id', 'chat_id', 'subject_id', 'chat_type', 'input_text')


def _identity(event):
    return [event.source.platform.value, *[getattr(event.source, name) for name in _SOURCE_FIELDS]]


def _matches(turn, event):
    return (isinstance(turn, MemeTurn) and event.source.platform.value == 'qqbot'
            and (turn.message_id, turn.chat_id, turn.subject_id, turn.chat_type)
            == (event.message_id, event.source.chat_id, event.source.user_id, event.source.chat_type))


def snapshot(adapter, event):
    turn = getattr(event, '_qq_meme_turn', None)
    if not _matches(turn, event) or event.internal or event.inherit_channel_pin:
        return None
    # The stamp contains no paths, artifact grants, cached catalog, selection or sent verdict.
    values = asdict(turn)
    return {'qq_meme_turn_v1': {'app_id': adapter._app_id, 'source': _identity(event),
                               'turn': {name: values[name] for name in _TURN_FIELDS}}}


def restore(adapter, event, state):
    data = state.get('qq_meme_turn_v1') if isinstance(state, dict) else None
    if (not isinstance(data, dict) or data.get('app_id') != adapter._app_id
            or data.get('source') != _identity(event)):
        return
    values = data.get('turn')
    if (not isinstance(values, dict) or set(values) != set(_TURN_FIELDS)
            or any(not isinstance(value, str) or len(value) > 2000 for value in values.values())):
        return
    turn = MemeTurn(**values)
    if not _matches(turn, event) or event.internal or event.inherit_channel_pin:
        return
    event._qq_meme_turn = turn
    # A recovered admission may be the first event for this chat after a restart.
    if event.source.chat_type in {'group', 'dm', 'c2c'}:
        adapter._chat_type_map[event.source.chat_id] = 'group' if event.source.chat_type == 'group' else 'c2c'


def bridge(adapter, executed, waiting):
    turn = getattr(executed, '_qq_meme_turn', None)
    if (not _matches(turn, executed) or not _matches(turn, waiting)
            or _identity(executed) != _identity(waiting)):
        return
    waiting._qq_meme_turn = turn
    context = getattr(executed, '_qq_expression_context', None)
    ctrl = controller_for(adapter)
    if (not isinstance(context, ExpressionContext) or ctrl.state is None
            or context.home != ctrl.home or context.transport != ctrl.state.transport
            or context.session_key != adapter._source_session_key(waiting.source)):
        return
    # These are live typed objects, not durable grants. Each final still does its usual
    # audience, home, request-policy, snapshot digest and native-receipt checks.
    waiting._qq_expression_context = context
    delivery = getattr(executed, '_qq_expression_delivery', None)
    if isinstance(delivery, ExpressionDelivery) and delivery.context is context:
        waiting._qq_expression_delivery = delivery
