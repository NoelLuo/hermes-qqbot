"""Actor-aware cache isolation without splitting shared group history."""
import threading
from types import SimpleNamespace

import pytest
import gateway.run
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_execution import GatewaySessionAgentMixin
from gateway.run_turn_runner import TurnRunner
from agent.request_security import current_policy


def make_turn():
    source = SessionSource(platform=Platform.QQBOT, chat_id='group', user_id='admin', chat_type='group')
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=False, platforms={Platform.QQBOT: SimpleNamespace(extra={
            'role_policy': {'enabled': True}, 'group_allow_admin_from': ['admin'], 'allow_admin_from': ['admin']})}),
        _agent_cache={}, _agent_cache_lock=threading.Lock(), _session_db=None,
        _prefill_messages=None, _service_tier=None,
        _agent_config_signature=lambda *a, **kw: 'same-route',
        _extract_cache_busting_config=lambda config: {},
        _refresh_fallback_model=lambda: None,
        _apply_fallback_chain_to_agent=lambda *a: None,
        _init_cached_agent_for_turn=lambda *a: None,
        _enforce_agent_cache_cap=lambda: None)
    ctx = SimpleNamespace(source=source, session_key='shared-group', session_id='sid', history=[],
        user_config={}, enabled_toolsets=['web'], disabled_toolsets=None, _interrupt_depth=0)
    class Turn(GatewaySessionAgentMixin):
        pass
    turn = Turn()
    turn._runner, turn._ctx = runner, ctx
    turn._skip_context_files = lambda platform: False
    return turn


def test_current_actor_and_config_rebuild_cache_without_splitting_history(monkeypatch):
    turn = make_turn()
    ctx = turn._ctx
    history = ctx.history
    built = []
    def construct(**kwargs):
        agent = SimpleNamespace(_request_security_policy=current_policy(), session_id='sid')
        built.append(agent)
        return agent
    ctx.AIAgent = construct
    monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *a: None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda *a: None)
    route = {'model': 'test', 'runtime': {}}
    def resolve():
        return turn._resolve_turn_agent(route, 'qqbot', '', 10, None, {})
    admin, reused = resolve()
    assert not reused and admin._request_security_policy.is_admin
    assert resolve() == (admin, True)
    ctx.source.user_id = 'guest'
    guest, reused = resolve()
    assert not reused and guest is not admin
    assert not guest._request_security_policy.is_admin
    assert resolve() == (guest, True)
    ctx.source.user_id = 'admin'
    second_admin, reused = resolve()
    assert not reused and second_admin is not guest
    turn._runner.config.platforms[Platform.QQBOT].extra['group_allow_admin_from'] = []
    revoked, reused = resolve()
    assert not reused and not revoked._request_security_policy.is_admin
    assert ctx.history is history and ctx.session_key == 'shared-group'
    assert all(agent._request_security_policy.public_context for agent in built)


@pytest.mark.parametrize('enabled,platform,expected', [(True, Platform.QQBOT, True),
    (False, Platform.QQBOT, False), (True, Platform.TELEGRAM, False)])
def test_construction_scope_is_opt_in_and_platform_specific(monkeypatch, enabled, platform, expected):
    turn = make_turn()
    turn._runner.config.platforms[Platform.QQBOT].extra['role_policy']['enabled'] = enabled
    turn._ctx.source.platform = platform
    seen = []
    def construct(**kwargs):
        seen.append(current_policy())
        return SimpleNamespace()
    turn._ctx.AIAgent = construct
    monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *a: None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda *a: None)
    agent = turn._build_fresh_agent({'model': 'test', 'runtime': {}}, platform.value, '', 10, None, {}, False)
    assert (seen[0] is not None) == expected
    assert agent._request_security_policy == seen[0]
    assert current_policy() is None


def test_turn_scope_uses_current_actor(monkeypatch):
    turn = make_turn()
    runner = TurnRunner.__new__(TurnRunner)
    runner._runner, runner._ctx = turn._runner, turn._ctx
    seen = []
    runner._run_sync_scoped = lambda: seen.append(current_policy()) or {'ok': True}
    monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *a: None)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda *a: None)
    runner.run_sync()
    runner._ctx.source.user_id = 'guest'
    runner.run_sync()
    assert seen[0].is_admin and not seen[1].is_admin
    assert current_policy() is None

@pytest.mark.parametrize('reused', [False, True])
def test_public_history_drops_hidden_execution_and_summaries_without_mutation(monkeypatch, reused):
    from agent.request_security import RequestSecurityPolicy
    from copy import deepcopy
    from agent.context_compressor import LEGACY_SUMMARY_PREFIX
    turn = make_turn()
    turn._ctx.channel_prompt = None
    history = [
        {'role': 'system', 'content': 'private system'},
        {'role': 'user', 'content': 'public question', 'api_content': 'private sidecar'},
        {'role': 'assistant', 'content': 'private pre-tool commentary', 'tool_calls': [{'id': 'tc'}]},
        {'role': 'tool', 'content': 'private filesystem', 'tool_call_id': 'tc'},
        {'role': 'assistant', 'content': 'public answer', 'reasoning': 'private thought',
         'api_content': 'private replay sidecar'},
        {'role': 'user', 'content': 'private summary', '_compressed_summary': True},
        {'role': 'user', 'content': LEGACY_SUMMARY_PREFIX + ' private legacy summary'},
        {'role': 'assistant', 'content': 'hidden summary', 'display_kind': 'context_summary'},
        {'role': 'user', 'content': 'internal event', 'display_kind': 'internal_notification'},
        {'role': 'assistant', 'content': '', 'reasoning_content': 'private reason'},
    ]
    original = deepcopy(history)
    turn._ctx.history = history
    agent = SimpleNamespace(session_id='sid', _session_messages=history + [
        {'role': 'tool', 'content': 'extra private tool'}],
        _request_security_policy=RequestSecurityPolicy('guest', 'qqbot', 'group'))
    runner = TurnRunner.__new__(TurnRunner)
    runner._runner, runner._ctx = turn._runner, turn._ctx
    # Force the DB-lag fallback: hidden live rows must not bypass projection.
    monkeypatch.setattr('gateway.run._select_cached_agent_history', lambda persisted, live: live)
    result, _observed, _ = runner._load_turn_history(agent, reused)
    assert result == [{'role': 'user', 'content': 'public question'},
                      {'role': 'assistant', 'content': 'public answer'}]
    assert history == original
    assert runner._ctx.session_key == 'shared-group'
