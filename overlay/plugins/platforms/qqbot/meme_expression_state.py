"""Bound intent and outcome facts; no chat text and no automatic replay."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager, closing

logger = logging.getLogger(__name__)

_OUTCOMES = {'selected', 'attempting', 'sent', 'unknown', 'blocked', 'not_selected', 'error'}


class ExpressionJournal:
    def __init__(self, state):
        self.state = state

    @contextmanager
    def _db(self):
        with self.state._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS expressions (
                transport TEXT NOT NULL, turn_id TEXT NOT NULL, chat TEXT NOT NULL,
                session_id TEXT NOT NULL, meme_id TEXT NOT NULL, description TEXT NOT NULL,
                outcome TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL,
                message_id TEXT, PRIMARY KEY (transport, turn_id))''')
            db.execute('CREATE INDEX IF NOT EXISTS expression_context ON expressions (transport, chat, session_id, created)')
            db.commit()
            yield db

    def add(self, turn_id, chat, session_id, meme_id, description, now):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM expressions WHERE created < ?', (now - 30 * 86400,))
            db.execute('''DELETE FROM expressions WHERE rowid IN (
                SELECT rowid FROM expressions ORDER BY created DESC LIMIT -1 OFFSET 10000)''')
            changed = db.execute('INSERT OR IGNORE INTO expressions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (self.state.transport, turn_id, chat, session_id, meme_id, description,
                 'selected', '', now, None)).rowcount
            db.commit()
            return changed == 1

    def finish(self, turn_id, outcome, reason='', message_id=None):
        if outcome not in _OUTCOMES - {'selected'}:
            raise ValueError('Invalid expression outcome')
        if outcome == 'sent' and (not isinstance(message_id, str) or not message_id.strip()):
            raise ValueError('Real expression receipt required')
        allowed = ('attempting',) if outcome in {'sent', 'unknown'} else ('selected',)
        with self._db() as db:
            placeholders = ','.join('?' for _ in allowed)
            changed = db.execute(f'''UPDATE expressions SET outcome=?,reason=?,message_id=?
                WHERE transport=? AND turn_id=? AND outcome IN ({placeholders})''',
                (outcome, reason, message_id, self.state.transport, turn_id, *allowed)).rowcount
            db.commit()
            return changed == 1

    def availability(self, chat, now, settings):
        with self.state._db() as db:
            last = db.execute('''SELECT MAX(created) FROM decisions WHERE transport=? AND chat=?
                AND state IN ('deciding','ready','attempting','sent','unknown','error')''',
                (self.state.transport, chat)).fetchone()[0]
            busy = db.execute('''SELECT 1 FROM decisions WHERE transport=? AND chat=? AND created>=?
                AND state IN ('deciding','ready','attempting') LIMIT 1''',
                (self.state.transport, chat, now - 30)).fetchone()
            count = db.execute('''SELECT COUNT(*) FROM decisions WHERE transport=? AND chat=? AND created>=?
                AND state IN ('attempting','sent','unknown')''',
                (self.state.transport, chat, now - 86400)).fetchone()[0]
        if settings.probability == 0:
            return 'disabled'
        if busy or (last is not None and now - last < settings.cooldown):
            return 'cooldown'
        if settings.daily_limit > 0 and count >= settings.daily_limit:
            return 'daily_limit'
        return 'available'

    def start_send(self, turn_id):
        """Commit both journals before crossing the network boundary, or neither."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT meme_id FROM expressions
                WHERE transport=? AND turn_id=? AND outcome='selected' ''',
                (self.state.transport, turn_id)).fetchone()
            if row is None:
                db.rollback()
                return False
            changed = db.execute('''UPDATE decisions SET state='attempting',meme_id=?
                WHERE transport=? AND turn_id=? AND state='ready' ''',
                (row[0], self.state.transport, turn_id)).rowcount
            if changed != 1:
                db.rollback()
                return False
            db.execute('''UPDATE expressions SET outcome='attempting'
                WHERE transport=? AND turn_id=? AND outcome='selected' ''',
                (self.state.transport, turn_id))
            db.commit()
            return True

    def end_send(self, turn_id, outcome, message_id=None):
        if outcome not in {'sent', 'unknown'}:
            raise ValueError('Invalid send outcome')
        if outcome == 'sent' and (not isinstance(message_id, str) or not message_id.strip()):
            raise ValueError('Real expression receipt required')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('''UPDATE expressions SET outcome=?,message_id=?
                WHERE transport=? AND turn_id=? AND outcome='attempting' ''',
                (outcome, message_id, self.state.transport, turn_id)).rowcount
            if changed != 1:
                db.rollback()
                return False
            db.execute('''UPDATE decisions SET state=?,message_id=?
                WHERE transport=? AND turn_id=? AND state='attempting' ''',
                (outcome, message_id, self.state.transport, turn_id))
            if outcome == 'sent':
                db.execute('''DELETE FROM meme_prd WHERE transport=? AND chat=(
                    SELECT chat FROM expressions WHERE transport=? AND turn_id=?)''',
                    (self.state.transport, self.state.transport, turn_id))
            db.commit()
            return True

    def _lineage(self, session_id):
        """Read only actual compression parents; never follow /new, branch or delegate forks."""
        from hermes_state_common import _non_continuation_child_sql
        sessions = [session_id]
        path = self.state.path.parent.parent.parent / 'state.db'
        try:
            if not path.is_file():
                return sessions
            edge = _non_continuation_child_sql('child.', 'parent.id')
            sql = ('''SELECT parent.id FROM sessions child JOIN sessions parent
                ON parent.id=child.parent_session_id WHERE child.id=? AND parent.end_reason='compression'
                AND COALESCE(child.session_key,'')=COALESCE(parent.session_key,'')
                AND child.source='qqbot' AND parent.source='qqbot' ''' + edge)
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                for _ in range(31):
                    row = db.execute(sql, (sessions[-1],)).fetchone()
                    if row is None or row[0] in sessions:
                        break
                    sessions.append(row[0])
        except (OSError, sqlite3.Error) as exc:
            logger.warning('QQ expression compression lineage unavailable: %s', type(exc).__name__)
        return sessions

    def recent_note(self, chat, session_id):
        lineage = self._lineage(session_id)
        placeholders = ','.join('?' for _ in lineage)
        with self._db() as db:
            rows = db.execute(f'''SELECT meme_id,description,outcome,reason FROM expressions
                WHERE transport=? AND chat=? AND session_id IN ({placeholders})
                ORDER BY created DESC,rowid DESC LIMIT 3''',
                (self.state.transport, chat, *lineage)).fetchall()
        if not rows:
            return ''
        facts = [{'id': identifier, 'description': description, 'outcome': outcome, 'reason': reason}
                 for identifier, description, outcome, reason in reversed(rows)]
        return ('[本机QQ表情发送事实/v1]\n'
                '以下由当前会话的本机出站记录提供，不是对方发言。sent表示有真实平台成功回执；'
                'selected/attempting/unknown均未确认送达，不可声称已经发出，也不要自动重发；'
                'blocked/error/not_selected表示没有完成发送。只把sent记录当作“刚才发的图”。\n'
                + json.dumps(facts, ensure_ascii=False, separators=(',', ':'))
                + '\n[/本机QQ表情发送事实/v1]')
