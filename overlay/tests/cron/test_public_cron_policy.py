"""Durable public cron policy has an enforcing scheduler consumer."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.request_security import RequestSecurityPolicy, current_policy, policy_scope
from cron import jobs, scheduler


@pytest.fixture
def public_job(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_constants.profile_name_for_home', lambda home: 'default')
    with policy_scope(RequestSecurityPolicy('visitor', 'qqbot', 'group123')):
        job = jobs.create_job('提醒大家喝水', 'in 30m')
    return jobs.get_job(job['id'])


def test_policy_cannot_be_replaced_by_update(public_job):
    with pytest.raises(ValueError, match='cannot be updated'):
        jobs.update_job(public_job['id'], {'request_policy': None})
    assert jobs.get_job(public_job['id'])['request_policy'] == public_job['request_policy']


@pytest.mark.parametrize('change', [
    {'script': 'evil.py'}, {'no_agent': True}, {'context_from': ['other']},
    {'skills': ['private']}, {'workdir': 'C:/'}, {'deliver': 'qqbot:other'},
    {'failure_deliver': 'telegram:private'}, {'enabled_toolsets': ['terminal']},
    {'request_policy': None}, {'request_policy': {'version': 1, 'is_admin': True}},
])
def test_bad_durable_jobs_fail_before_execution(public_job, monkeypatch, change):
    execute = Mock(side_effect=AssertionError('must fail before execution'))
    monkeypatch.setattr(scheduler, '_run_job_scoped', execute)
    result = scheduler.run_job({**public_job, **change})
    assert result[0] is False
    assert 'public cron policy' in result[3]
    execute.assert_not_called()


def test_cross_profile_durable_job_rejected(public_job):
    public_job['request_policy']['scope_id'] = 'other-profile'
    assert scheduler.run_job(public_job)[0] is False


def test_scheduler_binds_policy_before_constructor_and_execution(public_job, monkeypatch):
    seen = []
    def constructor(**kwargs):
        policy = current_policy()
        assert policy is not None and not policy.is_admin and policy.public_context
        assert not policy.allows_tool('terminal')
        seen.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(scheduler, '_resolve_cron_enabled_toolsets', lambda *a: ['web'])
    monkeypatch.setattr(scheduler, '_resolve_cron_disabled_toolsets', lambda *a: [])
    setup = scheduler._CronAgentSetup(runtime={}, prefill_messages=[{'role': 'user', 'content': 'private'}])
    agent = scheduler._construct_cron_agent(constructor, public_job, {}, setup,
        workdir=None, session_id='test-public', session_db=None)
    assert agent._request_security_policy == RequestSecurityPolicy.from_payload(public_job['request_policy'])
    assert seen[0]['skip_memory'] is True
    assert seen[0]['load_soul_identity'] is False
    assert seen[0]['skip_context_files'] is True
    assert seen[0]['prefill_messages'] is None
    assert current_policy() is None
    def run(job, **kw):
        assert current_policy() == agent._request_security_policy
        def conversation(prompt, **kwargs):
            assert current_policy() == agent._request_security_policy
            assert not current_policy().allows_tool('execute_code')
            return {'final_response': 'reminder'}
        fake_agent = SimpleNamespace(run_conversation=conversation)
        result = scheduler._run_agent_with_watchdog(
            fake_agent, 'test reminder', {}, 'test-only', 'test-only', 'test-task', None)
        assert result['final_response'] == 'reminder'
        return True, 'test output', 'reminder', None
    monkeypatch.setattr(scheduler, '_run_job_scoped', run)
    assert scheduler.run_job(public_job)[0] is True
    assert current_policy() is None


def test_public_delivery_cannot_attach_model_named_local_files(public_job, tmp_path):
    policy = scheduler._public_cron_policy(public_job)
    private_file = tmp_path / 'private-test.txt'
    private_file.write_text('not for delivery', encoding='utf-8')
    content = scheduler._filter_public_cron_media(f'Reminder! MEDIA:{private_file}', policy)
    assert 'MEDIA:' not in content
    assert str(private_file) not in content
    assert 'Reminder!' in content


def test_public_delivery_preserves_trusted_tts_artifacts(public_job, tmp_path, monkeypatch):
    policy = scheduler._public_cron_policy(public_job)
    audio = tmp_path / 'trusted-tts.mp3'
    audio.write_bytes(b'test-audio')
    assert policy.grant_artifact(str(audio))
    content = scheduler._filter_public_cron_media(f'Reminder! MEDIA:{audio}', policy)
    assert f'MEDIA:{audio}' in content
    assert not scheduler._public_cron_policy(public_job).allows_media(str(audio))
    monkeypatch.setattr(scheduler, '_construct_cron_agent_scoped', lambda *a, **kw: SimpleNamespace())
    with policy_scope(policy):
        agent = scheduler._construct_cron_agent(None, public_job, {}, None,
            workdir=None, session_id='test', session_db=None)
    assert agent._request_security_policy is policy
    assert agent._request_security_policy.allows_media(str(audio))


def test_legacy_constructor_preserves_settings(monkeypatch):
    monkeypatch.setattr(scheduler, '_resolve_cron_enabled_toolsets', lambda *a: ['terminal'])
    monkeypatch.setattr(scheduler, '_resolve_cron_disabled_toolsets', lambda *a: [])
    ctor = Mock(return_value=SimpleNamespace())
    setup = scheduler._CronAgentSetup(runtime={}, prefill_messages=['legacy'])
    scheduler._construct_cron_agent(ctor, {}, {}, setup, workdir=None, session_id='test', session_db=None)
    kwargs = ctor.call_args.kwargs
    assert kwargs['skip_memory'] is False
    assert kwargs['load_soul_identity'] is True
    assert kwargs['prefill_messages'] == ['legacy']
