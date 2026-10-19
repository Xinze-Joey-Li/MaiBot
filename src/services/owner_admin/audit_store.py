"""与聊天统计分离的主人管理审计与幂等请求存储。"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from .models import AdminActionRequest


class OwnerAdminAuditStore:
    """将审计事件写入独立 SQLite 文件；每次操作在线程池中打开短连接。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def default_path() -> Path:
        return Path(__file__).resolve().parents[3] / "data" / "owner_admin" / "audit.sqlite3"

    def reserve(
        self,
        request: AdminActionRequest,
        *,
        authorized: bool,
        authorization_error: Optional[str],
        parameter_summary: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """原子地登记请求及审计起始事件，返回是否首次请求及审计 ID。"""

        self._ensure_parent()
        audit_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        identity = request.actor_identity
        target = request.target
        with sqlite3.connect(self.path, timeout=5.0) as connection:
            self._create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM owner_admin_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """INSERT INTO owner_admin_audit (
                        audit_id, request_id, requested_at, platform, bot_account_id, user_id,
                        chat_type, session_id, chat_id, action_id, target_summary, parameter_summary,
                        authorization_result, execution_result, error_code, source_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        audit_id,
                        request.request_id,
                        now,
                        identity.platform,
                        identity.bot_account_id,
                        identity.user_id,
                        identity.chat_type,
                        identity.session_id,
                        identity.chat_id,
                        request.action_id,
                        self._json({"resource_type": target.resource_type, "resource_id": target.resource_id}),
                        self._json(parameter_summary),
                        "not_rechecked",
                        "duplicate_prevented",
                        "duplicate_request",
                        request.source_message_id,
                    ),
                )
                connection.commit()
                return False, audit_id

            status = "running" if authorized else "completed"
            connection.execute(
                "INSERT INTO owner_admin_requests (request_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (request.request_id, status, now, now),
            )
            connection.execute(
                """INSERT INTO owner_admin_audit (
                    audit_id, request_id, requested_at, platform, bot_account_id, user_id,
                    chat_type, session_id, chat_id, action_id, target_summary, parameter_summary,
                    authorization_result, execution_result, error_code, source_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id,
                    request.request_id,
                    now,
                    identity.platform,
                    identity.bot_account_id,
                    identity.user_id,
                    identity.chat_type,
                    identity.session_id,
                    identity.chat_id,
                    request.action_id,
                    self._json({"resource_type": target.resource_type, "resource_id": target.resource_id}),
                    self._json(parameter_summary),
                    "allowed" if authorized else "denied",
                    "pending" if authorized else "not_executed",
                    None if authorized else authorization_error,
                    request.source_message_id,
                ),
            )
            connection.commit()
        return True, audit_id

    def complete(
        self,
        request_id: str,
        audit_id: str,
        *,
        status: str,
        error_code: Optional[str],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path, timeout=5.0) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE owner_admin_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, now, request_id),
            )
            connection.execute(
                "UPDATE owner_admin_audit SET execution_result = ?, error_code = ? WHERE audit_id = ?",
                (status, error_code, audit_id),
            )
            connection.commit()

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS owner_admin_requests (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS owner_admin_audit (
                audit_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                platform TEXT NOT NULL,
                bot_account_id TEXT,
                user_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                session_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                target_summary TEXT NOT NULL,
                parameter_summary TEXT NOT NULL,
                authorization_result TEXT NOT NULL,
                execution_result TEXT NOT NULL,
                error_code TEXT,
                source_message_id TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_owner_admin_audit_request ON owner_admin_audit(request_id)"
        )

    @staticmethod
    def _json(value: Mapping[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
