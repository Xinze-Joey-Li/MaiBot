"""Synthetic SQLite/UI query tests. No production bot, model or QQ requests."""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sqlite3

import pytest

from src.decision_observation.dashboard import DashboardReader, read_database
from src.decision_observation.models import SCHEMA_VERSION
from src.decision_observation.store import ObservationStore


@pytest.fixture
def fixture(tmp_path, request):
    filename = getattr(request, "param", "decision-observation-phase2b3b-complete.json")
    source = json.loads((Path(__file__).resolve().parents[2] / "docs" / filename).read_text(encoding="utf8"))
    path = tmp_path / "observation.db"
    store = ObservationStore(path)
    connection = store._connect()  # Fixture creation only; Dashboard never uses this method.
    connection.execute("PRAGMA foreign_keys=OFF")
    aliases = {
        "messages": "message_events",
        "transfers": "opportunity_transfers",
        "claim_checks": "priority_claim_checks",
    }
    for key in (
        "messages",
        "opportunities",
        "evaluations",
        "outcomes",
        "execution_events",
        "transfers",
        "claim_checks",
        "reply_events",
        "delivery_receipts",
        "delivery_links",
    ):
        table = aliases.get(key, key)
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        for item in source.get(key, []):
            if table == "opportunities":
                for mid in item["message_event_ids"]:
                    connection.execute(
                        "INSERT INTO opportunity_messages VALUES(?,?,?)", (item["opportunity_id"], mid, "pending")
                    )
            values = [json.dumps(item[c]) if isinstance(item.get(c), (dict, list)) else item.get(c) for c in columns]
            connection.execute(f"INSERT INTO {table} VALUES(" + ",".join("?" for _ in columns) + ")", values)
    connection.commit()
    connection.close()
    chat = tmp_path / "chat.db"
    with sqlite3.connect(chat) as db:
        db.executescript(
            "CREATE TABLE chat_sessions(session_id TEXT,platform TEXT,group_id TEXT,group_name TEXT); CREATE TABLE mai_messages(session_id TEXT,platform TEXT,message_id TEXT,timestamp TEXT,user_id TEXT,user_nickname TEXT,user_cardname TEXT,processed_plain_text TEXT,is_at INTEGER,is_mentioned INTEGER,reply_to TEXT);"
        )
        db.execute("INSERT INTO chat_sessions VALUES('group-a','qq','synthetic','模拟群聊')")
        db.execute(
            "INSERT INTO mai_messages VALUES('group-a','qq','m1','2023-11-15','synthetic-user','匿名测试成员',NULL,'模拟消息',1,0,NULL)"
        )
    reader = DashboardReader(
        path, chat, tmp_path / "replay.db", frozenset(o["session_id"] for o in source["opportunities"])
    )
    yield SimpleNamespace(
        reader=reader,
        path=path,
        chat=chat,
        source=source,
        oid=source["opportunities"][0]["opportunity_id"],
        eid=source["evaluations"][0]["evaluation_id"] if source["evaluations"] else "no-evaluation",
        start=source["opportunities"][0]["created_at"] - 10,
        end=source["opportunities"][0]["created_at"] + 10,
    )


def listing(fixture, **kwargs):
    return fixture.reader.listing(start=fixture.start, end=fixture.end, **kwargs)


def test_no_database_is_normal_and_never_created(tmp_path):
    reader = DashboardReader(tmp_path / "absent.db", tmp_path / "chat.db", tmp_path / "replay.db", frozenset())
    assert reader.health()["database_exists"] is False
    assert reader.listing(start=1, end=2)["total"] == 0
    assert reader.detail("x") is None
    assert reader.context("x", "y") is None
    assert list(tmp_path.iterdir()) == []


def test_read_only_hash_schema_and_no_production_calls(fixture, monkeypatch):
    monkeypatch.setattr(ObservationStore, "_connect", lambda *args: pytest.fail("dashboard tried writer connection"))
    before = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
    assert fixture.reader.health()["schema_version"] == SCHEMA_VERSION
    assert listing(fixture)["total"] == 1
    assert fixture.reader.detail(fixture.oid)
    assert fixture.reader.context(fixture.oid, fixture.eid)
    assert hashlib.sha256(fixture.path.read_bytes()).hexdigest() == before
    with read_database(fixture.path) as connection, pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM opportunities")


def test_counts_multi_segment_and_receipts(fixture):
    result = listing(fixture)
    assert result["summary"]["generation_calls"] == 1
    assert result["summary"]["driver_attempts"] == 3
    assert result["summary"]["confirmed_attempts"] == 3
    assert result["summary"]["platform_unique_messages"] == 3
    assert result["summary"]["platform_confirmed_opportunities"] == 1
    assert result["items"][0]["session_name"] == "模拟群聊"
    detail = fixture.reader.detail(fixture.oid, page_size=2)
    assert len(detail["groups"]["reply_events"]["items"]) == 2
    assert detail["groups"]["reply_events"]["total"] == 22
    assert len(detail["groups"]["delivery_receipts"]["items"][0]["links"]) == 1
    assert detail["replay"][0]["status"] == "replay_unavailable"
    # replies.receipt uses the explicit acknowledged delivery key when QQ message ID is absent.
    # This is a counting identity, never a fabricated external platform_message_id.
    with sqlite3.connect(fixture.path) as db:
        db.execute("UPDATE delivery_receipts SET platform_message_id=NULL,acknowledged=1,delivery_id=receipt_id")
    assert listing(fixture)["summary"]["platform_unique_messages"] == 3


def test_many_to_many_counts_and_stable_paging(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute(
            "INSERT INTO opportunities SELECT 'second',session_id,created_at,run_id,policy_version,source_type,finalized_at,status,account_id FROM opportunities LIMIT 1"
        )
        db.execute(
            "INSERT INTO reply_events SELECT event_id||'copy',occurrence_id,parent_event_id,'second',evaluation_id,execution_id,flow_id,occurred_at,stage,status,reason,logical_turn_id,cycle_id,tool_call_id,reply_id,generation_group_id,request_id,outbound_id,attempt_id,detail FROM reply_events"
        )
        db.execute("INSERT INTO delivery_links SELECT receipt_id,parent_event_id||'copy',linked_at FROM delivery_links")
    result = listing(fixture, page_size=1)
    assert result["total"] == 2
    assert result["summary"]["driver_attempts"] == 3
    assert result["summary"]["confirmed_attempts"] == 3
    assert result["summary"]["platform_unique_messages"] == 3
    assert listing(fixture, page=2, page_size=1)["items"][0]["opportunity_id"] != result["items"][0]["opportunity_id"]


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_historical_schemas_without_migration(fixture, version):
    with sqlite3.connect(fixture.path) as db:
        for table, introduced in (
            ("annotation_revisions", 6),
            ("annotation_targets", 6),
            ("execution_events", 2),
            ("opportunity_transfers", 3),
            ("priority_claim_checks", 3),
            ("reply_events", 4),
            ("delivery_receipts", 5),
            ("delivery_links", 5),
            ("delivery_scopes", 5),
        ):
            if introduced > version:
                db.execute(f"DROP TABLE {table}")
        db.execute(f"PRAGMA user_version={version}")
    assert fixture.reader.health()["schema_version"] == version
    assert listing(fixture)["summary"]["platform_unique_messages"] is None
    assert fixture.reader.detail(fixture.oid)
    with sqlite3.connect(fixture.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version


def test_authorization_and_foreign_evaluation(fixture):
    with pytest.raises(PermissionError):
        listing(fixture, session_id="group-b")
    denied = DashboardReader(fixture.path, fixture.chat, fixture.path, frozenset({"group-b"}))
    assert denied.detail(fixture.oid) is None
    assert denied.context(fixture.oid, fixture.eid) is None
    assert denied.listing(start=fixture.start, end=fixture.end)["total"] == 0
    assert fixture.reader.context(fixture.oid, "foreign-evaluation") is None


def test_filter_and_bounds(fixture):
    assert listing(fixture, gate="reject")["total"] == 0
    assert listing(fixture, confirmation="platform_confirmed")["total"] == 1
    assert listing(fixture, source_type="priority_at")["total"] == 0
    for args in ({"start": 0, "end": 32 * 86400}, {"start": 2, "end": 1}, {"start": 0, "end": 1, "page_size": 101}):
        with pytest.raises(ValueError):
            fixture.reader.listing(**args)


def test_sent_without_confirmation_and_unlinked_positive_remain_unknown(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("UPDATE delivery_receipts SET conclusion='unknown',platform_message_id=NULL")
    result = listing(fixture)
    assert result["summary"]["confirmed_attempts"] == 0
    assert result["summary"]["unknown_attempts"] == 3
    assert result["summary"]["platform_unique_messages"] == 0
    with sqlite3.connect(fixture.path) as db:
        db.execute("UPDATE delivery_receipts SET conclusion='platform_confirmed'")
        db.execute("DELETE FROM delivery_links")
    assert listing(fixture)["summary"]["unobserved_attempts"] == 3
    assert listing(fixture)["summary"]["confirmed_attempts"] == 0
    diagnostics = fixture.reader.detail(fixture.oid)["groups"]["session_unlinked_receipts"]
    assert diagnostics["total"] == 3
    assert all(
        item["status"] == "unknown" and item["associated_with_this_opportunity"] is False
        for item in diagnostics["items"]
    )


def test_snapshot_membership_and_no_future_reference(fixture):
    with sqlite3.connect(fixture.path) as db:
        cutoff = db.execute("SELECT evaluated_at FROM evaluations").fetchone()[0]
        for mid, received in (("before", cutoff - 1), ("late", cutoff + 1), ("unknown-arrival", None)):
            db.execute(
                "INSERT INTO message_events SELECT ?,session_id,?,sender_id,?, ?,run_id,source_type,schema_version FROM message_events LIMIT 1",
                (mid, mid, cutoff - 2, received),
            )
    with sqlite3.connect(fixture.chat) as db:
        for mid in ("before", "late", "unknown-arrival"):
            db.execute(
                "INSERT INTO mai_messages SELECT session_id,platform,?,timestamp,user_id,user_nickname,user_cardname,?,is_at,is_mentioned,reply_to FROM mai_messages LIMIT 1",
                (mid, mid),
            )
        db.execute(
            "INSERT INTO mai_messages SELECT 'group-b',platform,'m1',timestamp,user_id,user_nickname,user_cardname,'secret other group',is_at,is_mentioned,reply_to FROM mai_messages LIMIT 1"
        )
    result = fixture.reader.context(fixture.oid, fixture.eid)
    assert result["total"] == 2
    assert {m["membership"] for m in result["items"]} == {"reference_only", "evaluation_member"}
    assert "late" not in {m.get("text") for m in result["items"]}
    assert "unknown-arrival" not in {m.get("text") for m in result["items"]}
    assert "secret other group" not in json.dumps(result)


def test_missing_chat_and_duplicate_identity(fixture):
    with sqlite3.connect(fixture.chat) as db:
        db.execute("INSERT INTO mai_messages SELECT * FROM mai_messages")
    result = fixture.reader.context(fixture.oid, fixture.eid)
    assert result["items"][0]["status"] == "missing_or_ambiguous"
    fixture.reader.chat_path = fixture.chat.with_name("missing-chat.db")
    assert fixture.reader.context(fixture.oid, fixture.eid)["context_status"] == "chat_database_unavailable"


def test_authenticated_api_readonly_and_error_isolation(fixture):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.webui.dependencies import require_auth
    from src.webui.routers.decision_observation import reader, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[reader] = lambda: fixture.reader
    client = TestClient(app)
    url = "/api/webui/decision-observation"
    assert client.get(url + "/opportunities").status_code == 401
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    assert client.get(url + f"/opportunities/{fixture.oid}").status_code == 200
    assert client.get(url + f"/opportunities/{fixture.oid}/context?evaluation_id=foreign").status_code == 404
    assert client.post(url + "/opportunities").status_code == 405
    assert client.get(url + "/opportunities?session_id=group-b").status_code == 403
    assert client.get(url + "/opportunities?start=0&end=999999999").status_code == 422
    fixture.path.write_bytes(b"broken sqlite")
    response = client.get(url + "/opportunities")
    assert response.status_code == 503 and str(fixture.path) not in response.text


@pytest.mark.parametrize(
    "fixture",
    [
        "decision-observation-phase2b1-example.json",
        "decision-observation-phase2b2-emoji-example.json",
        "decision-observation-phase2b2-text-example.json",
        "decision-observation-phase2b2-fallback-example.json",
        "decision-observation-phase2b2-redelivery-example.json",
        "decision-observation-phase2b3a-complete-example.json",
        "decision-observation-phase2b3a-redelivery-example.json",
        "decision-observation-phase2b3b-ambiguous.json",
        "decision-observation-phase2b3b-late.json",
    ],
    indirect=True,
)
def test_previous_phase_real_mock_records_are_queryable_without_reclassification(fixture):
    # These files were generated by the real mocked execution chain in previous phases.
    start = min(o["created_at"] for o in fixture.source["opportunities"]) - 1
    end = max(o["created_at"] for o in fixture.source["opportunities"]) + 1
    result = fixture.reader.listing(start=start, end=end)
    assert result["total"] == len(fixture.source["opportunities"])
    assert result["summary"]["generation_calls"] == len(
        {
            r["occurrence_id"]
            for r in fixture.source.get("reply_events", [])
            if r["stage"] == "generation" and r["status"] == "generation_started"
        }
    )
    for item in result["items"]:
        detail = fixture.reader.detail(item["opportunity_id"])
        assert detail["opportunity"]["opportunity_id"] == item["opportunity_id"]
        if item["source_type"] == "priority_at":
            assert item["planner"] == "not_applicable"
    if any(d.get("ambiguous") for d in fixture.source.get("delivery_receipts", [])):
        assert result["summary"]["confirmed_attempts"] == 0
    if (
        fixture.source.get("delivery_receipts")
        and len(fixture.source["delivery_receipts"]) == 2
        and len(fixture.source["delivery_links"]) == 2
    ):
        assert result["summary"]["confirmed_attempts"] == 1


def test_empty_existing_database(fixture):
    with sqlite3.connect(fixture.path) as db:
        for name in (
            "opportunities",
            "message_events",
            "evaluations",
            "execution_events",
            "reply_events",
            "delivery_receipts",
            "delivery_links",
            "opportunity_messages",
        ):
            db.execute(f"DELETE FROM {name}")
    assert listing(fixture)["total"] == 0
    assert listing(fixture)["summary"]["generation_calls"] == 0


def test_rejected_or_wait_never_creates_generation(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("DELETE FROM reply_events")
        db.execute("DELETE FROM execution_events")
        db.execute("UPDATE evaluations SET gate_result='reject'")
    result = listing(fixture)
    assert result["items"][0]["planner"] == "not_observed"
    assert result["summary"]["generation_calls"] == 0
    assert result["summary"]["driver_attempts"] == 0


def test_replay_expiration_and_group_membership(fixture):
    with sqlite3.connect(fixture.reader.replay_path) as db:
        db.execute("CREATE TABLE turns(logical_turn_id TEXT,session_id TEXT)")
        db.execute("INSERT INTO turns VALUES('turn:group-a','group-b')")
    assert fixture.reader.detail(fixture.oid)["replay"][0]["status"] == "expired_or_missing"
    with sqlite3.connect(fixture.reader.replay_path) as db:
        db.execute("INSERT INTO turns VALUES('turn:group-a','group-a')")
    assert fixture.reader.detail(fixture.oid)["replay"][0]["status"] == "available"


def test_opportunity_association_is_never_a_fake_evaluation_snapshot(fixture):
    result = fixture.reader.context(fixture.oid, "")
    assert result["association_only"] is True and result["membership_available"] is False
    assert result["items"][0]["membership"] == "opportunity_associated"
    detail = fixture.reader.detail(fixture.oid)
    assert detail["groups"]["attempt_states"]["total"] == 3
    assert {a["status"] for a in detail["groups"]["attempt_states"]["items"]} == {"platform_confirmed"}


def test_direct_emoji_is_only_not_applicable_with_explicit_fact(fixture):
    with sqlite3.connect(fixture.path) as db:
        db.execute("DELETE FROM reply_events WHERE stage='generation'")
        db.execute("UPDATE reply_events SET stage='direct_emoji',status='not_applicable' WHERE stage='reply_identity'")
    result = listing(fixture)
    assert result["items"][0]["generation"] == "not_applicable"
    assert result["summary"]["generation_calls"] == 0
