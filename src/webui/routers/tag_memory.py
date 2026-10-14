"""树状标签记忆管理；同步端点由 WebUI 线程池执行 SQLite 工作。"""

from pathlib import Path
from typing import Any, Callable, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from src.chat.message_receive.chat_manager import chat_manager
from src.common.database.database import get_db_session
from src.common.database.database_model import ChatSession
from src.config.config import global_config
from src.services.tag_memory_service import tag_memory_service
from src.services.tag_extraction_service import TagExtractionService
from src.services.tag_memory_store import StaleTagConflict, TagMemoryStore
from src.webui.dependencies import require_auth

router = APIRouter(prefix="/api/webui/tag-memory", tags=["tag-memory"], dependencies=[Depends(require_auth)])


class TagEdit(BaseModel):
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=600)
    is_active: bool
    requires_long_term: bool
    reason: str = Field(min_length=1, max_length=1000)


class TagDelete(BaseModel):
    revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class ConflictResolution(BaseModel):
    decision: Literal["replace_old", "keep_old", "uncertain"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_resolved_at: Optional[float] = None


class TopicUpdate(BaseModel):
    active: bool


def scoped_store(session_id: str) -> Tuple[TagMemoryStore, str]:
    if not global_config.chat.tag_memory.enabled:
        raise HTTPException(409, "标签记忆未启用")
    try:
        scope, name = tag_memory_service._resolve_scope(session_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
    session = chat_manager.get_existing_session_by_session_id(scope)
    if session is None or not session.group_id:
        raise HTTPException(404, "标签记忆只管理已存在的群会话")
    directory = Path(global_config.chat.tag_memory.data_dir)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parents[3] / directory
    store = TagMemoryStore(directory / "memory_index.db")
    store.initialize()
    store.ensure_scope(scope, name)
    return store, scope


def perform(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return operation(*args, **kwargs)
    except StaleTagConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/sessions")
def sessions():
    # 用数据库中的真实聊天流列表；显示群名，不自行生成归属 ID。
    with get_db_session(auto_commit=False) as db:
        rows = db.exec(select(ChatSession)).all()
    result = []
    for row in rows:
        session = chat_manager.get_existing_session_by_session_id(row.session_id)
        if session is not None and session.group_id:
            result.append({"session_id": session.session_id, "name": chat_manager.get_session_name(session.session_id)})
    return {"sessions": result, "enabled": global_config.chat.tag_memory.enabled}


@router.get("/{session_id}")
def overview(session_id: str):
    store, scope = scoped_store(session_id)
    config = global_config.chat.tag_memory
    return {"tree": store.get_tree(scope), "topics": store.get_topics(scope), "conflicts": store.get_conflicts(scope),
            "extraction": store.extraction_status(scope),
            "rules": {"batch_size": TagExtractionService.batch_sizes_for_sessions(config.batch_rules).get(scope, config.batch_size),
                      "max_wait_seconds": config.max_wait_seconds,
                      "retention_days": config.retention_days, "cleanup_interval_days": config.cleanup_interval_days}}


@router.get("/{session_id}/tags/{tag_id}")
def tag_detail(session_id: str, tag_id: str):
    store, scope = scoped_store(session_id)
    return perform(store.tag_detail, scope, tag_id)


@router.patch("/{session_id}/tags/{tag_id}")
def edit_tag(session_id: str, tag_id: str, body: TagEdit):
    store, scope = scoped_store(session_id)
    perform(store.edit_tag, scope, tag_id, **body.model_dump())
    return {"success": True}


@router.delete("/{session_id}/tags/{tag_id}")
def delete_tag(session_id: str, tag_id: str, body: TagDelete):
    store, scope = scoped_store(session_id)
    perform(store.delete_tag, scope, tag_id, body.revision, body.reason)
    return {"success": True}


@router.post("/{session_id}/conflicts/{conflict_id}/resolve")
def resolve_conflict(session_id: str, conflict_id: str, body: ConflictResolution):
    store, scope = scoped_store(session_id)
    perform(store.resolve_conflict, scope, conflict_id, body.decision, body.reason, body.expected_resolved_at)
    return {"success": True}


@router.patch("/{session_id}/topics/{topic_id}")
def update_topic(session_id: str, topic_id: str, body: TopicUpdate):
    store, scope = scoped_store(session_id)
    perform(store.set_topic_active, scope, topic_id, body.active)
    return {"success": True}


@router.get("/{session_id}/batches/{batch_id}")
def batch_context(session_id: str, batch_id: str):
    store, scope = scoped_store(session_id)
    return {"batches": store.get_batch_context(scope, batch_id)}


@router.post("/{session_id}/extraction/{job_id}/retry")
def retry_extraction(session_id: str, job_id: str):
    store, scope = scoped_store(session_id)
    perform(store.retry_extraction_job, scope, job_id)
    # WebUI 与 bot 分属不同事件循环；由 bot 的定期扫描拾取，不能直接 set 它的 Event。
    return {"success": True}


@router.get("/{session_id}/extraction-jobs")
def extraction_jobs(session_id: str):
    store, scope = scoped_store(session_id)
    return {"jobs": perform(store.list_extraction_jobs, scope)}


@router.post("/{session_id}/extraction-jobs/{job_id}/retry")
def retry_extraction_job(session_id: str, job_id: str):
    store, scope = scoped_store(session_id)
    perform(store.retry_extraction_job, scope, job_id)
    return {"success": True}
