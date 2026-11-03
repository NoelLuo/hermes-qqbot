"""Optional owner-only adapter state across native admission and its live delivery waiter.

The envelope is private owner data, not a client extension or a permission grant.
Adapters without these hooks retain their exact old payload and delivery behavior.
"""
import json

from hermes_state_runtime import RuntimeStoreError


def snapshot_adapter_turn(runner, event):
    adapter = runner._adapter_for_source(event.source)
    hook = getattr(type(adapter), 'snapshot_native_turn', None)
    if not callable(hook):
        return None
    state = hook(adapter, event)
    if state is None:
        return None
    if not isinstance(state, dict):
        raise RuntimeStoreError('invalid_params', field='adapter_turn')
    encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode('utf-8')) > 16 * 1024:
        raise RuntimeStoreError('invalid_params', field='adapter_turn')
    return json.loads(encoded)


def restore_adapter_turn(runner, event, state):
    if runner is None or state is None:
        return
    adapter = runner._adapter_for_source(event.source)
    hook = getattr(type(adapter), 'restore_native_turn', None)
    if callable(hook):
        hook(adapter, event, state)


def bridge_adapter_turn(runner, executed, waiting):
    if runner is None:
        return
    adapter = runner._adapter_for_source(executed.source)
    hook = getattr(type(adapter), 'bridge_native_turn', None)
    if callable(hook) and runner._adapter_for_source(waiting.source) is adapter:
        hook(adapter, executed, waiting)
