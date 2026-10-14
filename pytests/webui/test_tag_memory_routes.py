"""真实 FastAPI 请求与临时 SQLite 验证；宿主机器人和认证提供方隔离。"""

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from src.services.tag_memory_store import TagCandidate, TagMemoryStore


def create_test_app(directory: Path):
    config = SimpleNamespace(chat=SimpleNamespace(tag_memory=SimpleNamespace(
        enabled=True, data_dir=str(directory), batch_size=25, max_wait_seconds=1800, batch_rules=[],
        retention_days=14, cleanup_interval_days=14)))
    sessions = {"group": SimpleNamespace(session_id="group", group_id="123"),
                "other": SimpleNamespace(session_id="other", group_id="456"),
                "private": SimpleNamespace(session_id="private", group_id=None)}

    def resolve(scope):
        if scope not in sessions:
            raise ValueError("聊天流不存在")
        return scope, "测试讨论群" if scope == "group" else "其他群"

    async def auth(x_preview_auth: str = Header(default="")):
        if x_preview_auth != "local-fixture":
            raise HTTPException(401, "未登录")
        return x_preview_auth

    @contextmanager
    def database(**kwargs):
        yield SimpleNamespace(exec=lambda statement: SimpleNamespace(all=lambda: list(sessions.values())))

    modules = {
        "src.config.config": SimpleNamespace(global_config=config),
        "src.chat.message_receive.chat_manager": SimpleNamespace(chat_manager=SimpleNamespace(
            get_existing_session_by_session_id=sessions.get, get_session_name=lambda sid: resolve(sid)[1])),
        "src.common.database.database": SimpleNamespace(get_db_session=database),
        "src.common.database.database_model": SimpleNamespace(ChatSession=object),
        "src.services.tag_memory_service": SimpleNamespace(tag_memory_service=SimpleNamespace(_resolve_scope=resolve)),
        "src.webui.dependencies": SimpleNamespace(require_auth=auth),
        "sqlmodel": SimpleNamespace(select=lambda model: None),
    }
    spec = spec_from_file_location("tag_memory_routes_fixture", Path(__file__).resolve().parents[2] / "src/webui/routers/tag_memory.py")
    module = module_from_spec(spec)
    with patch.dict("sys.modules", modules):
        spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    return app, config


class TagMemoryRoutesTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        app, self.config = create_test_app(self.path)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"x-preview-auth": "local-fixture"}
        self.store = TagMemoryStore(self.path / "memory_index.db")
        self.store.initialize()
        self.store.record_batch(session_id="group", session_name="测试讨论群", external_id="first", summary="周末活动",
                                message_ids=["m1"], tags=[TagCandidate("周末计划", "周六爬山", ("群公共", "群活动"),
                                                                     ["m1"], evidence_quotes={"m1": "周六爬山"})])
        self.tag = self.store.list_current_tags("group")[0]

    def test_authentication_required_and_only_real_groups_listed(self):
        self.assertEqual(self.client.get("/api/webui/tag-memory/sessions").status_code, 401)
        response = self.client.get("/api/webui/tag-memory/sessions", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["session_id"] for item in response.json()["sessions"]], ["group", "other"])
        self.assertEqual(self.client.get("/api/webui/tag-memory/fake", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get("/api/webui/tag-memory/private", headers=self.headers).status_code, 404)

    def test_overview_edit_detail_and_optimistic_conflict(self):
        response = self.client.get("/api/webui/tag-memory/group", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["rules"]["batch_size"], 25)
        path = f"/api/webui/tag-memory/group/tags/{self.tag['id']}"
        body = {"name": "新计划", "summary": "周日看电影", "revision": 0, "reason": "改期", "is_active": False, "requires_long_term": True}
        self.assertEqual(self.client.patch(path, json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.patch(path, json=body, headers=self.headers).status_code, 409)
        detail = self.client.get(path, headers=self.headers).json()
        self.assertEqual(detail["summary"], "周日看电影")
        self.assertEqual(detail["evidence"][0]["quote"], "周六爬山")
        self.assertEqual(len(detail["revisions"]), 1)

    def test_cross_group_writes_disabled_mode_and_validation(self):
        path = f"/api/webui/tag-memory/other/tags/{self.tag['id']}"
        response = self.client.request("DELETE", path, json={"revision": 0, "reason": "误删"}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        path = f"/api/webui/tag-memory/group/tags/{self.tag['id']}"
        self.assertEqual(self.client.request("DELETE", path, json={"revision": 0, "reason": ""}, headers=self.headers).status_code, 422)
        self.config.chat.tag_memory.enabled = False
        self.assertEqual(self.client.get("/api/webui/tag-memory/group", headers=self.headers).status_code, 409)

    def test_retry_is_authenticated_scoped_and_refuses_running_jobs(self):
        self.store.initialize_scanner(0)
        self.store.enqueue_page([{"session_id": "group", "message_id": "queued", "timestamp": 0}], 1)
        job = self.store.next_extraction_job(claim=True)
        path = f"/api/webui/tag-memory/group/extraction/{job['id']}/retry"
        self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 409)
        self.store.fail_extraction_job(job["id"], "model failed", invalid_output=True)
        foreign = f"/api/webui/tag-memory/other/extraction/{job['id']}/retry"
        self.assertEqual(self.client.post(foreign, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 200)
        status = self.client.get("/api/webui/tag-memory/group", headers=self.headers).json()["extraction"]
        self.assertEqual(status["pending_messages"], 1)
        self.assertEqual(status["job"]["retry_at"], 0)

    def test_delete_and_batch_context(self):
        batch = self.client.get(f"/api/webui/tag-memory/group/batches/{self.tag['batch_id']}", headers=self.headers)
        self.assertEqual(batch.status_code, 200)
        self.assertEqual(len(batch.json()["batches"]), 1)
        path = f"/api/webui/tag-memory/group/tags/{self.tag['id']}"
        self.assertEqual(self.client.request("DELETE", path, json={"revision": 0, "reason": "错误记录"}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(path, headers=self.headers).status_code, 400)
        self.assertEqual(self.store.search_tags("group", "周末", []), [])

    def test_conflict_resolution_and_topic_update_use_scoped_routes(self):
        self.store.record_batch(session_id="group", session_name="测试讨论群", external_id="second", summary="改期提议",
            message_ids=["m2"], tags=[TagCandidate("新计划", "周日看电影", ("群公共", "群活动"), ["m2"])],
            conflict_decisions=[{"new_index": 0, "old_tag_id": self.tag["id"], "old_revision": 0,
                                 "decision": "uncertain", "reason": "待确认"}])
        conflict = self.store.get_conflicts("group")[0]
        path = f"/api/webui/tag-memory/group/conflicts/{conflict['id']}/resolve"
        payload = {"decision": "keep_old", "reason": "本人确认"}
        self.assertEqual(self.client.post(path, json=payload, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(path, json=payload, headers=self.headers).status_code, 409)
        with self.store._connect(write=True) as db:
            db.execute("INSERT INTO topics VALUES(?,?,?,?,?,?)", ("topic", "group", "周末计划", "待确认", 0, 1))
            db.execute("INSERT INTO topic_tags VALUES(?,?,?)", ("group", "topic", self.tag["id"]))
        self.assertEqual(self.client.patch("/api/webui/tag-memory/group/topics/topic", json={"active": True}, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.get_topics("group")[0]["is_active"], 1)

    def test_quarantined_job_recovery_is_authenticated_and_scoped(self):
        self.store.initialize_scanner(0)
        self.store.enqueue_page([{"session_id": "group", "message_id": "pending", "timestamp": 0,
                                  "text": "test", "user_id": "user"}], 1)
        job = self.store.next_extraction_job(1, 1)
        for _ in range(5):
            self.store.fail_extraction_job(job["id"], "invalid", invalid_output=True)
        path = f"/api/webui/tag-memory/group/extraction-jobs/{job['id']}/retry"
        self.assertEqual(self.client.post(path).status_code, 401)
        wrong = path.replace("/group/", "/other/")
        self.assertEqual(self.client.post(wrong, headers=self.headers).status_code, 400)
        jobs = self.client.get("/api/webui/tag-memory/group/extraction-jobs", headers=self.headers).json()["jobs"]
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.list_extraction_jobs("group")[0]["attempts"], 0)
