"""Maintenance scheduling is profile-bound, stoppable and cache-only via the store helper."""
import threading
from types import SimpleNamespace

from agent.request_security import RequestSecurityPolicy, current_policy, policy_scope
from tools import guest_workspace_maintenance as maintenance


def test_loop_uses_explicit_base_and_drops_request_grants(tmp_path):
    stop = threading.Event()
    seen = []

    def sweep(*, base_dir):
        seen.append((base_dir, current_policy()))
        stop.set()
        return {'deleted_files': 0}

    p = RequestSecurityPolicy('visitor', 'qqbot', 'room')
    with policy_scope(p):
        maintenance._maintenance_loop(tmp_path, stop, sweep=sweep)
        assert current_policy() is p
    assert seen == [(tmp_path, None)]


def test_registration_resolves_home_at_event_time_and_unload_stops_workers(tmp_path, monkeypatch):
    hooks, cleanup, threads = {}, [], []
    home = [tmp_path / 'profile-one']
    monkeypatch.setattr('hermes_constants.get_hermes_home', lambda: home[0])

    class Thread:
        def __init__(self, target, name, args):
            self.target, self.args = target, args
            self.alive = False
            self.joined = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def join(self, *, timeout):
            self.joined = True

    def spawn(target, *, name, args):
        thread = Thread(target, name, args)
        threads.append(thread)
        return thread

    monkeypatch.setattr('agent.memory_provider.spawn_context_thread', spawn)
    ctx = SimpleNamespace(register_hook=lambda name, fn: hooks.update({name: fn}),
                          on_unload=lambda fn: cleanup.append(fn))
    maintenance.register(ctx)
    assert not threads
    hooks['on_session_start'](session_id='one')
    hooks['on_session_start'](session_id='two')
    assert len(threads) == 1
    home[0] = tmp_path / 'profile-two'
    hooks['on_session_start'](session_id='three')
    assert len(threads) == 2
    assert threads[0].args[0] == tmp_path / 'profile-one/workspaces/guests'
    assert threads[1].args[0] == tmp_path / 'profile-two/workspaces/guests'
    cleanup[0]()
    assert all(t.args[1].is_set() and t.joined for t in threads)


def test_sweep_failure_does_not_leak_request_or_loop_unbounded(tmp_path):
    class Stop:
        def __init__(self):
            self.waited = []

        def is_set(self):
            return False

        def wait(self, interval):
            self.waited.append(interval)
            return True

    stop = Stop()

    def fail(**kw):
        raise OSError('test fixture')

    maintenance._maintenance_loop(tmp_path, stop, sweep=fail, interval=3600)
    assert stop.waited == [3600]


def test_default_loop_resolves_real_lifecycle_sweep(tmp_path):
    stop = SimpleNamespace(is_set=lambda: False, wait=lambda interval: True)
    base = tmp_path / 'missing-profile-storage'
    maintenance._maintenance_loop(base, stop)
    assert not base.exists()
