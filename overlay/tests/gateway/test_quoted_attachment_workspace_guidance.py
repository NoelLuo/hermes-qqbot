"""Import guidance distinguishes persisted work from unauthorised quoted media."""
from types import SimpleNamespace

import pytest

from agent.request_security import RequestSecurityPolicy
from agent.request_workspace_guidance import workspace_request_guidance
from gateway.request_security import attachment_workspace_note


def _fixture(*, platform='qqbot', private=False, media=True):
    extra = {'role_policy': {'enabled': True}, 'allow_admin_from': ['owner']}
    runner = SimpleNamespace(config=SimpleNamespace(platforms={'qqbot': SimpleNamespace(extra=extra)}))
    source = SimpleNamespace(platform=platform, user_id='owner', chat_id='room',
                             chat_type='dm' if private else 'group')
    event = SimpleNamespace(source=source, media_urls=['unverified-quote.pdf'] if media else [])
    return runner, event


def test_unverified_quote_note_requires_workspace_lookup_without_grant(tmp_path):
    runner, event = _fixture()
    path = tmp_path / 'same-name.pdf'
    path.write_bytes(b'%PDF-fixture')
    event.media_urls = [str(path)]
    note = attachment_workspace_note(runner, event, [])
    assert '先 list 本人工作区' in note
    assert '没有已核验' in note and '不要' in note
    assert str(path) not in note
    assert not event.source._request_security_policy._artifacts.allows(str(path))


def test_verified_note_names_only_current_final_paths(tmp_path):
    runner, event = _fixture()
    path = tmp_path / 'verified.pdf'
    path.write_bytes(b'%PDF-fixture')
    note = attachment_workspace_note(runner, event, [str(path)])
    assert str(path) in note and '列表外' in note
    assert 'unverified-quote.pdf' not in note
    assert not hasattr(event.source, '_request_security_policy')


@pytest.mark.parametrize('platform,private,media', [
    ('qqbot', True, True), ('qqbot', False, False), ('telegram', False, True),
])
def test_no_visitor_quote_warning_for_unrelated_context(platform, private, media):
    runner, event = _fixture(platform=platform, private=private, media=media)
    assert attachment_workspace_note(runner, event, []) == ''


def test_workspace_advice_reuses_persistent_copy_not_stale_ingress():
    p = RequestSecurityPolicy('visitor', 'qqbot', 'room', allowed_tools=frozenset({'guest_workspace'}))
    text = '\n'.join(workspace_request_guidance(SimpleNamespace(
        _request_security_policy=p, valid_tool_names={'guest_workspace'})))
    assert '先 list 本人工作区' in text
    assert '不必重新 import' in text
    assert '不能按文件名、相同哈希或缓存新鲜度补授权' in text
