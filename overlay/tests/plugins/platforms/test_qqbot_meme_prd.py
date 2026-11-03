"""Eligible opportunities, not messages or errors, advance durable smoothed odds."""
import math
import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.platforms.qqbot.meme_state import MemeState


def reserve(state, turn='turn', chat='group:room', now=1000, probability=.2, **kwargs):
    return state.claim(turn, chat, now, probability=probability,
                       cooldown=kwargs.get('cooldown', 180), daily_limit=kwargs.get('daily_limit', 30))


def choose(state, turn='turn', fit='natural', now=1000, probability=.2, draw=lambda: .1):
    return state.choose(turn, fit, now, probability=probability, draw=draw)


def misses(state, chat='group:room'):
    with sqlite3.connect(state.path) as db:
        row = db.execute('SELECT misses FROM meme_prd WHERE transport=? AND chat=?',
                         (state.transport, chat)).fetchone()
    return row[0] if row else 0


def test_reservation_does_not_roll_before_semantic_selection(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state)
    assert misses(state) == 0


def test_zero_daily_limit_never_caps_a_chat(tmp_path):
    """0 is the administrator's explicit "no per-chat daily cap"."""
    state = MemeState(tmp_path, 'transport')
    for index in range(120):
        now = 1_000_000 + index
        assert reserve(state, turn=f'turn-{index}', now=now, probability=1, cooldown=0, daily_limit=0)
        assert choose(state, turn=f'turn-{index}', fit='strong', now=now, probability=1)
        state.attempting(f'turn-{index}', 'yes')
        state.finish(f'turn-{index}', 'sent', f'message-{index}')
    # A real positive cap still stops the same chat: all 120 land inside one 24h window.
    assert not reserve(state, turn='capped', now=1_000_200, probability=1, cooldown=0, daily_limit=120)


def test_natural_miss_has_no_cooldown_and_next_odds_increase(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state)
    assert not choose(state)  # .1 exceeds the first ~.0557 chance
    assert misses(state) == 1
    assert reserve(state, turn='next', now=1001)
    assert choose(state, turn='next', now=1001)  # now ~.1114
    assert not choose(state, turn='next', now=1001)
    state.attempting('next', 'yes')
    state.finish('next', 'sent', 'real-platform-id')
    assert misses(state) == 0
    assert not reserve(state, turn='cooling', now=1002)
    assert misses(state) == 0


def test_strong_bypasses_rng_and_only_confirmed_send_resets(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state)
    assert not choose(state)
    assert reserve(state, turn='strong', now=1001)
    assert choose(state, turn='strong', fit='strong', now=1001,
                  draw=lambda: pytest.fail('Strong fit must not roll'))
    assert misses(state) == 1
    state.attempting('strong', 'yes')
    state.finish('strong', 'sent', 'confirmed-id')
    assert misses(state) == 0


def test_restart_preserves_odds_and_never_rerolls_a_missed_turn(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state)
    assert not choose(state)
    restored = MemeState(tmp_path, 'transport')
    assert misses(restored) == 1
    assert not reserve(restored)
    assert not choose(restored, draw=lambda: pytest.fail('No duplicate draw'))
    assert reserve(restored, turn='new', now=1001)
    assert choose(restored, turn='new', now=1001)


@pytest.mark.parametrize('chat,transport', [('group:elsewhere', 'transport'),
                                         ('dm:room', 'transport'), ('group:room', 'other-bot')])
def test_progress_is_not_shared_between_chats_or_transports(tmp_path, chat, transport):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state) and not choose(state)
    other = MemeState(tmp_path, transport)
    assert reserve(other, turn='other', chat=chat)
    assert not choose(other, turn='other')
    assert misses(other, chat) == 1
    assert misses(state) == 1


@pytest.mark.parametrize('outcome', ['none', 'error'])
def test_no_match_or_technical_failure_neither_adds_nor_erases_progress(tmp_path, outcome):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state) and not choose(state)
    assert reserve(state, turn='no-gif', now=1001)
    state.finish('no-gif', outcome)
    assert misses(state) == 1


@pytest.mark.parametrize('outcome', ['error', 'unknown'])
def test_failed_upload_or_unknown_ack_does_not_change_progress(tmp_path, outcome):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state) and not choose(state)
    assert reserve(state, turn='failed', now=1001)
    assert choose(state, turn='failed', now=1001)
    if outcome == 'unknown':
        state.attempting('failed', 'yes')
    state.finish('failed', outcome)
    assert misses(state) == 1
    assert not reserve(state, turn='blocked', now=1002)
    assert misses(state) == 1


def test_concurrent_choice_for_same_turn_advances_only_once(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert not any(pool.map(lambda _: choose(state), range(8)))
    assert misses(state) == 1


def test_target_rate_change_starts_a_new_curve(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert reserve(state) and not choose(state)
    assert reserve(state, turn='new-rate', now=1001, probability=.5)
    assert not choose(state, turn='new-rate', now=1001, probability=.5, draw=lambda: .4)
    assert misses(state) == 1


def test_zero_probability_is_an_explicit_off_switch_even_for_strong(tmp_path):
    state = MemeState(tmp_path, 'transport')
    assert not reserve(state, probability=0)
    assert not choose(state, fit='strong', probability=0, draw=lambda: pytest.fail('Off'))
    assert misses(state) == 0


def test_old_database_and_delivery_history_are_preserved(tmp_path):
    state = MemeState(tmp_path, 'transport')
    state.path.parent.mkdir(parents=True)
    with sqlite3.connect(state.path) as db:
        db.execute('''CREATE TABLE decisions (transport TEXT NOT NULL, turn_id TEXT NOT NULL,
            chat TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, meme_id TEXT,
            message_id TEXT, PRIMARY KEY (transport, turn_id))''')
        db.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?,?)',
                   ('transport', 'old', 'group:room', 'sent', 999, 'yes', 'old-real-id'))
    assert state.recent('group:room', 3) == ('yes',)
    assert not reserve(state, turn='old')
    assert not reserve(state, turn='still-cooling')
    assert misses(state) == 0
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT message_id FROM decisions WHERE turn_id='old'").fetchone() == ('old-real-id',)


@pytest.mark.parametrize('rate', [.01, .1, .2, .5, .9])
def test_calibration_matches_long_run_target_not_the_initial_chance(rate):
    from plugins.platforms.qqbot.meme_state import prd_step
    step = prd_step(rate)
    survival, expected, attempt = 1., 0., 0
    while survival > 1e-14:
        expected += survival
        attempt += 1
        survival *= 1 - min(1, attempt * step)
    assert 1 / expected == pytest.approx(rate, rel=1e-9)
    assert 0 < step <= rate


def test_seeded_simulation_is_close_to_twenty_percent_and_limits_dry_streaks():
    from plugins.platforms.qqbot.meme_state import prd_step
    step, rng, failures, hits, longest = prd_step(.2), random.Random(723), 0, 0, 0
    trials = 100000
    for _ in range(trials):
        if rng.random() < min(1, (failures + 1) * step):
            hits += 1
            failures = 0
        else:
            failures += 1
            longest = max(longest, failures)
    assert hits / trials == pytest.approx(.2, abs=.003)
    assert longest < math.ceil(1 / step)


@pytest.mark.parametrize('rate', [0, 1, 1e-6, 1e-100])
def test_boundary_and_rare_rates_remain_finite_and_bounded(rate):
    from plugins.platforms.qqbot.meme_state import prd_step
    step = prd_step(rate)
    assert math.isfinite(step) and 0 <= step <= rate
    if rate in (0, 1):
        assert step == rate


@pytest.mark.parametrize('rate', [True, float('nan'), float('inf'), -.1, 1.1])
def test_invalid_rates_fail_closed(rate):
    from plugins.platforms.qqbot.meme_state import prd_step
    with pytest.raises(ValueError):
        prd_step(rate)
