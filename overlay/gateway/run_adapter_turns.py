"""Optional adapter-owned final responses on in-band queued turns.

Retain the real inbound event: a reply anchor or ledger id alone is not provenance.
Adapters without this extension retain the existing queued text/media behavior.
"""


async def deliver_adapter_final(adapter, response, event, session_key, metadata=None, *,
                                text_already_delivered=False, deliver_media=True):
    hook = getattr(type(adapter), 'deliver_queued_response', None)
    if event is None or not callable(hook):
        return None
    return await adapter.deliver_queued_response(response, event, session_key, metadata,
        text_already_delivered=text_already_delivered, deliver_media=deliver_media)


async def prepare_adapter_followup(runner, event, session_id, session_key, history):
    if event is not None:
        notes = await runner._hmwa_adapter_turn_notes(event, session_id, session_key, history)
        if notes and session_key:
            existing = runner._consume_pending_turn_sidecar_notes(session_key)
            runner._set_pending_turn_sidecar_notes(session_key, [*existing, *notes])


async def finish_adapter_followup(runner, result, event, source, session_key, metadata):
    if (event is None or not isinstance(result, dict) or result.get('already_sent')
            or result.get('interrupted') or result.get('_notification_reply_muted')
            or 'queued_terminal_inbound_id' in result):
        return result
    response = result.get('final_response', '')
    if not isinstance(response, str) or not response:
        return result
    delivered = await deliver_adapter_final(runner._delivery_adapter_for(source), response,
        event, session_key, metadata, deliver_media=not result.get('failed'))
    if delivered is True:
        return {**result, 'already_sent': True, 'media_already_delivered': True}
    return result
