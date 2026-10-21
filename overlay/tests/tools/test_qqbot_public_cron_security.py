"""Public cron creation must not become a privileged delayed tool call."""
import json
from unittest.mock import Mock

import pytest

from agent.request_security import RequestSecurityPolicy, policy_scope
from cron import jobs, scheduler
from tools import cronjob_tools as tools
from tools.registry import registry


@pytest.fixture
def guest(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr('hermes_constants.profile_name_for_home', lambda home: 'default')
    monkeypatch.setattr(scheduler, 'create_job_with_scheduler_registration', jobs.create_job)
    monkeypatch.setattr(tools, '_gateway_liveness_notice', lambda **kw: {})
    monkeypatch.setattr(tools, '_local_delivery_notice', lambda *a: None)
    monkeypatch.setattr(tools, '_origin_from_env', lambda *a: {'platform': 'qqbot', 'chat_id': 'spoof'})
    return RequestSecurityPolicy('visitor', 'qqbot', 'group123')


def call(args):
    return json.loads(registry.get_entry('cronjob_manage').handler(args))


def test_public_create_roundtrips_bound_policy(guest):
    with policy_scope(guest):
        result = call({'action': 'create', 'schedule': 'in 30m', 'prompt': '提醒大家喝水'})
    assert result['success'], result
    job = jobs.get_job(result['job_id'])
    assert job['deliver'] == job['failure_deliver'] == 'qqbot:group123'
    assert job['origin'] == {'platform': 'qqbot', 'chat_id': 'group123', 'user_id': 'visitor'}
    policy = RequestSecurityPolicy.from_payload(job['request_policy'])
    assert not policy.is_admin and policy.public_context
    assert not policy.allows_tool('terminal')
    assert not policy.allows_tool('cronjob_manage', {'action': 'create'})
    assert job['enabled_toolsets'] == ['web']


@pytest.mark.parametrize('action', ['list', 'update', 'remove', 'pause', 'resume', 'run', 'trigger'])
def test_guest_cannot_inspect_or_mutate_existing_jobs(guest, monkeypatch, action):
    resolve = Mock(side_effect=AssertionError('must reject before job resolution'))
    monkeypatch.setattr(tools, '_resolve_job_or_error', resolve)
    monkeypatch.setattr(tools, 'list_jobs', resolve)
    with policy_scope(guest):
        result = call({'action': action, 'job_id': 'other-person-job'})
    assert not result['success']
    resolve.assert_not_called()


@pytest.mark.parametrize('override', [
    {'script': 'evil.py'}, {'monitor': 'http://127.0.0.1/'}, {'no_agent': True},
    {'skills': ['private']}, {'context_from': ['other-job']}, {'continuity': True},
    {'workdir': 'C:/'}, {'enabled_toolsets': ['terminal']}, {'attach_to_session': True},
    {'deliver': 'qqbot:other'}, {'deliver': 'all'}, {'failure_deliver': 'qqbot:other'},
    {'request_policy': {'is_admin': True}}, {'metadata': {'policy': 'admin'}},
    {'base_url': 'https://attacker.invalid'}, {'api_key': 'not-a-real-key'},
])
def test_guest_malicious_create_rejected_without_store(guest, override):
    with policy_scope(guest):
        result = call({'action': 'create', 'schedule': 'in 30m', 'prompt': '提醒喝水', **override})
    assert not result['success'], result
    assert jobs.load_jobs() == []


@pytest.mark.parametrize('policy', [None, RequestSecurityPolicy('admin', 'qqbot', 'group123', is_admin=True)])
def test_legacy_and_admin_actions_still_dispatch(guest, monkeypatch, policy):
    monkeypatch.setattr(tools, '_action_list', lambda a: json.dumps({'success': True, 'legacy': True}))
    monkeypatch.setitem(tools._JOBLESS_ACTIONS, 'list', tools._action_list)
    with policy_scope(policy):
        assert call({'action': 'list'}) == {'success': True, 'legacy': True}
