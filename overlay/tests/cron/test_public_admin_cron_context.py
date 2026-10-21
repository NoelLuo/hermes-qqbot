"""Public admin jobs isolate automatic context without persisting an admin grant."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.request_security import RequestSecurityPolicy, current_policy, policy_scope
from cron import jobs, scheduler
from gateway import config as gateway_config
from tools import cronjob_tools


@pytest.fixture
def public_admin_job(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_constants.profile_name_for_home', lambda home: 'default')
    extra = {'role_policy': {'enabled': True}, 'group_allow_admin_from': ['real-admin']}
    monkeypatch.setattr(gateway_config, 'load_gateway_config', lambda: SimpleNamespace(
        platforms={'qqbot': SimpleNamespace(extra=extra)}))
    with policy_scope(RequestSecurityPolicy('real-admin', 'qqbot', 'real-group', is_admin=True)):
        job = jobs.create_job('Public reminder', 'in 30m', enabled_toolsets=['terminal'],
                              origin={'platform': 'qqbot', 'chat_id': 'forged', 'user_id': 'forged'})
    return jobs.get_job(job['id']), extra


def test_create_persist_and_reread_bound_context_not_admin_grant(public_admin_job):
    job, _ = public_admin_job
    assert job['public_context_only'] is True
    assert job['public_context_author'] == {
        'subject_id': 'real-admin', 'platform': 'qqbot', 'chat_id': 'real-group', 'scope_id': 'default'}
    assert job['origin'] == {'platform': 'qqbot', 'chat_id': 'real-group', 'user_id': 'real-admin'}
    assert 'request_policy' not in job
    assert 'is_admin' not in json.dumps(job)
    assert job['enabled_toolsets'] == ['terminal']
    assert jobs.get_job(job['id'])['public_context_author'] == job['public_context_author']


@pytest.mark.parametrize('field,value', [
    ('public_context_only', False), ('public_context_author', {'subject_id': 'other'}),
    ('request_policy', None), ('origin', {'chat_id': 'other'}),
])
def test_updates_cannot_rewrite_provenance(public_admin_job, field, value):
    job, _ = public_admin_job
    with pytest.raises(ValueError, match='cannot be updated'):
        jobs.update_job(job['id'], {field: value})
    assert jobs.get_job(job['id'])['origin'] == job['origin']


@pytest.mark.parametrize('policy', [None, RequestSecurityPolicy('guest', 'qqbot', 'group'),
                                        RequestSecurityPolicy('admin', 'qqbot', 'group', is_admin=True)])
@pytest.mark.parametrize('field', ['public_context_only', 'public_context_author', 'request_policy'])
def test_model_cannot_supply_trusted_metadata(monkeypatch, policy, field):
    create = Mock(side_effect=AssertionError('must not dispatch'))
    monkeypatch.setattr(cronjob_tools, 'cronjob', create)
    with policy_scope(policy):
        result = json.loads(cronjob_tools._cronjob_handler({'action': 'create', field: True}))
    assert result['success'] is False
    create.assert_not_called()


def test_consumer_binds_before_constructor_and_watchdog_keeps_admin_tools(public_admin_job, monkeypatch):
    job, _ = public_admin_job
    monkeypatch.setattr(scheduler, '_resolve_cron_enabled_toolsets', lambda *a: ['terminal'])
    monkeypatch.setattr(scheduler, '_resolve_cron_disabled_toolsets', lambda *a: [])
    captured = []
    def constructor(**kwargs):
        policy = current_policy()
        assert policy.subject_id == 'real-admin' and policy.chat_id == 'real-group'
        assert policy.is_admin and policy.public_context
        assert policy.allows_tool('terminal') and policy.allows_tool('execute_code')
        captured.append(kwargs)
        return SimpleNamespace()
    setup = scheduler._CronAgentSetup(runtime={}, prefill_messages=['private-history'])
    def execute(job, **kw):
        assert current_policy().is_admin and current_policy().public_context
        agent = scheduler._construct_cron_agent(constructor, job, {}, setup,
            workdir='C:/test-workdir', session_id='test-only', session_db=None)
        def conversation(prompt, **kw):
            assert current_policy() is agent._request_security_policy
            assert current_policy().allows_tool('terminal')
            return {'final_response': 'public reminder'}
        agent.run_conversation = conversation
        result = scheduler._run_agent_with_watchdog(
            agent, 'reminder', {}, 'test-only', 'test-only', 'test-task', None)
        return True, '', result['final_response'], None
    monkeypatch.setattr(scheduler, '_run_job_scoped', execute)
    assert scheduler.run_job(job) == (True, '', 'public reminder', None)
    assert captured[0]['skip_memory'] is True
    assert captured[0]['skip_context_files'] is True
    assert captured[0]['load_soul_identity'] is False
    assert captured[0]['prefill_messages'] is None
    assert captured[0]['enabled_toolsets'] == ['terminal']
    assert current_policy() is None


@pytest.mark.parametrize('change', ['revoke', 'disable', 'config_error', 'origin', 'profile', 'marker', 'guest'])
def test_revocation_and_malformed_metadata_fail_before_execution(public_admin_job, monkeypatch, change):
    job, extra = public_admin_job
    job = copy.deepcopy(job)
    if change == 'revoke':
        extra['group_allow_admin_from'] = []
    elif change == 'disable':
        extra['role_policy']['enabled'] = False
    elif change == 'config_error':
        monkeypatch.setattr(gateway_config, 'load_gateway_config', Mock(side_effect=OSError('unavailable')))
    elif change == 'origin':
        job['origin']['user_id'] = 'other'
    elif change == 'profile':
        job['public_context_author']['scope_id'] = 'other'
    elif change == 'marker':
        job['public_context_only'] = False
    else:
        job['request_policy'] = RequestSecurityPolicy('guest', 'qqbot', 'real-group').to_payload()
    execute = Mock(side_effect=AssertionError('must not execute'))
    monkeypatch.setattr(scheduler, '_run_job_scoped', execute)
    result = scheduler.run_job(job)
    assert result[0] is False and 'public cron policy' in result[3]
    execute.assert_not_called()


def test_real_config_reload_revokes_public_admin_job(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_constants.profile_name_for_home', lambda home: 'default')
    config_path = tmp_path / 'config.yaml'
    config = {'platforms': {'qqbot': {'enabled': False, 'extra': {
        'role_policy': {'enabled': True}, 'group_allow_admin_from': ['real-admin']}}}}
    config_path.write_text(json.dumps(config), encoding='utf-8')
    with policy_scope(RequestSecurityPolicy('real-admin', 'qqbot', 'real-group', is_admin=True)):
        created = jobs.create_job('Public reminder', 'in 30m')
    job = jobs.get_job(created['id'])
    assert scheduler._public_cron_policy(job).allows_tool('terminal')
    config['platforms']['qqbot']['extra']['group_allow_admin_from'] = []
    config_path.write_text(json.dumps(config), encoding='utf-8')
    execute = Mock(side_effect=AssertionError('revoked must not execute'))
    monkeypatch.setattr(scheduler, '_run_job_scoped', execute)
    assert scheduler.run_job(job)[0] is False
    execute.assert_not_called()


def test_constructor_rechecks_revocation(public_admin_job, monkeypatch):
    job, extra = public_admin_job
    policy = scheduler._public_cron_policy(job)
    extra['group_allow_admin_from'] = []
    ctor = Mock()
    with policy_scope(policy), pytest.raises(ValueError, match='no longer'):
        scheduler._construct_cron_agent(ctor, job, {}, None,
            workdir=None, session_id='test-only', session_db=None)
    ctor.assert_not_called()


@pytest.mark.parametrize('private_admin', [True, False])
def test_private_admin_and_legacy_keep_none_policy_and_original_settings(tmp_path, monkeypatch, private_admin):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(gateway_config, 'load_gateway_config', Mock(side_effect=AssertionError('legacy must not read config')))
    policy = RequestSecurityPolicy('admin', 'qqbot', 'dm', is_admin=True, public_context=False) if private_admin else None
    with policy_scope(policy):
        job = jobs.create_job('Legacy reminder', 'in 30m')
    assert 'public_context_only' not in job and 'public_context_author' not in job
    assert 'request_policy' not in job
    assert scheduler._public_cron_policy(job) is None
    monkeypatch.setattr(scheduler, '_resolve_cron_enabled_toolsets', lambda *a: ['terminal'])
    monkeypatch.setattr(scheduler, '_resolve_cron_disabled_toolsets', lambda *a: [])
    seen = []
    def ctor(**kw):
        assert current_policy() is None
        seen.append(kw)
        return SimpleNamespace()
    scheduler._construct_cron_agent(ctor, job, {}, scheduler._CronAgentSetup(runtime={}, prefill_messages=['legacy']),
        workdir='C:/test-workdir', session_id='test-only', session_db=None)
    assert seen[0]['skip_memory'] is False
    assert seen[0]['skip_context_files'] is False
    assert seen[0]['load_soul_identity'] is True
    assert seen[0]['prefill_messages'] == ['legacy']
