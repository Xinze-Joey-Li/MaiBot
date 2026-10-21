"""Real isolated SQLite, commit barriers, Mock auth; no QQ or model calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4
import asyncio
import json
import sqlite3
import time

import pytest

from src.decision_observation import annotation_storage
from src.decision_observation.annotation_models import ACTOR_ID, AnnotationCommand, AnnotationError
from src.decision_observation.annotations import AnnotationService
from src.decision_observation.dashboard import read_database
from src.decision_observation.models import Policy, SCHEMA_VERSION
from src.decision_observation.store import ObservationStore
from test_dashboard import fixture as _fixture

fixture = _fixture


@pytest.fixture
def env(fixture):
    # The read-only Dashboard fixture intentionally omits scopes; add coherent
    # synthetic scope parents here so schema migration FK checks cover a valid v5 graph.
    with sqlite3.connect(fixture.path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO delivery_scopes SELECT DISTINCT d.scope_id,d.session_id,o.run_id,o.policy_version,o.created_at,1 FROM delivery_receipts d JOIN opportunities o ON o.session_id=d.session_id"
        )
    store = ObservationStore(fixture.path)
    store.configure(Policy(False, frozenset({"group-a"})))
    service = AnnotationService(store, fixture.chat)
    command = AnnotationCommand(
        fixture.oid, "evaluation", fixture.eid, "should_reply", None, "identity_context_available", 0, uuid4().hex
    )
    yield SimpleNamespace(data=fixture, store=store, service=service, command=command)
    assert store.close()


def save(env, **changes):
    return env.service.save(replace(env.command, **changes))


def facts(path):
    with read_database(path) as connection:
        return {
            name: [tuple(r) for r in connection.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in [
                "opportunities",
                "evaluations",
                "message_events",
                "outcomes",
                "execution_events",
                "reply_events",
                "delivery_receipts",
                "delivery_links",
            ]
        }


@pytest.mark.parametrize("label", ["should_reply", "should_not_reply", "uncertain"])
def test_first_annotation_labels_and_facts_are_independent(env, label):
    before = facts(env.store.path)
    result = save(env, label=label)
    annotation = result["annotation"]
    assert result["persisted"] and result["current_revision"] == 1
    assert annotation["label"] == label and annotation["actor_id"] == ACTOR_ID
    assert annotation["reason"] is None and annotation["schema_version"] == 6
    assert annotation["context_quality"] == "identity_context_available"
    assert not annotation["evidence"]["full_planner_prompt_recovered"]
    assert before == facts(env.store.path)
    assert env.store.submitted == 0 and env.store._worker is None
    current = env.service.current(env.data.oid, "evaluation", env.data.eid)
    assert current["annotation_state"] == ("uncertain" if label == "uncertain" else "annotated")


def test_evaluations_independent_and_opportunity_requires_real_missing_eval(env):
    with sqlite3.connect(env.store.path) as connection:
        connection.execute(
            "INSERT INTO evaluations SELECT 'eval-two',opportunity_id,2,evaluated_at+1,feature_version,feature_snapshot,effective_threshold,calculated_probability,'reject','second',weights_hash,evidence FROM evaluations"
        )
    a = save(env)
    b = save(env, evaluation_id="eval-two", label="should_not_reply", idempotency_key=uuid4().hex)
    assert a["annotation"]["annotation_id"] != b["annotation"]["annotation_id"]
    assert b["annotation"]["evidence"]["target"]["evaluation_sequence"] == 2
    with pytest.raises(AnnotationError, match="evaluation_target_required"):
        save(env, target_type="opportunity", evaluation_id=None)
    with sqlite3.connect(env.store.path) as connection:
        connection.execute(
            "INSERT INTO opportunities SELECT 'priority',session_id,created_at,run_id,policy_version,'priority_at',finalized_at,status,account_id FROM opportunities LIMIT 1"
        )
        connection.execute(
            "INSERT INTO opportunity_messages SELECT 'priority',message_event_id,role FROM opportunity_messages LIMIT 1"
        )
    result = save(
        env, opportunity_id="priority", target_type="opportunity", evaluation_id=None, idempotency_key=uuid4().hex
    )
    assert result["annotation"]["evaluation_id"] is None
    assert result["annotation"]["evidence"]["target"]["source_type"] == "priority_at"
    assert result["annotation"]["context_quality"] == "partial_context"


def test_unannotated_uncertain_and_list_filters(env):
    assert env.service.current(env.data.oid, "evaluation", env.data.eid)["annotation_state"] == "unannotated"
    stamp = env.data.source["evaluations"][0]["evaluated_at"]
    query = dict(start=stamp - 1, end=stamp + 1)
    assert env.service.targets(**query, annotation_state="unannotated")["total"] == 1
    save(env, label="uncertain")
    assert env.service.targets(**query, annotation_state="unannotated")["total"] == 0
    assert env.service.targets(**query, annotation_state="uncertain")["items"][0]["current_revision"] == 1
    assert env.service.targets(**query, annotation_state="annotated")["total"] == 0


def test_revision_history_idempotency_and_conflicts(env):
    a = save(env, reason="private: '); DROP TABLE opportunities;--")
    duplicate = save(env, reason="private: '); DROP TABLE opportunities;--")
    assert duplicate["idempotent_replay"] and duplicate["annotation"] == a["annotation"]
    b = save(env, label="uncertain", reason="", expected_revision=1, idempotency_key=uuid4().hex)
    assert b["current_revision"] == 2
    replayed = save(env, reason="private: '); DROP TABLE opportunities;--")
    assert replayed["annotation"]["revision"] == 1 and replayed["current_revision"] == 2
    with pytest.raises(AnnotationError, match="idempotency_conflict"):
        save(env, reason="different")
    with pytest.raises(AnnotationError, match="revision_conflict") as error:
        save(env, idempotency_key=uuid4().hex)
    assert error.value.current_revision == 2
    history = env.service.history(env.data.oid, "evaluation", env.data.eid, page_size=1)
    assert history["total"] == 2 and history["items"][0]["revision"] == 2
    assert env.service.history(env.data.oid, "evaluation", env.data.eid, 2, 1)["items"][0]["reason"].startswith(
        "private:"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE annotation_revisions SET label='uncertain'",
        "DELETE FROM annotation_revisions",
        "UPDATE annotation_targets SET target_type='opportunity'",
        "DELETE FROM annotation_targets",
    ],
)
def test_history_cannot_be_rewritten_or_deleted(env, sql):
    save(env)
    with sqlite3.connect(env.store.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(sql)
    assert env.service.history(env.data.oid, "evaluation", env.data.eid)["total"] == 1


def test_context_quality_cannot_be_upgraded_by_client(env):
    with sqlite3.connect(env.data.chat) as connection:
        connection.execute("DELETE FROM mai_messages")
    result = save(env)
    assert result["annotation"]["declared_context_quality"] == "identity_context_available"
    assert result["annotation"]["context_quality"] == "critical_context_missing"
    assert result["annotation"]["evidence"]["training_review_required"]
    assert "模拟消息" not in json.dumps(result, ensure_ascii=False)
    explicit = save(
        env,
        label="uncertain",
        expected_revision=1,
        idempotency_key=uuid4().hex,
        context_quality="critical_context_missing",
    )
    assert explicit["annotation"]["label"] == "uncertain"


def test_deleted_parent_cascades_private_history_and_old_requests_cannot_recreate(env):
    save(env, reason="private reason")
    env.store.delete_session("group-a")
    with read_database(env.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM annotation_targets").fetchone()[0] == 0
    with pytest.raises(AnnotationError):
        save(env)
    with pytest.raises(AnnotationError):
        env.service.history(env.data.oid, "evaluation", env.data.eid)


@pytest.mark.parametrize("operation", ["revoke", "delete", "revoke_regrant"])
def test_late_write_during_control_invalidation_rolls_back(env, monkeypatch, operation):
    entered, release = Event(), Event()
    original = env.service._revision
    calls = 0

    def barrier(connection, aid, revision=None):
        nonlocal calls
        result = original(connection, aid, revision)
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(env.service, "_revision", barrier)
    with ThreadPoolExecutor(max_workers=3) as pool:
        future = pool.submit(save, env)
        assert entered.wait(3)
        control = (
            pool.submit(env.store.delete_session, "group-a")
            if operation == "delete"
            else pool.submit(env.store.configure, Policy(False, frozenset()))
        )
        deadline = time.monotonic() + 2
        while env.store.annotation_generation("group-a") is not None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert env.store.annotation_generation("group-a") is None
        release.set()
        with pytest.raises((AnnotationError, PermissionError)):
            future.result(3)
        control.result(3)
        if operation == "revoke_regrant":
            env.store.configure(Policy(False, frozenset({"group-a"})))
    with read_database(env.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 0


def test_concurrent_writes_have_one_winner_and_duplicate_has_one_revision(env):
    def attempt(key):
        try:
            return env.service.save(replace(env.command, idempotency_key=key))
        except AnnotationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [uuid4().hex, uuid4().hex]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "annotation_revision_conflict" in results
    second = replace(env.command, expected_revision=1, idempotency_key=uuid4().hex)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: env.service.save(second), range(2)))
    assert {r["annotation"]["revision"] for r in results} == {2}
    assert sum(r["idempotent_replay"] for r in results) == 1


def test_locked_database_and_insert_failure_never_claim_success(env, monkeypatch):
    connection = sqlite3.connect(env.store.path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            save(env)
    finally:
        connection.rollback()
        connection.close()
    assert env.service.history(env.data.oid, "evaluation", env.data.eid)["total"] == 0
    with sqlite3.connect(env.store.path) as connection:
        connection.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON annotation_revisions BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        save(env)
    with read_database(env.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM annotation_targets").fetchone()[0] == 0


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_historical_schema_transactional_upgrade_preserves_all_rows(env, version):
    with sqlite3.connect(env.store.path) as connection:
        for name, level in [
            ("annotation_revisions", 6),
            ("annotation_targets", 6),
            ("delivery_links", 5),
            ("delivery_receipts", 5),
            ("delivery_scopes", 5),
            ("reply_events", 4),
            ("priority_claim_checks", 3),
            ("opportunity_transfers", 3),
            ("execution_events", 2),
        ]:
            if level > version:
                connection.execute("DROP TABLE " + name)
        if version < 3:
            connection.execute("ALTER TABLE opportunities DROP COLUMN account_id")
        connection.execute("PRAGMA user_version=" + str(version))
        before = {
            name: [tuple(r) for r in connection.execute("SELECT * FROM " + name)]
            for name in ["opportunities", "evaluations", "message_events", "outcomes"]
        }
    assert env.service.current(env.data.oid, "evaluation", env.data.eid)["annotation"] is None
    with read_database(env.store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
    save(env)
    with read_database(env.store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        for name, rows in before.items():
            restored = [tuple(r)[: len(rows[0])] for r in connection.execute("SELECT * FROM " + name)] if rows else []
            assert restored == rows
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_failure_rolls_back_schema_six(env, monkeypatch):
    with sqlite3.connect(env.store.path) as connection:
        connection.execute("DROP TABLE annotation_revisions")
        connection.execute("DROP TABLE annotation_targets")
        connection.execute("PRAGMA user_version=5")
    monkeypatch.setattr(
        annotation_storage,
        "MIGRATION",
        "BEGIN; CREATE TABLE should_rollback(x); SELECT invalid_column; PRAGMA user_version=6; COMMIT;",
    )
    with pytest.raises(sqlite3.Error):
        save(env)
    with read_database(env.store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None


def test_no_database_no_creation_and_revoked_read(env, tmp_path):
    store = ObservationStore(tmp_path / "no-parent" / "absent.db")
    store.configure(Policy(False, frozenset({"group-a"})))
    service = AnnotationService(store, tmp_path / "chat.db")
    with pytest.raises(AnnotationError):
        service.save(env.command)
    with pytest.raises(AnnotationError):
        service.current(env.data.oid, "evaluation", env.data.eid)
    assert service.targets(start=0, end=1)["total"] == 0
    assert not store.path.parent.exists()
    save(env)
    env.store.configure(Policy(False, frozenset()))
    with pytest.raises(AnnotationError):
        env.service.current(env.data.oid, "evaluation", env.data.eid)


@pytest.fixture
def client(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.webui.dependencies import require_auth
    from src.webui.routers.decision_annotations import annotation_service, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[annotation_service] = lambda: env.service
    with TestClient(app) as client:
        yield client, app, require_auth


def request_body(env):
    return {k: v for k, v in env.command.__dict__.items() if k != "opportunity_id"}


def test_api_auth_actor_validation_history_and_storage_errors(env, client):
    client, app, auth = client
    url = "/api/webui/decision-observation/annotations/" + env.data.oid
    body = request_body(env)
    assert client.put(url, json=body).status_code == 401
    app.dependency_overrides[auth] = lambda: "private-shared-auth-token"
    assert client.put(url, json={**body, "actor_id": "fake user"}).status_code == 422
    for invalid in [{"label": "bad"}, {"reason": "x" * 2001}, {"expected_revision": True}, {"evaluation_id": "wrong"}]:
        assert client.put(url, json={**body, **invalid}).status_code in (422, 404)
    response = client.put(url, json=body)
    assert response.status_code == 201 and response.json()["persisted"]
    assert response.json()["annotation"]["actor_id"] == ACTOR_ID
    assert "private-shared-auth-token" not in response.text
    assert client.put(url, json=body).status_code == 200
    assert client.put(url, json={**body, "idempotency_key": uuid4().hex}).status_code == 409
    assert (
        client.get(url + "/history", params={"target_type": "evaluation", "evaluation_id": env.data.eid}).json()[
            "total"
        ]
        == 1
    )
    connection = sqlite3.connect(env.store.path)
    connection.execute("BEGIN IMMEDIATE")
    try:
        assert client.put(url, json={**body, "expected_revision": 1, "idempotency_key": uuid4().hex}).status_code == 503
    finally:
        connection.rollback()
        connection.close()
    env.store.configure(Policy(False, frozenset()))
    assert client.put(url, json=body).status_code == 404
    assert client.get(url, params={"target_type": "evaluation", "evaluation_id": env.data.eid}).status_code == 404


@pytest.mark.asyncio
async def test_transaction_delay_does_not_block_main_event_loop(env, monkeypatch):
    original = env.service._evidence
    entered = Event()

    def slow(*args):
        entered.set()
        time.sleep(0.15)
        return original(*args)

    monkeypatch.setattr(env.service, "_evidence", slow)
    task = asyncio.create_task(asyncio.to_thread(save, env))
    while not entered.is_set():
        await asyncio.sleep(0.005)
    before = time.monotonic()
    await asyncio.sleep(0.02)
    assert time.monotonic() - before < 0.1
    assert (await task)["persisted"]


def test_api_response_waits_for_commit_and_commit_error_rolls_back(env, client, monkeypatch):
    http, app, auth = client
    app.dependency_overrides[auth] = lambda: "mock-admin"
    entered, release = Event(), Event()
    should_fail = False

    class CommitBarrier(sqlite3.Connection):
        def commit(self):
            entered.set()
            assert release.wait(3)
            if should_fail:
                raise sqlite3.OperationalError("private commit failure")
            return super().commit()

    def connect(**kwargs):
        connection = sqlite3.connect(env.store.path, timeout=0.1, factory=CommitBarrier)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    monkeypatch.setattr(env.store, "_connect", connect)
    url = "/api/webui/decision-observation/annotations/" + env.data.oid
    body = request_body(env)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(http.put, url, json=body)
        assert entered.wait(3)
        assert not future.done()
        # The service authorization check intentionally serializes with commit;
        # inspect SQLite directly here instead of waiting on that same barrier.
        with read_database(env.store.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM annotation_revisions").fetchone()[0] == 0
        release.set()
        assert future.result(3).status_code == 201
    should_fail = True
    entered.clear()
    response = http.put(url, json={**body, "expected_revision": 1, "idempotency_key": uuid4().hex})
    assert response.status_code == 503 and "private commit failure" not in response.text
    assert env.service.history(env.data.oid, "evaluation", env.data.eid)["total"] == 1


def test_revoke_and_regrant_cannot_validate_an_old_inflight_request(env, monkeypatch):
    entered, release = Event(), Event()
    original = env.service._evidence

    def delayed(*args):
        evidence = original(*args)
        entered.set()
        assert release.wait(3)
        return evidence

    monkeypatch.setattr(env.service, "_evidence", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(save, env)
        assert entered.wait(3)
        env.store.configure(Policy(False, frozenset()))
        env.store.configure(Policy(False, frozenset({"group-a"})))
        release.set()
        with pytest.raises(AnnotationError, match="authorization_changed"):
            future.result(3)
    assert env.service.history(env.data.oid, "evaluation", env.data.eid)["total"] == 0


def test_cross_session_targets_and_idempotency_keys_never_expose_other_labels(env, client):
    save(env, reason="private label for group a")
    with sqlite3.connect(env.store.path) as connection:
        connection.execute(
            "INSERT INTO opportunities SELECT 'other', 'group-b',created_at,run_id,policy_version,source_type,finalized_at,status,account_id FROM opportunities LIMIT 1"
        )
        connection.execute(
            "INSERT INTO evaluations SELECT 'other-eval','other',1,evaluated_at,feature_version,feature_snapshot,effective_threshold,calculated_probability,gate_result,decision_reason,weights_hash,evidence FROM evaluations LIMIT 1"
        )
    with pytest.raises(AnnotationError):
        save(env, opportunity_id="other", evaluation_id="other-eval")
    with pytest.raises(AnnotationError):
        env.service.history("other", "evaluation", "other-eval")
    http, app, auth = client
    app.dependency_overrides[auth] = lambda: "admin"
    assert (
        http.get("/api/webui/decision-observation/annotation-targets", params={"session_id": "group-b"}).status_code
        == 403
    )
    with pytest.raises(AnnotationError):
        save(env, evaluation_id="other-eval")


def test_annotation_write_does_not_initialize_or_finalize_existing_production_run(env):
    with sqlite3.connect(env.store.path) as connection:
        connection.execute("UPDATE opportunities SET status='pending',finalized_at=NULL,run_id='older-run'")
    before = facts(env.store.path)
    env.store._initialized = False
    save(env)
    assert env.store._initialized is False
    assert facts(env.store.path) == before


def test_list_pagination_partial_quality_and_read_only_history(env):
    with sqlite3.connect(env.store.path) as connection:
        connection.execute(
            "INSERT INTO evaluations SELECT 'later',opportunity_id,2,evaluated_at+1,feature_version,feature_snapshot,effective_threshold,calculated_probability,gate_result,decision_reason,weights_hash,evidence FROM evaluations LIMIT 1"
        )
    save(env, context_quality="partial_context")
    stamp = env.data.source["evaluations"][0]["evaluated_at"]
    first = env.service.targets(start=stamp - 1, end=stamp + 2, page_size=1)
    second = env.service.targets(start=stamp - 1, end=stamp + 2, page_size=1, page=2)
    assert first["total"] == 2
    assert first["items"][0]["evaluation_id"] != second["items"][0]["evaluation_id"]
    annotation = env.service.current(env.data.oid, "evaluation", env.data.eid)["annotation"]
    assert annotation["context_quality"] == "partial_context"
    assert annotation["evidence"]["derived_context_quality"] == "identity_context_available"
