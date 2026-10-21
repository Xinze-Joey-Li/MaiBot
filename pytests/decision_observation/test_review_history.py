"""Synthetic historical-send proof, merged paging and identity isolation."""

from typing import Any, Dict
import hashlib
import sqlite3
import sys
import types

import pytest

from test_dashboard import fixture as _fixture

fixture = _fixture


def copy_row(db: sqlite3.Connection, table: str, changes: Dict[str, Any]) -> None:
    db.row_factory = sqlite3.Row
    row = dict(db.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())
    row.update(changes)
    db.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))


def add_history(fixture, number=1, *, seconds=-30, recorded_seconds=None, account="bot", stored=True,
                text="合成历史发言", stored_time="2023-11-15", internal=False):
    boundary = fixture.source["evaluations"][0]["evaluated_at"]
    changes = {"receipt_id": f"old-receipt-{number}", "outbound_id": f"old-internal-{number}",
               "platform_message_id": None if internal else f"old-external-{number}", "account_id": account,
               "observed_at": boundary + seconds,
               "recorded_at": boundary + (seconds + 1 if recorded_seconds is None else recorded_seconds),
               "conclusion": "platform_confirmed", "ambiguous": 0}
    with sqlite3.connect(fixture.path) as db:
        copy_row(db, "delivery_receipts", changes)
    # sqlite3's transaction context commits but does not close the writer. Close
    # it before hashing, so a later GC-driven WAL checkpoint cannot change the
    # physical file during an otherwise read-only projection test.
    db.close()
    if stored:
        with sqlite3.connect(fixture.chat) as db:
            db.execute("INSERT INTO mai_messages VALUES('group-a','qq',?,?,?,'大肥鱼',NULL,?,0,0,NULL)",
                       (changes["outbound_id"] if internal else changes["platform_message_id"], stored_time, account, text))
        db.close()
    return changes


def context(fixture, **kwargs):
    return fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True,
                                  bot_accounts=frozenset({"bot"}), **kwargs)


def history(result):
    return [row for row in result["items"] if row["membership"] == "bot_history_reference"]


def test_predecision_history_is_current_text_with_independent_confirmation_proof(fixture):
    add_history(fixture)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (fixture.path, fixture.chat)}
    result = context(fixture)
    row = history(result)[0]
    assert row["name"] == "大肥鱼" and row["is_bot"] is True
    assert row["text"] == "合成历史发言"
    assert row["time_basis"] == "platform_confirmation"
    assert row["event_timestamp"] < row["confirmation_recorded_at"] < result["evaluated_at"]
    assert result["expected_associated_count"] == 1
    assert result["target_items"][0]["message_event_id"] != row["message_event_id"]
    assert result["total"] == 2
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # Other observation pages retain their existing projection.
    assert fixture.reader.context(fixture.oid, fixture.eid)["total"] == 1


@pytest.mark.parametrize("seconds,recorded", [(1, 2), (0, 0), (-10, 1), (-10, 0), (-601, -600), (-10, -11)])
def test_after_decision_late_ack_window_and_contradictory_times_cannot_admit_history(fixture, seconds, recorded):
    add_history(fixture, seconds=seconds, recorded_seconds=recorded)
    assert history(context(fixture)) == []


def test_main_chat_business_time_alone_and_generation_are_not_send_evidence(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.path) as db:
        db.execute("DELETE FROM delivery_receipts WHERE receipt_id='old-receipt-1'")
    assert history(context(fixture)) == []


@pytest.mark.parametrize("stored,text,stored_time", [(False, "", "2023-11-15"), (True, "", "2023-11-15"),
                                                   (True, "未来正文不得显示", "2099-01-01"),
                                                   (True, "无时间证明的正文", "invalid")])
def test_proven_send_without_reliable_body_never_invents_text(fixture, stored, text, stored_time):
    add_history(fixture, stored=stored, text=text, stored_time=stored_time)
    item = history(context(fixture))[0]
    assert item["is_bot"] is True
    assert item["text"] == ""
    assert item["status"] == ("available" if stored and not text else "body_unavailable")


def test_internal_id_without_external_id_is_supported_only_with_receipt(fixture):
    add_history(fixture, internal=True)
    assert history(context(fixture))[0]["text"] == "合成历史发言"


def test_multiple_bot_accounts_and_human_named_like_bot_are_not_confused(fixture):
    add_history(fixture, 1, account="bot")
    add_history(fixture, 2, account="second-bot")
    with sqlite3.connect(fixture.chat) as db:
        db.execute("UPDATE mai_messages SET user_nickname='大肥鱼' WHERE user_id='synthetic-user'")
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True,
                                    bot_accounts=frozenset({"bot", "second-bot"}))
    assert len(history(result)) == 2
    assert result["target_items"][0]["is_bot"] is False
    assert result["target_items"][0]["name"].startswith("群友 ")
    assert all(item["name"] == "大肥鱼" for item in history(result))


@pytest.mark.parametrize("accounts", [frozenset(), None])
def test_unconfirmed_bot_identity_omits_history_and_keeps_anonymous_senders(fixture, accounts):
    add_history(fixture)
    result = fixture.reader.context(fixture.oid, fixture.eid, annotation_view=True, bot_accounts=accounts)
    assert history(result) == []
    assert result["bot_history_status"] in {"identity_missing", "identity_unavailable"}
    assert "bot" not in result["target_items"][0]["name"]


def test_observation_duplicate_is_not_added_twice(fixture):
    data = add_history(fixture)
    boundary = fixture.source["evaluations"][0]["evaluated_at"]
    with sqlite3.connect(fixture.path) as db:
        copy_row(db, "message_events", {"message_event_id": "observed-bot", "platform_message_id": data["platform_message_id"],
                                        "sender_id": "bot", "event_timestamp": boundary - 30, "received_timestamp": boundary - 29})
    result = context(fixture)
    assert result["total"] == 2
    assert history(result) == []
    assert [row for row in result["items"] if row["is_bot"]][0]["name"] == "大肥鱼"


def test_duplicate_receipts_merge_by_stored_message_identity(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.path) as db:
        copy_row(db, "delivery_receipts", {"receipt_id": "duplicate-receipt"})
        # Ensure the duplicate describes the historical message, not the fixture's later send.
        db.execute("UPDATE delivery_receipts SET outbound_id='old-internal-1',platform_message_id='old-external-1',"
                   "observed_at=?,recorded_at=? WHERE receipt_id='duplicate-receipt'",
                   (fixture.source["evaluations"][0]["evaluated_at"] - 25, fixture.source["evaluations"][0]["evaluated_at"] - 24))
    assert len(history(context(fixture))) == 1


def test_different_evaluation_boundaries_do_not_reveal_later_send(fixture):
    add_history(fixture, seconds=10)
    assert history(context(fixture)) == []
    with sqlite3.connect(fixture.path) as db:
        copy_row(db, "evaluations", {"evaluation_id": "later-evaluation", "evaluation_sequence": 2,
                                      "evaluated_at": fixture.source["evaluations"][0]["evaluated_at"] + 30})
    result = fixture.reader.context(fixture.oid, "later-evaluation", annotation_view=True, bot_accounts=frozenset({"bot"}))
    assert any(row["receipt_id"] == "old-receipt-1" for row in history(result))
    assert all(row["event_timestamp"] < result["evaluated_at"] for row in history(result))


def test_merged_paging_is_sorted_unique_and_keeps_offpage_target_preview(fixture):
    boundary = fixture.source["evaluations"][0]["evaluated_at"]
    for n in range(1, 8):
        add_history(fixture, n, seconds=-100 + n * 10)
    with sqlite3.connect(fixture.path) as db:
        for n in range(6):
            copy_row(db, "message_events", {"message_event_id": f"reference-{n}", "platform_message_id": f"reference-{n}",
                                            "event_timestamp": boundary - 95 + n * 10, "received_timestamp": boundary - 95 + n * 10})
    results = [context(fixture, page=page, page_size=3) for page in range(1, 6)]
    combined = [row for result in results for row in result["items"]]
    assert all(result["total"] == 14 for result in results)
    assert len(combined) == len({row["message_event_id"] for row in combined}) == 14
    times = [row.get("received_at") or row["event_timestamp"] for row in combined]
    assert times == sorted(times)
    assert results[-1]["target_items"] == results[0]["target_items"]


def test_unauthorized_session_never_reads_history(fixture):
    add_history(fixture)
    fixture.reader.authorized = frozenset()
    assert context(fixture) is None


def test_conflicting_sender_or_receipt_omits_unreliable_record(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.chat) as db:
        db.execute("UPDATE mai_messages SET user_id='not-the-bot' WHERE message_id='old-external-1'")
    assert history(context(fixture)) == []


def test_identity_service_failure_is_an_explicit_safe_capability_loss(monkeypatch):
    from src.webui.routers.decision_annotations import review_bot_accounts

    def fail(platform):
        raise RuntimeError("synthetic failure, no identity disclosure")

    monkeypatch.setitem(sys.modules, "src.services.bot_account_service", types.SimpleNamespace(get_bot_accounts=fail))
    assert review_bot_accounts() is None


def test_duplicate_chat_rows_cannot_choose_a_body(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.chat) as db:
        db.execute("INSERT INTO mai_messages SELECT * FROM mai_messages WHERE message_id='old-external-1'")
    item = history(context(fixture))[0]
    assert item["status"] == "body_unavailable" and item["text"] == ""


def test_conflicting_confirmation_or_unknown_account_is_omitted(fixture):
    add_history(fixture)
    add_history(fixture, 2, account="unconfirmed-bot")
    with sqlite3.connect(fixture.path) as db:
        copy_row(db, "delivery_receipts", {"receipt_id": "conflict", "account_id": "bot",
                                           "outbound_id": "old-internal-1", "conclusion": "definitive_failure",
                                           "recorded_at": fixture.source["evaluations"][0]["evaluated_at"] - 20})
    assert history(context(fixture)) == []


def test_idless_confirmation_and_later_external_mapping_do_not_duplicate_a_send(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM delivery_receipts WHERE receipt_id='old-receipt-1'").fetchone())
        row.update(receipt_id="idless-receipt", platform_message_id=None,
                   observed_at=row["observed_at"] - 2, recorded_at=row["recorded_at"] - 2)
        db.execute(f"INSERT INTO delivery_receipts ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    assert len(history(context(fixture))) == 1


def test_history_read_cap_is_explicit_and_does_not_return_partial_pages(fixture):
    add_history(fixture)
    with sqlite3.connect(fixture.path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute("SELECT * FROM delivery_receipts WHERE receipt_id='old-receipt-1'").fetchone())
        for n in range(512):
            row["receipt_id"] = f"many-{n}"
            db.execute(f"INSERT INTO delivery_receipts ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    result = context(fixture)
    assert result["bot_history_status"] == "read_limit"
    assert history(result) == []
    assert result["total"] == 1
