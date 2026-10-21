"""QQ provenance for storage only; keep full identifiers out of message text."""
from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any


def sender_record(adapter: Any, user_id: str, message_id: str) -> dict:
    # Only the canonical routing sender can establish the role. Do not inspect
    # message text, names, or unrelated alternate fields in the author payload.
    return {"sender_id": user_id, "speaker": adapter._speaker_label(user_id),
            "message_id": message_id}


def strip_bot_invocation(content: str, bot_ids: Collection[str]) -> str:
    """Remove one verified bot invocation, not arbitrary members or @names."""
    text = content.strip()
    match = re.match(r"^<@!?([^\s<>]+)>\s*", text)
    if match and match.group(1) in bot_ids:
        return text[match.end():]
    return text
