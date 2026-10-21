"""Profile-bound outgoing receipts and explicitly invoked, own-message group recall.

The trusted adapter supplies only the *actual* POST response id, never a SendResult
fallback. A logical id identifies a local multipart operation, not a QQ message.
No content, raw API payloads, credentials or exception strings enter this journal.
The caller owns authorization and HTTP decoding (including successful empty bodies).
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote


class ReceiptScopeError(ValueError):
    """Unknown receipt or mismatched owning profile, transport, or chat."""


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value  # opaque identifiers must not be stripped or coerced


@dataclass(frozen=True)
class DeliveryReceipt:
    logical_id: str
    chat_id: str
    chat_type: str
    transport_owner: str
    part_index: int
    message_id: str | None
    delivery_status: str
    recall_status: str
    http_status: int | None
    recall_attempts: int
    source_session_key: str | None = None
    source_message_id: str | None = None
    source_quote_id: str | None = None


@dataclass(frozen=True)
class RecallHTTPResult:
    """Normalized HTTP outcome; a 2xx alone is not API acknowledgement.

    The injected transport must explicitly set acknowledged=True after checking
    the API response, including HTTP 200 with an empty body. None means uncertain.
    HTTP failures use their status code and acknowledged=False. No raw body needed.
    """
    status_code: int | None
    acknowledged: bool | None = None


class OutgoingReceiptJournal:
    """One explicit profile root/owner, with separately bound bot transport identity.

    ``profile_root`` must be the owning profile's absolute data root, not ambient
    HERMES_HOME. ``owner`` is its stable nonsecret identity; ``transport_owner`` is
    a stable nonsecret account/endpoint identity (e.g. app-id + production/sandbox).
    A copied DB, changed owner, or different account cannot authorize old receipts.
    Each public write commits synchronously; no global/shared connection is kept.
    """

    def __init__(self, profile_root: Path, *, owner: str, transport_owner: str):
        root = Path(profile_root)
        if not root.is_absolute():
            raise ValueError("profile_root must be absolute")
        self.profile_root = root.resolve()
        self.owner = _identifier(owner, "owner")
        self.transport_owner = _identifier(transport_owner, "transport_owner")
        self.path = self.profile_root / "cache" / "qqbot" / "outgoing_receipts.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS journal_owner (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    owner TEXT NOT NULL, profile_root TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sends (
                    logical_id TEXT PRIMARY KEY, transport_owner TEXT NOT NULL,
                    chat_id TEXT NOT NULL, chat_type TEXT NOT NULL,
                    created_at REAL NOT NULL, source_session_key TEXT, source_message_id TEXT, source_quote_id TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    logical_id TEXT NOT NULL REFERENCES sends(logical_id),
                    part_index INTEGER NOT NULL CHECK(part_index >= 0),
                    message_id TEXT, delivery_status TEXT NOT NULL,
                    recall_status TEXT NOT NULL DEFAULT 'not_requested',
                    http_status INTEGER, recall_attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(logical_id, part_index)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if 'source_quote_id' not in {row[1] for row in db.execute('PRAGMA table_info(sends)')}:
                db.execute('ALTER TABLE sends ADD COLUMN source_quote_id TEXT')
            db.execute("INSERT OR IGNORE INTO journal_owner VALUES (1, ?, ?)",
                       (self.owner, str(self.profile_root)))
            self._check_owner(db)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _check_owner(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT owner, profile_root FROM journal_owner WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (self.owner, str(self.profile_root)):
            raise ReceiptScopeError("journal owner/root mismatch")

    def begin_send(self, *, chat_id: str, chat_type: str = "group",
                   source_session_key: str | None = None, source_message_id: str | None = None,
                   logical_id: str | None = None, source_quote_id: str | None = None) -> str:
        _identifier(chat_id, "chat_id")
        for name, value in (("source_session_key", source_session_key), ("source_message_id", source_message_id),
                            ("source_quote_id", source_quote_id)):
            if value is not None:
                _identifier(value, name)
        if chat_type not in {"group", "c2c", "guild"}:
            raise ValueError("unsupported chat_type")
        logical_id = uuid.uuid4().hex if logical_id is None else _identifier(logical_id, "logical_id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_owner(db)
            existing = db.execute("SELECT transport_owner, chat_id, chat_type, source_session_key, source_message_id, source_quote_id "
                                  "FROM sends WHERE logical_id=?", (logical_id,)).fetchone()
            expected = (self.transport_owner, chat_id, chat_type, source_session_key, source_message_id, source_quote_id)
            if existing is not None:
                if tuple(existing) != expected:
                    raise ReceiptScopeError("logical send binding mismatch")
            else:
                db.execute("INSERT INTO sends VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (logical_id, self.transport_owner, chat_id, chat_type, time.time(),
                            source_session_key, source_message_id, source_quote_id))
        return logical_id

    def _send(self, db: sqlite3.Connection, logical_id: str, chat_id: str) -> sqlite3.Row:
        self._check_owner(db)
        row = db.execute("SELECT * FROM sends WHERE logical_id=? AND chat_id=? AND transport_owner=?",
                         (logical_id, chat_id, self.transport_owner)).fetchone()
        if row is None:
            raise ReceiptScopeError("unknown send in this chat/transport")
        return row

    def record_part(self, logical_id: str, *, chat_id: str, part_index: int,
                    returned_message_id: object = None, success: bool | None = True) -> DeliveryReceipt:
        """Commit one part immediately, even when a later part fails.

        success=None denotes an uncertain POST outcome; False a known failure.
        Only nonblank string IDs on successful POSTs are usable; malformed/missing
        IDs stay NULL/unknown, never str(None), a local uuid, or an input/reply id.
        Parts are immutable: retries must not replace a previously known receipt.
        """
        if type(part_index) is not int or part_index < 0:
            raise ValueError("part_index must be a nonnegative integer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._send(db, logical_id, chat_id)
            self._insert_part(db, logical_id, part_index, returned_message_id, success)
        return next(r for r in self.lookup(logical_id, chat_id=chat_id) if r.part_index == part_index)

    @staticmethod
    def _insert_part(db, logical_id, part_index, returned_message_id, success):
        if success is not None and type(success) is not bool:
            raise ValueError("success must be bool or None")
        message_id = (returned_message_id if success is True and
                      isinstance(returned_message_id, str) and returned_message_id.strip() else None)
        status = "error" if success is False else ("known" if message_id is not None else "unknown")
        old = db.execute("SELECT message_id, delivery_status FROM receipts WHERE logical_id=? AND part_index=?",
                         (logical_id, part_index)).fetchone()
        if old is not None and tuple(old) != (message_id, status):
            raise ValueError("part receipt already recorded; cannot overwrite")
        db.execute("""INSERT OR IGNORE INTO receipts
            (logical_id, part_index, message_id, delivery_status, updated_at) VALUES (?, ?, ?, ?, ?)""",
                   (logical_id, part_index, message_id, status, time.time()))

    def append_part(self, logical_id: str, *, chat_id: str, returned_message_id: object = None,
                    success: bool | None = True) -> DeliveryReceipt:
        """Atomically append from _post_message, across text/media/keyboard sends.

        Reuse the turn's ContextVar logical_id; callers must NOT derive part indexes
        by counting an earlier lookup. Separate processes/connections allocate unique
        indexes here. Every invocation is a new part, not an idempotent POST retry.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._send(db, logical_id, chat_id)
            part_index = db.execute("SELECT COALESCE(MAX(part_index), -1)+1 FROM receipts WHERE logical_id=?",
                                    (logical_id,)).fetchone()[0]
            self._insert_part(db, logical_id, part_index, returned_message_id, success)
        return next(r for r in self.lookup(logical_id, chat_id=chat_id) if r.part_index == part_index)

    def lookup(self, logical_id: str, *, chat_id: str) -> tuple[DeliveryReceipt, ...]:
        """Exact logical-id AND chat lookup, ordered by part; never prefix/latest."""
        with self._connect() as db:
            send = self._send(db, logical_id, chat_id)
            rows = db.execute("SELECT * FROM receipts WHERE logical_id=? ORDER BY part_index",
                              (logical_id,)).fetchall()
        return tuple(DeliveryReceipt(logical_id, chat_id, send["chat_type"], self.transport_owner,
                                     r["part_index"], r["message_id"], r["delivery_status"],
                                     r["recall_status"], r["http_status"], r["recall_attempts"],
                                     send["source_session_key"], send["source_message_id"], send["source_quote_id"]) for r in rows)

    def finish_part(self, logical_id: str, *, chat_id: str, part_index: int,
                    returned_message_id: object = None, success: bool | None = True) -> DeliveryReceipt:
        """Complete a pre-I/O unknown record; known acknowledgements are immutable."""
        if success is not None and type(success) is not bool:
            raise ValueError("success must be bool or None")
        mid = returned_message_id if success is True and isinstance(returned_message_id, str) and returned_message_id.strip() else None
        status = "error" if success is False else ("known" if mid is not None else "unknown")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._send(db, logical_id, chat_id)
            old = db.execute("SELECT message_id, delivery_status FROM receipts WHERE logical_id=? AND part_index=?",
                             (logical_id, part_index)).fetchone()
            if old is None or (old["delivery_status"] != "unknown" and tuple(old) != (mid, status)):
                raise ReceiptScopeError("unknown or immutable part receipt")
            db.execute("UPDATE receipts SET message_id=?, delivery_status=?, updated_at=? WHERE logical_id=? AND part_index=?",
                       (mid, status, time.time(), logical_id, part_index))
        return next(r for r in self.lookup(logical_id, chat_id=chat_id) if r.part_index == part_index)

    def lookup_source(self, *, chat_id: str, source_session_key: str,
                      source_message_id: str) -> tuple[DeliveryReceipt, ...]:
        """Resolve a persisted local turn's inbound linkage, never by latest/body guessing."""
        _identifier(source_session_key, "source_session_key")
        _identifier(source_message_id, "source_message_id")
        with self._connect() as db:
            self._check_owner(db)
            ids = db.execute("SELECT logical_id FROM sends WHERE chat_id=? AND transport_owner=? "
                             "AND source_session_key=? AND source_message_id=? ORDER BY created_at, logical_id",
                             (chat_id, self.transport_owner, source_session_key, source_message_id)).fetchall()
        return tuple(receipt for row in ids for receipt in self.lookup(row[0], chat_id=chat_id))

    def _claim_recall(self, logical_id: str, group_id: str, message_id: str,
                      retry_unknown: bool) -> DeliveryReceipt:
        _identifier(message_id, "message_id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            send = self._send(db, logical_id, group_id)
            if send["chat_type"] != "group":
                raise ReceiptScopeError("recall supports only own known group receipts")
            rows = db.execute("""SELECT * FROM receipts WHERE logical_id=? AND message_id=?
                                 AND delivery_status='known'""", (logical_id, message_id)).fetchall()
            if len(rows) != 1:
                raise ReceiptScopeError("unknown or ambiguous own message receipt")
            row = rows[0]
            state = row["recall_status"]
            if state == "transport_unknown" and not retry_unknown:
                raise ReceiptScopeError("previous recall uncertain; explicit retry_unknown required")
            if state != "api_acknowledged":
                # Persist uncertainty BEFORE I/O: cancellation/process death cannot imply success.
                db.execute("""UPDATE receipts SET recall_status='transport_unknown', http_status=NULL,
                              recall_attempts=recall_attempts+1, updated_at=?
                              WHERE logical_id=? AND part_index=?""",
                           (time.time(), logical_id, row["part_index"]))
        return next(r for r in self.lookup(logical_id, chat_id=group_id) if r.part_index == row["part_index"])

    def _finish_recall(self, receipt: DeliveryReceipt, status: str, http_status: int | None) -> DeliveryReceipt:
        with self._connect() as db:
            self._send(db, receipt.logical_id, receipt.chat_id)
            db.execute("""UPDATE receipts SET recall_status=?, http_status=?, updated_at=?
                          WHERE logical_id=? AND part_index=? AND recall_attempts=?""",
                       (status, http_status, time.time(), receipt.logical_id,
                        receipt.part_index, receipt.recall_attempts))
        return next(r for r in self.lookup(receipt.logical_id, chat_id=receipt.chat_id)
                    if r.part_index == receipt.part_index)


class GroupRecallService:
    """No model tool registration, automatic recall, age cutoff, or inbound-ID API.

    ``delete_transport(method, path)`` is async and returns RecallHTTPResult. It
    must be bound to the SAME account/environment as transport_owner; this service
    never obtains credentials. API acknowledgement does not prove client removal.
    """

    def __init__(self, journal: OutgoingReceiptJournal, *, owner: str, transport_owner: str,
                 delete_transport: Callable[[str, str], Awaitable[RecallHTTPResult]]):
        if owner != journal.owner or transport_owner != journal.transport_owner:
            raise ReceiptScopeError("recall service owner/transport mismatch")
        self.journal = journal
        self.delete_transport = delete_transport

    async def recall(self, logical_id: str, *, group_id: str, message_id: str,
                     retry_unknown: bool = False) -> DeliveryReceipt:
        receipt = self.journal._claim_recall(logical_id, group_id, message_id, retry_unknown)
        if receipt.recall_status == "api_acknowledged":
            return receipt
        path = f"/v2/groups/{quote(group_id, safe='')}/messages/{quote(message_id, safe='')}"
        try:
            result = await self.delete_transport("DELETE", path)
        except Exception:
            # Never persist exceptions: HTTP libraries may embed URLs/headers/body.
            return self.journal._finish_recall(receipt, "transport_unknown", None)
        status, code = "transport_unknown", None
        if isinstance(result, RecallHTTPResult):
            code = result.status_code
            if type(code) is not int or not 100 <= code <= 599:
                code = None
            if code is not None and (not 200 <= code < 300 or result.acknowledged is False):
                status = "api_error"
            elif code is not None and result.acknowledged is True:
                status = "api_acknowledged"
        return self.journal._finish_recall(receipt, status, code)
