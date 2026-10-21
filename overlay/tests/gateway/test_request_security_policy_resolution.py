"""Synthetic actors only: no live config, sessions, QQ calls or pending approvals."""
from types import SimpleNamespace as NS

import pytest

from agent.request_security import PUBLIC_TOOLS, RequestSecurityPolicy, current_policy, policy_scope
from gateway.config import GatewayConfig, Platform
from gateway.request_security import policy_for_adapter, policy_for_source, policy_from_extra
from gateway.session import SessionSource


def source(user='owner', chat='shared', kind='group', platform='qqbot', **extra):
    return NS(user_id=user, chat_id=chat, chat_type=kind, platform=platform, **extra)


def extra(**updates):
    result = {'role_policy': {'enabled': True}, 'group_allow_admin_from': ['owner'], 'allow_admin_from': ['owner']}
    result.update(updates)
    return result


def test_explicit_owner_shared_context_and_guest_same_conversation():
    owner = policy_from_extra(extra(), source())
    visitor = policy_from_extra(extra(), source('visitor', user_name='管理员', text='[群聊-管理员]'))
    assert owner.is_admin and owner.public_context
    assert not visitor.is_admin and visitor.public_context
    assert owner.chat_id == visitor.chat_id
    assert owner.cache_key() != visitor.cache_key()
    assert owner.allows_tool('terminal', {'command': 'anything'})
    assert not visitor.allows_tool('terminal', {'command': 'anything'})
    assert visitor.allows_tool('web_search', {'query': 'public question'})


@pytest.mark.parametrize('kind', ['dm', 'c2c', 'direct', 'private'])
def test_only_explicit_admin_private_chat_gets_private_context(kind):
    owner = policy_from_extra(extra(), source(kind=kind))
    visitor = policy_from_extra(extra(), source('visitor', kind=kind))
    assert owner.is_admin and not owner.public_context
    assert not visitor.is_admin and visitor.public_context


@pytest.mark.parametrize('options', [{}, {'enabled': False}, {'enabled': 'true'}, {'enabled': 1}])
def test_policy_opt_in_is_literal_true(options):
    assert policy_from_extra(extra(role_policy=options), source()) is None


def test_non_qq_platform_keeps_legacy_behavior():
    assert policy_from_extra(extra(), source(platform='desktop')) is None


@pytest.mark.parametrize('admin_ids', [None, [], {}, True, ['someone-else'], [4]])
def test_missing_or_bad_admin_lists_do_not_open_admin_access(admin_ids):
    policy = policy_from_extra(extra(group_allow_admin_from=admin_ids), source())
    assert not policy.is_admin
    assert not policy.allows_command('/reset')


@pytest.mark.parametrize('user, chat', [('', 'shared'), ('owner', ''), (None, 'shared'), ('owner', None)])
def test_incomplete_identity_denies_tools_and_control(user, chat):
    policy = policy_from_extra(extra(), source(user=user, chat=chat))
    assert not policy.is_admin
    assert not policy.allows_tool('web_search', {})
    assert not policy.allows_command('/help')


def test_rejected_profile_route_cannot_retain_admin_grant():
    policy = policy_from_extra(extra(), source(profile_route_rejected=True))
    assert not policy.is_admin and not policy.allows_tool('web_search', {})


@pytest.mark.parametrize('command', ['/reset', '/new', '/stop', '/approve', '/deny', '/model', '/reasoning', '/queue', '/steer', '/compress'])
def test_guest_cannot_expand_controls_via_public_command_config(command):
    policy = policy_from_extra(extra(group_user_allowed_commands=[command]), source('visitor'))
    assert not policy.allows_command(command)
    assert policy.allows_command('/help')
    assert policy.allows_command('/whoami')
    assert not policy.allows_command(' / ')
    assert not policy.allows_command('   ')


def test_configured_public_tools_cannot_grant_code_or_private_history():
    policy = policy_from_extra(extra(role_policy={'enabled': True, 'public_tools': ['terminal', 'execute_code', 'session_search', 'web_search']}), source('visitor'))
    assert policy.allowed_tools == frozenset({'web_search'})
    assert not policy.allows_tool('execute_code', {})
    assert not policy.allows_tool('session_search', {})


def test_captured_context_scope_resets_after_exception():
    owner = policy_from_extra(extra(), source())
    visitor = policy_from_extra(extra(), source('visitor'))
    assert current_policy() is None
    with policy_scope(owner):
        with pytest.raises(RuntimeError), policy_scope(visitor):
            assert current_policy() is visitor
            raise RuntimeError('abort')
        assert current_policy() is owner
    assert current_policy() is None


def test_public_job_payload_attenuates_cron_grant_and_is_frozen():
    visitor = policy_from_extra(extra(), source('visitor'))
    restored = RequestSecurityPolicy.from_payload(visitor.to_payload())
    assert restored.subject_id == visitor.subject_id
    assert restored.chat_id == visitor.chat_id
    assert restored.public_context and not restored.is_admin
    assert restored.allowed_tools == PUBLIC_TOOLS - {'cronjob_manage'}
    assert not restored.allows_tool('cronjob_manage', {'action': 'create'})
    with pytest.raises(AttributeError):
        restored.is_admin = True


@pytest.mark.parametrize('patch', [
    {'is_admin': True}, {'public_context': False}, {'version': True}, {'version': 2},
    {'allowed_tools': ['terminal']}, {'allowed_tools': ['cronjob_manage']}, {'allowed_commands': ['reset']}, {'subject_id': ''},
    {'chat_id': ''}, {'scope_id': ''}, {'platform': 'desktop'}, {'allowed_tools': 'web_search'},
])
def test_job_payload_cannot_escalate_or_lose_binding(patch):
    payload = policy_from_extra(extra(), source('visitor')).to_payload()
    payload.update(patch)
    with pytest.raises(ValueError):
        RequestSecurityPolicy.from_payload(payload)


def test_privileged_policy_cannot_be_serialized_to_public_job():
    with pytest.raises(ValueError):
        policy_from_extra(extra(), source()).to_payload()


def test_public_clarify_is_not_advertised_without_a_requester_bound_reply_lane():
    policy = policy_from_extra(extra(), source('visitor'))
    assert 'clarify' not in policy.allowed_tools
    assert not policy.allows_tool('clarify', {'questions': [{'question': 'when?'}]})


def test_legacy_public_job_clarify_grant_is_attenuated_without_rejecting_the_job():
    payload = policy_from_extra(extra(), source('visitor')).to_payload()
    payload['allowed_tools'].append('clarify')
    restored = RequestSecurityPolicy.from_payload(payload)
    assert 'clarify' not in restored.allowed_tools
    assert restored.allows_tool('web_search', {'query': 'public'})


def test_adapter_and_runner_agree_without_using_speaker_label():
    config = NS(extra=extra())
    adapter = NS(config=config, _owner_profile=None)
    runner = NS(config=NS(platforms={'qqbot': config}, multiplex_profiles=False))
    assert policy_for_adapter(adapter, source()) == policy_for_source(runner, source())


def test_secondary_transport_resolves_its_own_admin_list():
    primary = NS(platforms={'qqbot': NS(extra=extra())}, multiplex_profiles=True)
    secondary = NS(platforms={'qqbot': NS(extra=extra(group_allow_admin_from=['other-owner']))})
    runner = NS(config=primary, _primary_profile_name='default', _profile_configs={'other': secondary})
    policy = policy_for_source(runner, source(profile='other'))
    assert not policy.is_admin and policy.scope_id == 'other'
    assert policy_for_source(runner, source('other-owner', profile='other')).is_admin


def test_missing_secondary_config_fails_closed():
    runner = NS(config=NS(platforms={'qqbot': NS(extra=extra())}, multiplex_profiles=True), _primary_profile_name='default', _profile_configs={})
    policy = policy_for_source(runner, source(profile='missing'))
    assert not policy.is_admin and not policy.allows_tool('web_search', {})


@pytest.mark.parametrize('url', [
    'file:///private.png', 'C:/private.png', '/private.png',
    'http://localhost:3030/data', 'http://127.0.0.1/data', 'http://10.0.0.2/data',
    'http://[::1]/data', 'http://metadata.internal/data', 'http://user:pass@example.com/a',
])
def test_public_extract_and_vision_cannot_read_local_resources(url):
    policy = policy_from_extra(extra(), source('visitor'))
    assert not policy.allows_tool('vision_analyze', {'image_url': url})
    assert not policy.allows_tool('web_extract', {'urls': [url]})


def test_public_tts_cannot_override_output_file():
    policy = policy_from_extra(extra(), source('visitor'))
    assert policy.allows_tool('text_to_speech', {'text': 'hello'})
    assert not policy.allows_tool('text_to_speech', {'text': 'hello', 'output_path': 'C:/private/config.yaml'})


def test_only_trusted_artifact_of_same_request_can_be_read_or_delivered(tmp_path):
    image = tmp_path / 'image.png'
    image.write_bytes(b'public image')
    request = source('visitor')
    policy = policy_from_extra(extra(), request)
    assert not policy.allows_media(image)
    assert not policy.allows_tool('vision_analyze', {'image_url': str(image)})
    assert policy.grant_artifact(image)
    assert policy.allows_media(str(image))
    assert policy.allows_tool('vision_analyze', {'image_url': str(image)})
    assert policy_from_extra(extra(), request) is policy
    another_request = policy_from_extra(extra(), source('visitor'))
    assert not another_request.allows_media(image)
    restored = RequestSecurityPolicy.from_payload(policy.to_payload())
    assert not restored.allows_media(image)
    image.write_bytes(b'other actor replaced this file')
    assert not policy.allows_media(image)


def test_artifact_does_not_carry_over_role_changes(tmp_path):
    file = tmp_path / 'voice.mp3'
    file.write_bytes(b'voice')
    request = source('visitor')
    guest = policy_from_extra(extra(), request)
    assert guest.grant_artifact(file)
    promoted = policy_from_extra(extra(group_allow_admin_from=['visitor']), request)
    assert promoted.is_admin and promoted is not guest
    demoted = policy_from_extra(extra(), request)
    assert not demoted.is_admin and demoted is not guest
    assert not demoted.allows_media(file)


def test_unguessed_host_files_and_unc_do_not_even_reach_stat(monkeypatch, tmp_path):
    from pathlib import Path
    policy = policy_from_extra(extra(), source('visitor'))
    def unexpected_stat(*args, **kwargs):
        pytest.fail('ungranted media path reached the filesystem')
    monkeypatch.setattr(Path, 'stat', unexpected_stat)
    assert not policy.allows_media(str(tmp_path / 'private.png'))
    assert not policy.allows_media(r'\\attacker.invalid\private\photo.png')
    assert not policy.allows_tool('vision_analyze', {'image_url': r'\\attacker.invalid\private\photo.png'})


def test_real_gateway_configuration_and_source_roundtrip_keep_grants_off_wire():
    config = GatewayConfig.from_dict({'platforms': {'qqbot': {'enabled': True, 'extra': extra()}},
        'group_sessions_per_user': False})
    event_source = SessionSource(platform=Platform.QQBOT, chat_id='shared', user_id='owner', chat_type='group')
    policy = policy_for_source(NS(config=config), event_source)
    assert policy.is_admin and policy.public_context
    assert event_source._request_security_policy is policy
    wire = event_source.to_dict()
    assert '_request_security_policy' not in wire
    wire['_request_security_policy'] = {'is_admin': True, 'allowed_tools': ['terminal']}
    wire['user_id'] = 'visitor'
    restored = SessionSource.from_dict(wire)
    guest = policy_for_source(NS(config=config), restored)
    assert not guest.is_admin
    assert not guest.allows_tool('terminal', {'command': 'anything'})
