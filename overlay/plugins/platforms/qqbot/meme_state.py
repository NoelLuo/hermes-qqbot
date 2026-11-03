"""Small, bounded decision journal; optional memes are never replayed after a crash."""
from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=128, typed=True)
def prd_step(probability):
    """Calibrate P(hit on eligible trial n)=min(1,n*C) to the target renewal rate."""
    if (type(probability) not in (int, float) or not math.isfinite(probability)
            or not 0 <= probability <= 1):
        raise ValueError('Invalid PRD target')
    if probability in (0, 1):
        return probability
    if probability < .001:
        # Rare-rate asymptotic: E[T] = sqrt(pi/(2*C)) - 1/3 + O(sqrt(C)).
        # Avoid unbounded work for a tiny administrator-configured probability.
        adjusted = probability / (1 + probability / 3)
        return (math.pi / 2) * adjusted * adjusted
    lo, hi = 0., probability
    for _ in range(64):
        step = (lo + hi) / 2
        survival, expected, attempt = 1., 0., 0
        while survival > 1e-14:
            expected += survival
            attempt += 1
            survival *= 1 - min(1, attempt * step)
        if 1 / expected < probability:
            lo = step
        else:
            hi = step
    return (lo + hi) / 2


class MemeState:
    def __init__(self, home: Path, transport: str):
        self.path = home / "cache" / "qqbot" / "memes.sqlite3"
        self.transport = transport

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS decisions (
                transport TEXT NOT NULL, turn_id TEXT NOT NULL, chat TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, meme_id TEXT, message_id TEXT,
                PRIMARY KEY (transport, turn_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS meme_chat_time ON decisions (transport, chat, created)")
            db.execute("""CREATE TABLE IF NOT EXISTS meme_prd (
                transport TEXT NOT NULL, chat TEXT NOT NULL, probability REAL NOT NULL,
                misses INTEGER NOT NULL CHECK(misses >= 0), updated REAL NOT NULL,
                PRIMARY KEY (transport, chat))""")
            db.commit()
            yield db
        finally:
            db.close()

    def claim(self, turn_id, chat, now, *, probability, cooldown, daily_limit) -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM decisions WHERE created < ?", (now - 30 * 86400,))
            # A bounded dedup window, not an ever-growing history of conversation text.
            db.execute("""DELETE FROM decisions WHERE rowid IN (
                SELECT rowid FROM decisions ORDER BY created DESC LIMIT -1 OFFSET 10000)""")
            db.execute('DELETE FROM meme_prd WHERE updated < ?', (now - 30 * 86400,))
            db.execute("""DELETE FROM meme_prd WHERE rowid IN (
                SELECT rowid FROM meme_prd ORDER BY updated DESC LIMIT -1 OFFSET 10000)""")
            if db.execute("SELECT 1 FROM decisions WHERE transport=? AND turn_id=?",
                          (self.transport, turn_id)).fetchone():
                db.rollback()
                return False
            cooling = db.execute("""SELECT 1 FROM decisions WHERE transport=? AND chat=?
                AND created >= ? AND state IN ('deciding','ready','attempting','sent','unknown','error') LIMIT 1""",
                (self.transport, chat, now - cooldown)).fetchone()
            count = db.execute("""SELECT COUNT(*) FROM decisions WHERE transport=? AND chat=?
                AND created >= ? AND state IN ('attempting','sent','unknown')""",
                (self.transport, chat, now - 86400)).fetchone()[0]
            # Even a zero configured cooldown must not allow overlapping model/send work.
            busy = db.execute("""SELECT 1 FROM decisions WHERE transport=? AND chat=?
                AND created >= ? AND state IN ('deciding','ready','attempting') LIMIT 1""",
                (self.transport, chat, now - 30)).fetchone()
            under_cap = daily_limit <= 0 or count < daily_limit
            selected = not cooling and not busy and under_cap and probability > 0
            db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                       (self.transport, turn_id, chat, "deciding" if selected else "skip", now, None, None))
            db.commit()
            return bool(selected)

    def choose(self, turn_id, fit, now, *, probability, draw) -> bool:
        step = prd_step(probability)
        if fit not in ('natural', 'strong'):
            raise ValueError('Invalid meme suitability')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT chat, created FROM decisions WHERE transport=? AND turn_id=?
                AND state='deciding'""", (self.transport, turn_id)).fetchone()
            if row is None or probability <= 0 or now - row[1] >= 30:
                db.rollback()
                return False
            chat = row[0]
            selected = fit == 'strong'
            if not selected:
                previous = db.execute('SELECT probability, misses FROM meme_prd WHERE transport=? AND chat=?',
                                      (self.transport, chat)).fetchone()
                failures = previous[1] if previous and previous[0] == probability else 0
                value = draw()
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 1:
                    raise ValueError('Invalid meme luck draw')
                selected = value < min(1, (failures + 1) * step)
                if not selected:
                    db.execute('INSERT OR REPLACE INTO meme_prd VALUES (?,?,?,?,?)',
                               (self.transport, chat, probability, failures + 1, now))
            db.execute('UPDATE decisions SET state=? WHERE transport=? AND turn_id=?',
                       ('ready' if selected else 'miss', self.transport, turn_id))
            db.commit()
            return selected

    def recent(self, chat, limit) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        with self._db() as db:
            return tuple(row[0] for row in db.execute("""SELECT meme_id FROM decisions
                WHERE transport=? AND chat=? AND state='sent' AND meme_id IS NOT NULL
                ORDER BY created DESC LIMIT ?""", (self.transport, chat, limit)))

    def attempting(self, turn_id, meme_id):
        with self._db() as db:
            changed = db.execute("""UPDATE decisions SET state='attempting', meme_id=?
                WHERE transport=? AND turn_id=? AND state='ready'""",
                (meme_id, self.transport, turn_id)).rowcount
            db.commit()
            if changed != 1:
                raise ValueError("Meme decision is not owned by this attempt")

    def finish(self, turn_id, state, message_id=None):
        if state not in {"none", "sent", "error", "unknown"}:
            raise ValueError("Invalid meme outcome")
        if state == "sent" and (not isinstance(message_id, str) or not message_id.strip()):
            raise ValueError("A real message ID is required")
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT chat, state FROM decisions WHERE transport=? AND turn_id=?
                AND state IN ('deciding','ready','attempting')""", (self.transport, turn_id)).fetchone()
            if row is None:
                db.rollback()
                return
            if state in {'sent', 'unknown'} and row[1] != 'attempting':
                raise ValueError('Meme send was not attempted')
            db.execute("""UPDATE decisions SET state=?, message_id=? WHERE transport=?
                AND turn_id=? AND state IN ('deciding','ready','attempting')""",
                (state, message_id, self.transport, turn_id))
            if state == 'sent':
                db.execute('DELETE FROM meme_prd WHERE transport=? AND chat=?', (self.transport, row[0]))
            db.commit()
