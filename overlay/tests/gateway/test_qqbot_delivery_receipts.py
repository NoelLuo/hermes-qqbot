"""Offline receipt durability, profile boundaries and injected group recall contracts."""
import asyncio
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.qqbot.delivery_receipts import (
    GroupRecallService,
    OutgoingReceiptJournal,
    RecallHTTPResult,
    ReceiptScopeError,
)


@pytest.fixture
def journal(tmp_path):
    return OutgoingReceiptJournal(tmp_path / "profile-a", owner="profile-a", transport_owner="app:prod")


def reopen(journal, **overrides):
    return OutgoingReceiptJournal(journal.profile_root, owner=overrides.get("owner", journal.owner),
                                  transport_owner=overrides.get("transport_owner", journal.transport_owner))


def known(journal, *, group="group", message="wire-id", chat_type="group"):
    logical = journal.begin_send(chat_id=group, chat_type=chat_type)
    journal.record_part(logical, chat_id=group, part_index=0, returned_message_id=message)
    return logical


def service(journal, transport):
    return GroupRecallService(journal, owner=journal.owner, transport_owner=journal.transport_owner,
                              delete_transport=transport)


def test_multipart_partial_and_missing_receipts_survive_restart(journal):
    logical = journal.begin_send(chat_id="group")
    first = journal.record_part(logical, chat_id="group", part_index=0, returned_message_id="real-0")
    journal.record_part(logical, chat_id="group", part_index=1)
    journal.record_part(logical, chat_id="group", part_index=2, success=False)
    rows = reopen(journal).lookup(logical, chat_id="group")
    assert first.logical_id == logical != first.message_id
    assert [(r.part_index, r.message_id, r.delivery_status) for r in rows] == [
        (0, "real-0", "known"), (1, None, "unknown"), (2, None, "error")]
    assert all(r.chat_id == "group" and r.transport_owner == "app:prod" for r in rows)


def test_append_across_calls_and_restart_keeps_source_link(journal):
    logical = journal.begin_send(chat_id="group", source_session_key="session:group",
                                 source_message_id="inbound-source")
    first = journal.append_part(logical, chat_id="group", returned_message_id="text")
    second = reopen(journal).append_part(logical, chat_id="group", returned_message_id="media")
    third = journal.append_part(logical, chat_id="group")
    assert [first.part_index, second.part_index, third.part_index] == [0, 1, 2]
    rows = reopen(journal).lookup(logical, chat_id="group")
    assert [r.message_id for r in rows] == ["text", "media", None]
    assert all((r.source_session_key, r.source_message_id) == ("session:group", "inbound-source") for r in rows)


def test_concurrent_append_allocates_unique_parts_across_journal_instances(journal):
    logical = journal.begin_send(chat_id="group")

    def append(index):
        return reopen(journal).append_part(logical, chat_id="group", returned_message_id=f"wire-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(append, range(12)))
    assert sorted(r.part_index for r in receipts) == list(range(12))
    assert {r.message_id for r in receipts} == {f"wire-{i}" for i in range(12)}
    assert len(reopen(journal).lookup(logical, chat_id="group")) == 12
    with pytest.raises(ReceiptScopeError):
        journal.append_part(logical, chat_id="wrong", returned_message_id="x")


@pytest.mark.parametrize("message", [None, "", "  ", 123, True, {"id": "not-a-payload-store"}])
def test_missing_or_malformed_returned_id_is_unknown_not_fabricated(journal, message):
    logical = journal.begin_send(chat_id="group")
    receipt = journal.record_part(logical, chat_id="group", part_index=0, returned_message_id=message)
    assert receipt.message_id is None
    assert receipt.delivery_status == "unknown"


@pytest.mark.parametrize("success,expected", [(False, "error"), (None, "unknown")])
def test_unsuccessful_send_cannot_supply_a_recallable_id(journal, success, expected):
    logical = journal.begin_send(chat_id="group")
    receipt = journal.record_part(logical, chat_id="group", part_index=0,
                                  returned_message_id="looks-real", success=success)
    assert (receipt.message_id, receipt.delivery_status) == (None, expected)


def test_exact_logical_chat_lookup_and_immutable_part(journal):
    logical = known(journal)
    assert journal.record_part(logical, chat_id="group", part_index=0,
                               returned_message_id="wire-id") == journal.lookup(logical, chat_id="group")[0]
    with pytest.raises(ValueError, match="overwrite"):
        journal.record_part(logical, chat_id="group", part_index=0, returned_message_id="other-id")
    for bad_logical, bad_group in [(logical[:6], "group"), (logical, "group-other")]:
        with pytest.raises(ReceiptScopeError):
            journal.lookup(bad_logical, chat_id=bad_group)
        with pytest.raises(ReceiptScopeError):
            journal.record_part(bad_logical, chat_id=bad_group, part_index=1, returned_message_id="x")
    assert journal.lookup(logical, chat_id="group")[0].message_id == "wire-id"


@pytest.mark.parametrize("part", [-1, True, 1.5])
def test_invalid_part_indices_rejected(journal, part):
    logical = journal.begin_send(chat_id="group")
    with pytest.raises(ValueError):
        journal.record_part(logical, chat_id="group", part_index=part)


def test_owner_and_transport_fail_closed(journal, tmp_path):
    logical = known(journal)
    with pytest.raises(ReceiptScopeError):
        reopen(journal, owner="other")
    other_account = reopen(journal, transport_owner="app:sandbox")
    with pytest.raises(ReceiptScopeError):
        other_account.lookup(logical, chat_id="group")
    other_profile = OutgoingReceiptJournal(tmp_path / "profile-b", owner="profile-b", transport_owner="app:prod")
    with pytest.raises(ReceiptScopeError):
        other_profile.lookup(logical, chat_id="group")
    for kwargs in [{"owner": "other", "transport_owner": "app:prod"},
                   {"owner": "profile-a", "transport_owner": "other"}]:
        with pytest.raises(ReceiptScopeError):
            GroupRecallService(journal, delete_transport=AsyncMock(), **kwargs)


def test_copied_database_cannot_change_owning_root(journal, tmp_path):
    known(journal)
    other_root = tmp_path / "copied"
    target = other_root / "cache" / "qqbot" / journal.path.name
    target.parent.mkdir(parents=True)
    shutil.copyfile(journal.path, target)
    with pytest.raises(ReceiptScopeError, match="root mismatch"):
        OutgoingReceiptJournal(other_root, owner=journal.owner, transport_owner=journal.transport_owner)


def test_root_must_be_explicit_absolute(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        OutgoingReceiptJournal("relative", owner="p", transport_owner="app")


@pytest.mark.asyncio
async def test_encoded_opaque_ids_and_acknowledged_empty_http_success(journal):
    group, message = "group /?#汉", " wire /?%#汉 "
    logical = known(journal, group=group, message=message)
    transport = AsyncMock(return_value=RecallHTTPResult(200, acknowledged=True))
    receipt = await service(journal, transport).recall(logical, group_id=group, message_id=message)
    transport.assert_awaited_once_with(
        "DELETE", "/v2/groups/group%20%2F%3F%23%E6%B1%89/messages/%20wire%20%2F%3F%25%23%E6%B1%89%20")
    assert (receipt.recall_status, receipt.http_status, receipt.recall_attempts) == ("api_acknowledged", 200, 1)
    assert reopen(journal).lookup(logical, chat_id=group)[0] == receipt
    # An explicit repeated call reads the acknowledgement without another DELETE.
    assert await service(reopen(journal), transport).recall(logical, group_id=group, message_id=message) == receipt
    assert transport.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected,code", [
    (RecallHTTPResult(204, True), "api_acknowledged", 204),
    (RecallHTTPResult(403, False), "api_error", 403),
    (RecallHTTPResult(404, False), "api_error", 404),
    (RecallHTTPResult(500, None), "api_error", 500),
    (RecallHTTPResult(200, False), "api_error", 200),
    (RecallHTTPResult(200), "transport_unknown", 200),
    (RecallHTTPResult(None), "transport_unknown", None),
    (RecallHTTPResult(True, True), "transport_unknown", None),
    ({}, "transport_unknown", None),
])
async def test_http_failure_ack_and_uncertainty_are_distinct(journal, response, expected, code):
    logical = known(journal)
    receipt = await service(journal, AsyncMock(return_value=response)).recall(
        logical, group_id="group", message_id="wire-id")
    assert (receipt.recall_status, receipt.http_status) == (expected, code)
    assert reopen(journal).lookup(logical, chat_id="group")[0] == receipt


@pytest.mark.asyncio
async def test_timeout_persists_unknown_without_sensitive_exception(journal):
    logical = known(journal)
    transport = AsyncMock(side_effect=TimeoutError("secret-header-and-raw-body"))
    result = await service(journal, transport).recall(logical, group_id="group", message_id="wire-id")
    assert result.recall_status == "transport_unknown"
    restarted = reopen(journal)
    with pytest.raises(ReceiptScopeError, match="explicit retry_unknown"):
        await service(restarted, transport).recall(logical, group_id="group", message_id="wire-id")
    assert transport.await_count == 1
    transport.side_effect = None
    transport.return_value = RecallHTTPResult(200, True)
    retried = await service(restarted, transport).recall(
        logical, group_id="group", message_id="wire-id", retry_unknown=True)
    assert (retried.recall_status, retried.recall_attempts) == ("api_acknowledged", 2)
    with sqlite3.connect(journal.path) as db:
        dump = "\n".join(db.iterdump())
    assert "secret-header-and-raw-body" not in dump


@pytest.mark.asyncio
async def test_cancellation_leaves_durable_unknown_and_blocks_concurrent_recall(journal):
    logical = known(journal)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transport(method, path):
        entered.set()
        await release.wait()
        return RecallHTTPResult(200, True)

    recall = service(journal, transport)
    task = asyncio.create_task(recall.recall(logical, group_id="group", message_id="wire-id"))
    await entered.wait()
    assert reopen(journal).lookup(logical, chat_id="group")[0].recall_status == "transport_unknown"
    with pytest.raises(ReceiptScopeError):
        await recall.recall(logical, group_id="group", message_id="wire-id")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reopen(journal).lookup(logical, chat_id="group")[0].recall_status == "transport_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_group", "unknown_id", "inbound_id", "logical_id", "c2c", "guild", "missing", "duplicate"])
async def test_recall_rejects_unrecognized_or_non_group_receipts_without_io(journal, case):
    chat_type = case if case in {"c2c", "guild"} else "group"
    logical = known(journal, chat_type=chat_type)
    group, message = "group", "wire-id"
    if case == "wrong_group":
        group = "different"
    elif case in {"unknown_id", "inbound_id"}:
        message = case
    elif case == "logical_id":
        message = logical
    elif case == "missing":
        logical = journal.begin_send(chat_id="group")
        journal.record_part(logical, chat_id="group", part_index=0)
    elif case == "duplicate":
        journal.record_part(logical, chat_id="group", part_index=1, returned_message_id=message)
    transport = AsyncMock(return_value=RecallHTTPResult(200, True))
    with pytest.raises(ReceiptScopeError):
        await service(journal, transport).recall(logical, group_id=group, message_id=message)
    transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_send_known_parts_recall_independently_after_restart(journal):
    logical = journal.begin_send(chat_id="group")
    journal.record_part(logical, chat_id="group", part_index=0, returned_message_id="first")
    journal.record_part(logical, chat_id="group", part_index=1)
    journal.record_part(logical, chat_id="group", part_index=2, returned_message_id="third")
    journal.record_part(logical, chat_id="group", part_index=3, success=False)
    restarted = reopen(journal)
    transport = AsyncMock(side_effect=[RecallHTTPResult(403, False), RecallHTTPResult(200, True)])
    recall = service(restarted, transport)
    failed = await recall.recall(logical, group_id="group", message_id="first")
    acknowledged = await recall.recall(logical, group_id="group", message_id="third")
    assert failed.recall_status == "api_error"
    assert acknowledged.recall_status == "api_acknowledged"
    rows = reopen(journal).lookup(logical, chat_id="group")
    assert [(r.delivery_status, r.recall_status) for r in rows] == [
        ("known", "api_error"), ("unknown", "not_requested"),
        ("known", "api_acknowledged"), ("error", "not_requested")]
    transport.side_effect = None
    transport.return_value = RecallHTTPResult(200, True)
    retried = await recall.recall(logical, group_id="group", message_id="first")
    assert (retried.recall_status, retried.recall_attempts) == ("api_acknowledged", 2)


@pytest.mark.asyncio
async def test_old_receipt_has_no_artificial_age_rejection(journal):
    logical = known(journal)
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE sends SET created_at=0 WHERE logical_id=?", (logical,))
        db.execute("UPDATE receipts SET updated_at=0 WHERE logical_id=?", (logical,))
    transport = AsyncMock(return_value=RecallHTTPResult(200, True))
    receipt = await service(reopen(journal), transport).recall(logical, group_id="group", message_id="wire-id")
    assert receipt.recall_status == "api_acknowledged"
    transport.assert_awaited_once()
