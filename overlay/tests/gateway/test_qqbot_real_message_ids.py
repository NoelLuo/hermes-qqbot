"""Remote IDs are real response values, and empty DELETE acknowledgements are valid."""
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import PlatformConfig
from plugins.platforms.qqbot.adapter import QQAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected', [
    ({'id': 'opaque.REAL/message:1'}, 'opaque.REAL/message:1'),
    ({}, None), ({'id': None}, None), ({'id': 123}, None), ({'id': ''}, None),
])
async def test_post_message_never_invents_a_remote_id(response, expected):
    adapter = QQAdapter(PlatformConfig(enabled=True))
    adapter._api_request = AsyncMock(return_value=response)
    result = await adapter._post_message('/v2/groups/test/messages', {'content': 'hello'})
    assert result.success and result.message_id == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,succeeds', [
    (200, b'', True), (200, b'{}', True),
    (200, b'{"code":40062003,"message":"private error detail"}', False),
    (403, b'{"code":40062003,"message":"private error detail"}', False),
])
async def test_delete_ack_contract_uses_real_http_client(status, body, succeeds):
    requests = []

    async def request(req):
        requests.append(req)
        return httpx.Response(status, content=body)

    adapter = QQAdapter(PlatformConfig(enabled=True))
    adapter._auth_headers = AsyncMock(return_value={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        adapter._http_client = client
        if succeeds:
            assert await adapter._api_request('DELETE', '/v2/groups/test/messages/real-id') == {}
        else:
            with pytest.raises(RuntimeError) as exc:
                await adapter._api_request('DELETE', '/v2/groups/test/messages/real-id')
            assert 'private error detail' not in str(exc.value)
    assert len(requests) == 1 and requests[0].method == 'DELETE'
