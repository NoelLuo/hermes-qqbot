"""Per-plugin, per-profile maintenance; never infer a profile from a background env."""
from __future__ import annotations

import logging
import threading

_LOG = logging.getLogger(__name__)
_INTERVAL_SECONDS = 3600


def _maintenance_loop(base_dir, stop, *, sweep=None, interval=_INTERVAL_SECONDS):
    if sweep is None:
        from tools.guest_workspace_lifecycle import sweep_expired_workspaces
        sweep = sweep_expired_workspaces
    from agent.request_security import policy_scope
    # Retain the served profile context, but no requesting user's tool/media grants.
    with policy_scope(None):
        while not stop.is_set():
            try:
                result = sweep(base_dir=base_dir)
                if result.get('deleted_files', 0):
                    _LOG.info('Guest storage expired-cache sweep: deleted_files=%s', result['deleted_files'])
            except (OSError, RuntimeError, ValueError, TypeError):
                _LOG.warning('Guest storage expired-cache sweep refused or unavailable')
            if stop.wait(interval):
                break


def register(ctx):
    """Start only on a bound session lifecycle event; unload stops this registration."""
    lock = threading.Lock()
    workers = {}

    def start(**kwargs):
        from hermes_constants import get_hermes_home
        from agent.memory_provider import spawn_context_thread
        base = get_hermes_home() / 'workspaces' / 'guests'
        key = str(base)
        with lock:
            existing = workers.get(key)
            if existing and existing[0].is_alive():
                return
            stop = threading.Event()
            thread = spawn_context_thread(
                _maintenance_loop, name='guest-cache-maintenance', args=(base, stop),
            )
            workers[key] = (thread, stop)
            thread.start()

    def shutdown():
        with lock:
            active = list(workers.values())
            workers.clear()
        for thread, stop in active:
            stop.set()
        for thread, _ in active:
            if thread is not threading.current_thread():
                thread.join(timeout=5)

    ctx.register_hook('on_session_start', start)
    ctx.on_unload(shutdown)
