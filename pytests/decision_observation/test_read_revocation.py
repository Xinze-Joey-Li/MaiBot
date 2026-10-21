"""Synthetic-only, deterministic read authorization and ASGI delivery races."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
import asyncio
import gzip
import hashlib
import sqlite3

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
import pytest

from src.decision_observation import recorder
from src.decision_observation import annotations, dashboard
from src.decision_observation.dashboard import DashboardReader
from src.decision_observation.models import Policy
from src.decision_observation.read_authorization import ReadAuthorization, ReadAuthorizationExpired
from src.webui.dependencies import require_auth
from src.webui.routers import decision_observation as api
from src.webui.routers import decision_annotations as annotation_api
from src.webui.observation_read import AuthorizedReadResponse, ObservationReadMiddleware
from test_annotations import env as _env, fixture as _fixture

env = _env
fixture = _fixture
PREFIX = "/api/webui/decision-observation"


def test_ia1_old_admitted_reader_cannot_outlive_revocation(env, monkeypatch):
    monkeypatch.setattr(recorder, "store", env.store)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
    admitted, release = Event(), Event()

    def old_reader():
        captured = DashboardReader(env.store.path, env.data.chat, env.data.reader.replay_path,
                                   env.store.policy.authorized_session_ids)
        admitted.set()
        assert release.wait(5), "read barrier timed out"
        return captured

    app.dependency_overrides[api.reader] = old_reader
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, PREFIX + "/opportunities/" + env.data.oid)
        try:
            assert admitted.wait(5), "request did not reach read barrier"
            env.store.configure(Policy(False, frozenset()))
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == 404
    assert env.data.oid not in response.text


def application(env, monkeypatch):
    monkeypatch.setattr(recorder, "store", env.store)
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(annotation_api.router)
    app.add_middleware(ObservationReadMiddleware)
    app.dependency_overrides[require_auth] = lambda: "synthetic-admin"

    def bound_reader(request: Request):
        return DashboardReader(env.store.path, env.data.chat, env.data.reader.replay_path,
                               env.store.policy.authorized_session_ids,
                               read_authorization=request.state.observation_read_authorization)

    app.dependency_overrides[api.reader] = bound_reader
    app.dependency_overrides[annotation_api.annotation_service] = lambda: env.service
    return app


KINDS = ["health", "summary", "list", "detail", "context", "targets", "current", "history"]


def url(env, kind):
    window = f"start={env.data.start}&end={env.data.end}"
    if kind in {"summary", "list", "targets"}:
        name = {"summary": "summary", "list": "opportunities", "targets": "annotation-targets"}[kind]
        return PREFIX + "/" + name + "?" + window
    if kind == "health":
        return PREFIX + "/health"
    if kind in {"detail", "context"}:
        result = PREFIX + "/opportunities/" + env.data.oid
        return result + "/context?evaluation_id=" + env.data.eid if kind == "context" else result
    result = PREFIX + "/annotations/" + env.data.oid
    if kind == "history":
        result += "/history"
    return result + "?target_type=evaluation&evaluation_id=" + env.data.eid


@pytest.mark.parametrize("kind", KINDS)
def test_normal_reads_readonly_and_fresh_revocation(env, monkeypatch, kind):
    app = application(env, monkeypatch)
    before = hashlib.sha256(env.store.path.read_bytes()).hexdigest()
    with TestClient(app) as client:
        assert client.get(url(env, kind)).status_code == 200
        assert hashlib.sha256(env.store.path.read_bytes()).hexdigest() == before
        app.dependency_overrides.pop(require_auth)
        assert client.get(url(env, kind)).status_code == 401
        app.dependency_overrides[require_auth] = lambda: "synthetic-admin"
        env.store.configure(Policy(False, frozenset()))
        response = client.get(url(env, kind))
    if kind in {"detail", "context", "current", "history"}:
        assert response.status_code == 404
    else:
        assert response.status_code == 200
        assert "模拟群聊" not in response.text and env.data.oid not in response.text
        if kind == "health":
            assert response.json()["sessions"] == []
        else:
            assert response.json()["total"] == 0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("phase", ["reading", "constructed", "serializing", "asgi_entry"])
@pytest.mark.parametrize("reauthorize", [False, True])
def test_epoch_races_at_query_serialization_and_asgi(env, monkeypatch, kind, phase, reauthorize):
    app = application(env, monkeypatch)
    entered, release = Event(), Event()
    armed = True

    def barrier():
        nonlocal armed
        if armed:
            armed = False
            entered.set()
            assert release.wait(5), "phase barrier timed out"

    if phase == "reading":
        module = annotations if kind in {"targets", "current", "history"} else dashboard
        original = module.read_database

        @contextmanager
        def gated(path):
            with original(path) as connection:
                barrier()
                yield connection

        monkeypatch.setattr(module, "read_database", gated)
    elif phase == "constructed":
        cls = annotations.AnnotationService if kind in {"targets", "current", "history"} else DashboardReader
        name = {"list": "listing", "summary": "listing", "targets": "targets"}.get(kind, kind)
        original = getattr(cls, name)

        def gated(self, *args, **kwargs):
            value = original(self, *args, **kwargs)
            barrier()
            return value

        monkeypatch.setattr(cls, name, gated)
    elif phase == "serializing":
        original = JSONResponse.render

        def gated(self, content):
            # Test-only synchronous pause in the existing serialization phase;
            # revocation runs in a separate thread, with no sleep timing guesses.
            if not isinstance(content, dict) or "detail" not in content:
                barrier()
            return original(self, content)

        monkeypatch.setattr(JSONResponse, "render", gated)
    else:
        original = AuthorizedReadResponse.verify_for_send

        async def gated(self):
            await asyncio.to_thread(barrier)
            await original(self)

        monkeypatch.setattr(AuthorizedReadResponse, "verify_for_send", gated)
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, url(env, kind))
        try:
            assert entered.wait(5), "request did not reach requested phase"
            env.store.configure(Policy(False, frozenset()))
            if reauthorize:
                env.store.configure(Policy(False, frozenset({"group-a"})))
        finally:
            release.set()
        response = pending.result(timeout=5)
        assert response.status_code in {403, 404, 409}
        assert env.data.oid not in response.text and "模拟消息" not in response.text
        if reauthorize:
            assert client.get(url(env, kind)).status_code == 200


def test_multigroup_filters_summary_and_guessed_ids(env, monkeypatch):
    with sqlite3.connect(env.store.path) as connection:
        connection.execute("INSERT INTO opportunities SELECT 'b-op','group-b',created_at,run_id,policy_version,source_type,finalized_at,status,account_id FROM opportunities LIMIT 1")
    with sqlite3.connect(env.data.chat) as connection:
        connection.execute("INSERT INTO chat_sessions VALUES('group-b','qq','synthetic-b','模拟群B')")
    env.store.configure(Policy(False, frozenset({"group-a", "group-b"})))
    app = application(env, monkeypatch)
    with TestClient(app) as client:
        assert client.get(url(env, "list")).json()["total"] == 2
        assert client.get(url(env, "list") + "&session_id=group-b").json()["total"] == 1
        env.store.configure(Policy(False, frozenset({"group-b"})))
        for kind in ["summary", "list", "targets"]:
            data = client.get(url(env, kind)).json()
            assert data["total"] == 1
            assert all(item["session_id"] == "group-b" for item in data["items"])
        assert client.get(url(env, "list") + "&session_id=group-a").status_code == 403
        assert client.get(url(env, "targets") + "&session_id=group-a").status_code == 403
        assert client.get(url(env, "detail")).status_code == 404
        assert client.get(url(env, "context")).status_code == 404
        assert client.get(url(env, "current")).status_code == 404
        assert client.get(url(env, "history")).status_code == 404
        assert client.get(PREFIX + "/opportunities/b-op/context?evaluation_id=" + env.data.eid).status_code == 404
        assert client.get(PREFIX + "/annotations/b-op?target_type=evaluation&evaluation_id=" + env.data.eid).status_code == 404
        assert client.get(url(env, "list") + "&page_size=1&page=2").json()["items"] == []


@pytest.mark.parametrize("kind", KINDS)
def test_missing_database_never_created(env, monkeypatch, kind, tmp_path):
    env.store.path = tmp_path / "never-created.db"
    app = application(env, monkeypatch)
    with TestClient(app) as client:
        response = client.get(url(env, kind))
    assert response.status_code == (404 if kind in {"detail", "context", "current", "history"} else 200)
    assert not env.store.path.exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_collection_switch_does_not_revoke_historical_permission(env, monkeypatch, enabled):
    env.store.configure(Policy(enabled, frozenset({"group-a"})))
    app = application(env, monkeypatch)
    with TestClient(app) as client:
        for kind in KINDS:
            assert client.get(url(env, kind)).status_code == 200


def test_delete_tombstone_and_reauthorization_never_revive_old_reader(env):
    captured = ReadAuthorization(env.store)
    reader = DashboardReader(env.store.path, env.data.chat, env.data.reader.replay_path, captured.sessions,
                             read_authorization=captured)
    env.store.delete_session("group-a")
    env.store.configure(Policy(False, frozenset()))
    env.store.configure(Policy(False, frozenset({"group-a"})))
    with pytest.raises(ReadAuthorizationExpired):
        reader.detail(env.data.oid)
    assert ReadAuthorization(env.store).sessions == frozenset()
    assert env.store.annotation_generation("group-a") is None


def test_delete_and_revoke_during_admitted_read(env, monkeypatch):
    app = application(env, monkeypatch)
    entered, release = Event(), Event()
    original = DashboardReader.detail

    def gated(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(DashboardReader, "detail", gated)
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.get, url(env, "detail"))
        try:
            assert entered.wait(5)
            delete = pool.submit(env.store.delete_session, "group-a")
            revoke = pool.submit(env.store.configure, Policy(False, frozenset()))
            delete.result(timeout=5)
            revoke.result(timeout=5)
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight", ["headers", "body"])
async def test_asgi_admitted_send_boundary_is_explicit(env, inflight):
    authorization = ReadAuthorization(env.store)
    response = AuthorizedReadResponse(JSONResponse({"synthetic_secret": "PRIVATE_ASGI_BODY"}), authorization,
                                      JSONResponse({"detail": "revoked"}, status_code=403))
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []

    async def send(message):
        target = "http.response.start" if inflight == "headers" else "http.response.body"
        if message["type"] == target:
            entered.set()
            await release.wait()
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    task = asyncio.create_task(response({"type": "http", "method": "GET"}, receive, send))
    await asyncio.wait_for(entered.wait(), 5)
    # This finishes while send is still waiting: no global lock spans transport I/O.
    await asyncio.wait_for(asyncio.to_thread(env.store.configure, Policy(False, frozenset())), 5)
    release.set()
    if inflight == "headers":
        with pytest.raises(ReadAuthorizationExpired):
            await task
        assert not any(message.get("body") for message in sent)
    else:
        await task
        # Deliberately demonstrate the limit: bytes already admitted before
        # invalidation may finish sending later; no fake network barrier is claimed.
        assert any(b"PRIVATE_ASGI_BODY" in message.get("body", b"") for message in sent)


@pytest.mark.asyncio
async def test_contended_authorization_lock_does_not_block_webui_loop(env):
    authorization = ReadAuthorization(env.store)
    entered = Event()
    original = env.store.read_authorization_valid

    def checked(*args):
        entered.set()
        return original(*args)

    env.store.read_authorization_valid = checked
    with env.store._annotation_authorization_lock:
        pending = asyncio.create_task(asyncio.to_thread(authorization.verify))
        assert await asyncio.to_thread(entered.wait, 5)
        responsive = asyncio.Event()
        asyncio.get_running_loop().call_soon(responsive.set)
        await asyncio.wait_for(responsive.wait(), 1)
    await pending


@pytest.mark.asyncio
async def test_production_outer_guard_rechecks_after_gzip_buffering(env, monkeypatch):
    from src.webui.middleware.compression import TextGZipMiddleware

    entered, release = Event(), Event()
    original = gzip.compress

    def compress(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(gzip, "compress", compress)
    monkeypatch.setattr(recorder, "store", env.store)
    response = JSONResponse({"synthetic_secret": "PRIVATE_GZIP_BODY" + "x" * 70000})
    app = ObservationReadMiddleware(TextGZipMiddleware(response))
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    task = asyncio.create_task(app({"type": "http", "method": "GET", "path": PREFIX + "/summary",
                                    "headers": [(b"accept-encoding", b"gzip")]}, receive, send))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert sent == []  # Outer middleware has buffered both admitted messages.
        await asyncio.wait_for(asyncio.to_thread(env.store.configure, Policy(False, frozenset())), 5)
    finally:
        release.set()
    await task
    body = b"".join(message.get("body", b"") for message in sent)
    assert sent[0]["status"] == 403
    assert b"PRIVATE_GZIP_BODY" not in body
    assert b"content-encoding" not in dict(sent[0]["headers"])


def test_real_app_installs_guard_outside_gzip(env, monkeypatch):
    from src.webui.app import create_app
    from src.webui.middleware.compression import TextGZipMiddleware

    mounted = application(env, monkeypatch)
    app = create_app(enable_static=False)
    app.dependency_overrides.update(mounted.dependency_overrides)
    middleware = [item.cls for item in app.user_middleware]
    assert middleware.index(ObservationReadMiddleware) < middleware.index(TextGZipMiddleware)
    with TestClient(app) as client:
        assert client.get(url(env, "detail")).status_code == 200
        assert client.get(url(env, "current")).status_code == 200
