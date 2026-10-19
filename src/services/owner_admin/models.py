"""主人管理基础设施使用的不可变数据对象。"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class AdminIdentity:
    """从单条可信平台入站消息解析出的操作者身份。"""

    platform: str
    bot_account_id: Optional[str]
    user_id: str
    chat_type: str
    session_id: str
    chat_id: str
    trusted_platform_metadata: bool
    bot_mentioned: bool = False
    bot_account_verified: bool = False


@dataclass(frozen=True)
class AdminTarget:
    """动作的资源目标及发起该动作的聊天范围。"""

    resource_type: str
    resource_id: str
    platform: str
    bot_account_id: Optional[str]
    chat_type: str
    session_id: str
    chat_id: str


@dataclass(frozen=True)
class AdminActionRequest:
    """已解析但尚未授权或执行的管理动作请求。"""

    action_id: str
    actor_identity: AdminIdentity
    target: AdminTarget
    parameters: Mapping[str, Any] = field(default_factory=dict)
    request_id: str = ""
    source_message_id: str = ""


@dataclass(frozen=True)
class AdminActionResult:
    """结构化管理动作结果。"""

    success: bool
    status: str
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    audit_id: Optional[str] = None


@dataclass(frozen=True)
class PermissionDecision:
    """OwnerPermissionGuard 的授权结论。"""

    allowed: bool
    error_code: Optional[str] = None


@dataclass(frozen=True)
class AdminActionSpec:
    """已注册动作的静态授权与审计策略。"""

    action_id: str
    permission_id: str
    allowed_target_types: frozenset[str]
    owner_required: bool
    allowed_chat_types: frozenset[str] = frozenset({"group", "private"})
    require_bot_account: bool = True
    require_bot_mention: bool = False
    require_group_commands_enabled: bool = False
    audit_parameter_keys: tuple[str, ...] = ()
