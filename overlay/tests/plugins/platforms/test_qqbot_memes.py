"""Real QQ final-text/media/receipt boundary; network transport is replaced, not policy."""
import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.request_security import policy_for_adapter
from gateway.run import GatewayRunner  # bootstrap before the real-home guard
from plugins.platforms.qqbot.adapter import API_BASE, QQAdapter
from plugins.platforms.qqbot.meme_selector import MemeSelection
from plugins.platforms.qqbot.memes import MemeSettings, capture_turn, controller_for
from plugins.platforms.qqbot.receipt_adapter import receipt_journal

GROUP = 'meme-test-group'


@pytest.fixture
def bot(tmp_path):
    root = tmp_path / 'meme-root'
    root.mkdir()
    image = root / '肯定.gif'
    Image.new('RGB', (8, 8), 'pink').save(image, format='GIF')
    item = {'id': 'yes', 'relative_path': '肯定.gif', 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
            'description': 'affirmation', 'use_when': ['agree'], 'avoid_when': ['serious'],
            'audiences': ['group', 'dm'], 'enabled': True, 'reviewed': True}
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'schema_version': 1, 'items': [item]}), encoding='utf-8')
    adapter = QQAdapter(PlatformConfig(enabled=True, extra={
        'app_id': 'meme-test', 'group_policy': 'open', 'markdown_support': True,
        'role_policy': {'enabled': True}, 'group_allow_admin_from': ['owner'], 'allow_admin_from': ['owner'],
        'memes': {'enabled': True, 'root_dir': str(root), 'catalog_path': str(catalog),
                  'groups': [GROUP], 'dm_users': ['owner'], 'intimacy_users': ['owner'],
                  'probability': 1, 'cooldown_seconds': 180}}))
    adapter._ensure_connected = AsyncMock(return_value=True)
    adapter._auth_headers = AsyncMock(return_value={})
    adapter._chat_type_map[GROUP] = 'group'
    adapter._upload_local_file = AsyncMock(return_value={'file_info': 'synthetic-upload'})
    controller = controller_for(adapter)
    controller.draw = lambda: 0
    controller.clock = lambda: 1000
    controller.selector = AsyncMock(return_value=MemeSelection('yes', 'natural'))
    return adapter


def make_event(bot, *, chat=GROUP, user='visitor', kind='group', identifier='question-id'):
    event = MessageEvent(source=bot.build_source(chat_id=chat, user_id=user, chat_type=kind),
                         text='that was fun', message_id=identifier,
                         raw_message={'id': identifier, 'content': 'that was fun'})
    capture_turn(event)
    return event


async def final(bot, event, *, text='yes, that was fun', ephemeral=False):
    key = bot._source_session_key(event.source)
    extracted = await bot._extract_response_content(text, event, key, is_ephemeral_response=ephemeral)
    return await bot.send_final_ledgered(event, key, extracted.text_content, {},
        reply_to=event.message_id, is_ephemeral_response=ephemeral)


@pytest.fixture
def wire():
    requests = []
    def request(req):
        requests.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={'id': f'actual-platform-{len(requests)}'})
    return requests, httpx.MockTransport(request)


@pytest.mark.asyncio
async def test_text_then_exact_granted_snapshot_with_same_turn_receipts(bot, wire, tmp_path):
    event = make_event(bot)
    requests, transport = wire
    policy = policy_for_adapter(bot, event.source)
    private = tmp_path / 'private.gif'
    private.write_bytes(b'private')
    assert not policy.allows_media(private)
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, owner = await final(bot, event)
    assert owner is bot and result.success and result.message_id == 'actual-platform-1'
    assert [body['msg_type'] for _, body in requests] == [2, 7]
    text_body, gif_body = (body for _, body in requests)
    assert 'media' not in text_body
    assert 'media' in gif_body
    assert not {'content', 'markdown', 'caption'} & gif_body.keys()
    assert all(path == f'/v2/groups/{GROUP}/messages' for path, _ in requests)
    upload_path = Path(bot._upload_local_file.call_args.args[2])
    assert upload_path.name.startswith('meme-') and not upload_path.exists()
    assert not policy.allows_media(private) and not policy.allows_media(Path(bot.config.extra['memes']['root_dir']) / '肯定.gif')
    rows = receipt_journal(bot, API_BASE).lookup_source(chat_id=GROUP,
        source_session_key=bot._source_session_key(event.source), source_message_id=event.message_id)
    assert [row.message_id for row in rows] == ['actual-platform-1', 'actual-platform-2']
    assert not list((bot._receipt_home / 'cache' / 'qqbot' / 'meme_snapshots').glob('*.gif'))


@pytest.mark.asyncio
async def test_same_final_reentry_never_reselects_or_reuploads(bot, wire):
    event = make_event(bot)
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        await final(bot, event)
        await final(bot, event)
    controller_for(bot).selector.assert_awaited_once()
    bot._upload_local_file.assert_awaited_once()


@pytest.mark.parametrize('case', ['off', 'p0', 'not-allowed', 'internal', 'synthetic', 'command', 'raw-command',
    'ephemeral', 'no-marker', 'attachments', 'different-turn', 'changed-subject', 'long-preview'])
@pytest.mark.asyncio
async def test_non_dialogue_and_ineligible_finals_never_select(bot, wire, case):
    event = make_event(bot)
    ctrl = controller_for(bot)
    text, ephemeral = 'yes, fun', False
    if case == 'off':
        ctrl.settings = replace(ctrl.settings, enabled=False)
    elif case == 'p0':
        ctrl.settings = replace(ctrl.settings, probability=0)
    elif case == 'not-allowed':
        ctrl.settings = replace(ctrl.settings, groups=frozenset())
    elif case == 'internal':
        event.internal = True
    elif case == 'synthetic':
        event.inherit_channel_pin = True
    elif case == 'command':
        event.text = '/help'
    elif case == 'raw-command':
        event.text = '[群聊-群友-visitor] /help'
        event._qq_meme_turn.input_text = '<@bot> /help'
    elif case == 'ephemeral':
        ephemeral = True
    elif case == 'no-marker':
        del event._qq_meme_turn
    elif case == 'attachments':
        text += '\n![explicit](https://example.com/image.gif)'
    elif case == 'different-turn':
        event.ledger_message_id = 'queued-other-message'
    elif case == 'changed-subject':
        event.source.user_id = 'someone-else'
    elif case == 'long-preview':
        bot._long_reply_preview.settings = replace(bot._long_reply_preview.settings,
            enabled=True, groups=(GROUP,), threshold=200)
        text *= 100
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event, text=text, ephemeral=ephemeral)
    assert result.success
    ctrl.selector.assert_not_awaited()
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_failure_never_attempts_optional_media(bot):
    bot._send_with_retry = AsyncMock(return_value=SendResult(success=False, error='synthetic refusal'))
    result, _ = await final(bot, make_event(bot))
    assert not result.success
    controller_for(bot).selector.assert_not_awaited()
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_media_never_changes_text_result_or_creates_replay(bot, wire):
    bot.send_image_file = AsyncMock(return_value=SendResult(success=False, error='lost ACK'))
    event = make_event(bot)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
        assert result.success and len(requests) == 1
        await final(bot, event)
    bot.send_image_file.assert_awaited_once()
    with sqlite3.connect(controller_for(bot).state.path) as db:
        assert db.execute('SELECT state FROM decisions').fetchone()[0] == 'unknown'


@pytest.mark.asyncio
async def test_selector_timeout_keeps_text_and_never_sends_late_meme(bot, wire):
    cancelled = asyncio.Event()
    async def stuck(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    ctrl = controller_for(bot)
    ctrl.selector = stuck
    ctrl.settings = replace(ctrl.settings, selector_timeout=.1)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await asyncio.wait_for(final(bot, make_event(bot)), 3)
    assert result.success and len(requests) == 1 and cancelled.is_set()
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_uses_user_endpoint_not_group_endpoint(bot, wire):
    bot._chat_type_map['owner'] = 'c2c'
    event = make_event(bot, chat='owner', user='owner', kind='dm')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
    assert result.success and len(requests) == 2
    assert all(path == '/v2/users/owner/messages' for path, _ in requests)


def test_invalid_settings_do_not_enable_a_bot():
    for value in (True, float('nan'), -1, 1.5, '20%'):
        settings = MemeSettings.from_extra({'memes': {'enabled': True, 'root_dir': 'C:/library',
            'probability': value, 'groups': [GROUP]}})
        assert not settings.enabled


def test_zero_daily_cap_is_an_explicit_unlimited_setting():
    settings = MemeSettings.from_extra({'memes': {'enabled': True, 'root_dir': 'C:/library',
        'groups': [GROUP], 'max_per_chat_per_day': 0}})
    assert settings.enabled and settings.daily_limit == 0
    for value in (-1, 101, 1.5, True):
        assert not MemeSettings.from_extra({'memes': {'enabled': True, 'root_dir': 'C:/library',
            'groups': [GROUP], 'max_per_chat_per_day': value}}).enabled


@pytest.mark.asyncio
async def test_real_ingress_captures_current_raw_text_not_injected_group_history(bot, wire):
    bot._process_attachments = AsyncMock(return_value={
        'voice_transcripts': [], 'attachment_info': '', 'image_urls': [], 'image_media_types': []})
    bot._process_quoted_context = AsyncMock(return_value={'quote_block': '', 'image_urls': [], 'image_media_types': []})
    bot.handle_message = AsyncMock()
    raw = {'id': 'ingress-id', 'content': 'current question only'}
    await bot._ingest(raw, raw['id'], '[群聊上下文] unrelated old text', [], '',
        chat_id=GROUP, qq_chat_type='group', chat_type='group', user_id='visitor')
    event = bot.handle_message.call_args.args[0]
    assert event._qq_meme_turn.input_text == 'current question only'
    assert not event._qq_meme_turn.prepared
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
    assert result.success and len(requests) == 2
    assert controller_for(bot).selector.call_args.args[0] == 'current question only'


@pytest.mark.parametrize('chosen', [None, 'C:/secret.gif', 'unknown-id',
                                  MemeSelection('C:/secret.gif', 'strong'), MemeSelection('unknown-id', 'strong')])
@pytest.mark.asyncio
async def test_null_or_out_of_catalog_selection_never_uploads(bot, wire, chosen):
    controller_for(bot).selector.return_value = chosen
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 1
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_catalog_gif_fails_closed_without_affecting_text(bot, wire):
    (Path(bot.config.extra['memes']['root_dir']) / '肯定.gif').write_bytes(b'GIF89a changed')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 1
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_replaced_transport_owns_optional_send(bot, wire, monkeypatch):
    replacement = QQAdapter(bot.config)
    replacement._chat_type_map[GROUP] = 'group'
    replacement._ensure_connected = AsyncMock(return_value=True)
    replacement._auth_headers = AsyncMock(return_value={})
    replacement._upload_local_file = AsyncMock(return_value={'file_info': 'synthetic-upload'})
    ctrl = controller_for(replacement)
    ctrl.draw, ctrl.clock, ctrl.selector = lambda: 0, lambda: 1000, AsyncMock(return_value=MemeSelection('yes', 'natural'))
    monkeypatch.setattr(bot, '_final_delivery_adapter', lambda source: replacement)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        replacement._http_client = client
        result, owner = await final(bot, make_event(bot))
    assert result.success and owner is replacement and len(requests) == 2
    replacement._upload_local_file.assert_awaited_once()
    bot._upload_local_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_privileged_family_member_is_not_implicitly_intimate(bot, wire):
    catalog_path = Path(bot.config.extra['memes']['catalog_path'])
    data = json.loads(catalog_path.read_text(encoding='utf-8'))
    intimate = {**data['items'][0], 'id': 'intimate-hug', 'audiences': ['intimate_dm']}
    data['items'].append(intimate)
    catalog_path.write_text(json.dumps(data), encoding='utf-8')
    bot.config.extra['allow_admin_from'].append('teacher')
    bot._chat_type_map['teacher'] = 'c2c'
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, dm_users=frozenset({'owner', 'teacher'}))
    event = make_event(bot, chat='teacher', user='teacher', kind='dm')
    assert policy_for_adapter(bot, event.source).is_admin
    _, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
    assert result.success
    assert [entry.id for entry in ctrl.selector.call_args.args[2]] == ['yes']


def test_all_groups_requires_an_explicit_boolean_opt_in():
    settings = MemeSettings.from_extra({'memes': {
        'enabled': True, 'root_dir': 'C:/library', 'groups': [], 'all_groups': True}})
    assert settings.enabled and settings.all_groups is True
    default = MemeSettings.from_extra({'memes': {'enabled': True, 'root_dir': 'C:/library'}})
    assert default.all_groups is False


@pytest.mark.parametrize('value', ['true', 1, None, []])
def test_malformed_all_groups_flag_does_not_enable_a_bot(value):
    settings = MemeSettings.from_extra({'memes': {
        'enabled': True, 'root_dir': 'C:/library', 'all_groups': value}})
    assert not settings.enabled


@pytest.mark.asyncio
async def test_all_groups_covers_new_group_not_present_in_any_allowlist(bot, wire):
    options = {**bot.config.extra['memes'], 'all_groups': True, 'groups': []}
    controller_for(bot).settings = MemeSettings.from_extra({'memes': options})
    group = 'newly-joined-group'
    bot._chat_type_map[group] = 'group'
    event = make_event(bot, chat=group)
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
    assert result.success and [body['msg_type'] for _, body in requests] == [2, 7]
    assert all(path == f'/v2/groups/{group}/messages' for path, _ in requests)


@pytest.mark.asyncio
async def test_all_groups_does_not_open_other_private_chats(bot, wire):
    controller_for(bot).settings = MemeSettings.from_extra({'memes': {
        **bot.config.extra['memes'], 'all_groups': True, 'groups': []}})
    bot._chat_type_map['not-an-allowed-dm'] = 'c2c'
    event = make_event(bot, chat='not-an-allowed-dm', user='not-an-allowed-dm', kind='dm')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, event)
    assert result.success and len(requests) == 1
    controller_for(bot).selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_selection_happens_before_any_luck_draw(bot, wire):
    from plugins.platforms.qqbot.meme_selector import MemeSelection
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, probability=.2)
    observed = []
    async def select(*args, **kwargs):
        observed.append('semantic')
        return MemeSelection('yes', 'natural')
    def draw():
        observed.append('luck')
        assert observed == ['semantic', 'luck']
        return .9
    ctrl.selector, ctrl.draw = select, draw
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 1 and observed == ['semantic', 'luck']
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT misses FROM meme_prd').fetchone() == (1,)


@pytest.mark.asyncio
async def test_strong_semantic_fit_sends_even_when_a_random_roll_would_miss(bot, wire):
    from plugins.platforms.qqbot.meme_selector import MemeSelection
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, probability=.2)
    ctrl.selector.return_value = MemeSelection('yes', 'strong')
    ctrl.draw = lambda: pytest.fail('Strong semantic fit must bypass the lottery')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 2


@pytest.mark.asyncio
async def test_no_match_does_not_draw_or_accumulate_luck(bot, wire):
    ctrl = controller_for(bot)
    ctrl.selector.return_value = None
    ctrl.draw = lambda: pytest.fail('No suitable candidate is not a failed luck roll')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 1
    ctrl.selector.assert_awaited_once()
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT COUNT(*) FROM meme_prd').fetchone() == (0,)


@pytest.mark.asyncio
async def test_broken_asset_is_not_counted_as_a_failed_luck_roll(bot, wire):
    from plugins.platforms.qqbot.meme_selector import MemeSelection
    ctrl = controller_for(bot)
    ctrl.settings = replace(ctrl.settings, probability=.2)
    ctrl.selector.return_value = MemeSelection('yes', 'natural')
    ctrl.draw = lambda: pytest.fail('An invalid GIF must be rejected before luck is changed')
    (Path(bot.config.extra['memes']['root_dir']) / '肯定.gif').write_bytes(b'changed')
    requests, transport = wire
    async with httpx.AsyncClient(transport=transport) as client:
        bot._http_client = client
        result, _ = await final(bot, make_event(bot))
    assert result.success and len(requests) == 1
    with sqlite3.connect(ctrl.state.path) as db:
        assert db.execute('SELECT COUNT(*) FROM meme_prd').fetchone() == (0,)
