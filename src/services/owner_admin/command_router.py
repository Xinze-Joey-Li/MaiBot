"""/dfy 管理命令路由与已注册动作执行。"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Mapping, Optional

import asyncio
import hashlib
import logging
import re

from src.common.data_models.message_component_data_model import (
    AtComponent,
    ForwardNodeComponent,
    ReplyComponent,
    TextComponent,
)

from .action_registry import ActionRegistry
from .audit_store import OwnerAdminAuditStore
from .identity import OwnerIdentityResolver
from .models import AdminActionRequest, AdminActionResult, AdminActionSpec, AdminIdentity, AdminTarget
from .permissions import OwnerPermissionGuard

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage

logger = logging.getLogger("owner_admin")
_COMMAND_PATTERN = re.compile(r"^/(?:dfy|主人)(?:\s|$)")
_SENSITIVE_PARAMETER_KEY = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|cookie|credential)")
_SECRET_VALUE = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{16,}|AIza[a-z0-9_-]{30,}|gh[pousr]_[a-z0-9]{20,}|"
    r"github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{10,}|bearer\s+\S+)"
)

ActionHandler = Callable[[AdminActionRequest], Awaitable[AdminActionResult]]


class OwnerAdminCommandRouter:
    """将直接管理命令映射到固定动作，并在动作执行前统一授权和审计。"""

    def __init__(
        self,
        *,
        registry: Optional[ActionRegistry] = None,
        audit_store: Optional[OwnerAdminAuditStore] = None,
        permission_guard: Optional[OwnerPermissionGuard] = None,
    ) -> None:
        self.registry = registry or ActionRegistry()
        self.audit_store = audit_store or OwnerAdminAuditStore(OwnerAdminAuditStore.default_path())
        self.permission_guard = permission_guard or OwnerPermissionGuard()
        self._handlers: Dict[str, ActionHandler] = {}
        self._resource_locks: Dict[tuple[str, str], asyncio.Lock] = {}
        if registry is None:
            self._register_builtin_actions()
            from .read_commands import READ_ACTIONS
            from .read_service import OwnerReadService

            read_service = OwnerReadService()
            for command, (permission_id, _) in READ_ACTIONS.items():
                self.register_action(
                    AdminActionSpec(
                        f"owner_admin.read.{command}", permission_id, frozenset({"chat"}),
                        owner_required=True, allowed_chat_types=frozenset({"private"}),
                        audit_parameter_keys=("window", "page"),
                    ),
                    read_service.handle,
                )

    def _register_builtin_actions(self) -> None:
        self.register_action(
            AdminActionSpec(
                "owner_admin.help",
                "owner_admin.help",
                frozenset({"chat"}),
                owner_required=False,
                require_bot_account=False,
            ),
            self._help,
        )
        self.register_action(
            AdminActionSpec(
                "owner_admin.whoami",
                "owner_admin.identity.read",
                frozenset({"chat"}),
                owner_required=False,
                require_bot_account=False,
            ),
            self._whoami,
        )
        self.register_action(
            AdminActionSpec(
                "owner.personality.read",
                "owner.personality.read",
                frozenset({"chat"}),
                owner_required=True,
                allowed_chat_types=frozenset({"group"}),
                require_bot_account=True,
                require_bot_mention=True,
                require_group_commands_enabled=True,
            ),
            self._legacy_personality,
        )
        self.register_action(
            AdminActionSpec(
                "owner.models.list",
                "owner.models.read",
                frozenset({"chat"}),
                owner_required=True,
                allowed_chat_types=frozenset({"group"}),
                require_bot_account=True,
                require_bot_mention=True,
                require_group_commands_enabled=True,
            ),
            self._legacy_models,
        )
        self.register_action(
            AdminActionSpec(
                "owner.models.select",
                "owner.models.update",
                frozenset({"system_config"}),
                owner_required=True,
                allowed_chat_types=frozenset({"group"}),
                require_bot_account=True,
                require_bot_mention=True,
                require_group_commands_enabled=True,
                audit_parameter_keys=("task", "model"),
            ),
            self._legacy_model_select,
        )
        self.register_action(
            AdminActionSpec(
                "owner.legacy.help",
                "owner.legacy.help",
                frozenset({"chat"}),
                owner_required=True,
                allowed_chat_types=frozenset({"group"}),
                require_bot_account=True,
                require_bot_mention=True,
                require_group_commands_enabled=True,
            ),
            self._legacy_help,
        )

    def register_action(self, spec: AdminActionSpec, handler: ActionHandler) -> None:
        """注册固定动作与处理器；未注册动作始终保持拒绝。"""

        self.registry.register(spec)
        self._handlers[spec.action_id] = handler

    @staticmethod
    def command_body(message: "SessionMessage") -> Optional[str]:
        """仅解析消息顶层的纯文本和 @；引用/转发等复合消息不会成为命令。"""

        parts: list[str] = []
        for component in message.raw_message.components:
            if isinstance(component, TextComponent):
                parts.append(component.text)
            elif isinstance(component, AtComponent):
                continue
            else:
                return None
        body = "".join(parts).strip()
        if _COMMAND_PATTERN.match(body):
            return body
        if body in {"输出简化人格模板", "输出简化人格", "请输出简化人格模板", "列出可选模型", "查看可选模型"}:
            return body
        if body.startswith("选择模型 "):
            return body
        return None

    @classmethod
    def is_legacy_command(cls, message: "SessionMessage") -> bool:
        body = cls.command_body(message)
        return bool(
            body
            and (
                body.startswith("/主人")
                or body in {"输出简化人格模板", "输出简化人格", "请输出简化人格模板", "列出可选模型", "查看可选模型"}
                or body.startswith("选择模型 ")
            )
        )

    @classmethod
    def contains_embedded_command(cls, message: "SessionMessage") -> bool:
        """识别引用或合并转发中的命令字样，阻止它们进入管理动作或 Planner。"""

        def has_marker(components: list[object]) -> bool:
            for component in components:
                if isinstance(component, TextComponent) and _COMMAND_PATTERN.match(component.text.strip()):
                    return True
                if isinstance(component, ReplyComponent):
                    if component.target_message_content and _COMMAND_PATTERN.match(component.target_message_content.strip()):
                        return True
                if isinstance(component, ForwardNodeComponent):
                    if any(has_marker(node.content) for node in component.forward_components):
                        return True
            return False

        return has_marker(message.raw_message.components)

    @staticmethod
    def is_management_command(message: "SessionMessage") -> bool:
        return OwnerAdminCommandRouter.command_body(message) is not None

    async def handle_message(self, message: "SessionMessage") -> Optional[AdminActionResult]:
        body = self.command_body(message)
        if body is None:
            if self.contains_embedded_command(message):
                return await self._reject_embedded_command(message)
            return None

        parsed = self._parse(body)
        from src.config.config import global_config

        bot_mentioned = False
        if parsed.action_id.startswith("owner."):
            from src.chat.utils.utils import is_mentioned_bot_in_message

            try:
                bot_mentioned = await asyncio.to_thread(is_mentioned_bot_in_message, message)
            except Exception:
                logger.exception("无法确认旧主人命令是否 @ 了机器人；按未提及处理")
        identity = OwnerIdentityResolver.from_message(
            message,
            bot_mentioned=bot_mentioned,
        )
        target = self._target_for(parsed.action_id, identity)
        request_id = self._request_id(identity, parsed.action_id, message.message_id)
        request = AdminActionRequest(
            action_id=parsed.action_id,
            actor_identity=identity,
            target=target,
            parameters=parsed.parameters,
            request_id=request_id,
            source_message_id=str(message.message_id),
        )
        spec = self.registry.get(parsed.action_id)
        if spec is not None and spec.require_bot_account and identity.bot_account_id:
            from src.services.bot_account_service import bot_account_service

            try:
                account_verified = await asyncio.to_thread(
                    bot_account_service.is_bot_self,
                    identity.platform,
                    identity.bot_account_id,
                )
            except Exception:
                logger.exception("无法确认当前 Bot 账号绑定；按未验证账号处理")
                account_verified = False
            identity = replace(identity, bot_account_verified=account_verified)
            request = replace(request, actor_identity=identity)
        decision = self.permission_guard.authorize(
            request,
            spec,
            owner_user_id=global_config.bot.owner_qq,
            group_commands_enabled=global_config.bot.owner_group_commands_enabled,
        )
        parameter_summary = self._parameter_summary(spec, parsed.parameters)
        try:
            is_new, audit_id = await asyncio.to_thread(
                self.audit_store.reserve,
                request,
                authorized=decision.allowed,
                authorization_error=decision.error_code,
                parameter_summary=parameter_summary,
            )
        except Exception:
            logger.exception("主人管理审计存储不可用；请求未执行")
            return AdminActionResult(
                success=False,
                status="unavailable",
                message="管理审计暂不可用，操作未执行。",
                error_code="audit_unavailable",
            )

        if not is_new:
            return AdminActionResult(
                success=False,
                status="duplicate",
                message="该请求正在处理或已经处理，未重复执行。",
                error_code="duplicate_request",
                audit_id=audit_id,
            )

        if not decision.allowed:
            result = AdminActionResult(
                success=False,
                status="denied",
                message=self._denial_message(decision.error_code),
                error_code=decision.error_code or "permission_denied",
                audit_id=audit_id,
            )
            await self._complete_safely(request.request_id, audit_id, result)
            return result

        handler = self._handlers.get(parsed.action_id)
        if handler is None:
            result = AdminActionResult(
                success=False,
                status="denied",
                message="这条管理操作未获授权，未执行。",
                error_code="unregistered_action",
                audit_id=audit_id,
            )
            await self._complete_safely(request.request_id, audit_id, result)
            return result

        try:
            lock_key = (request.target.resource_type, request.target.resource_id)
            lock = self._resource_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                result = await handler(request)
        except Exception:
            logger.exception("主人管理动作执行失败: %s", parsed.action_id)
            result = AdminActionResult(
                success=False,
                status="failed",
                message="管理操作执行失败，请检查本地日志。",
                error_code="action_failed",
            )
        result = replace(result, audit_id=audit_id)
        await self._complete_safely(request.request_id, audit_id, result)
        return result

    async def _complete_safely(self, request_id: str, audit_id: str, result: AdminActionResult) -> None:
        try:
            await asyncio.to_thread(
                self.audit_store.complete,
                request_id,
                audit_id,
                status=result.status,
                error_code=result.error_code,
            )
        except Exception:
            # 请求保留在 running/pending 状态，重试将被幂等表拒绝，避免未记录副作用再次执行。
            logger.exception("主人管理审计结果回写失败: request_id=%s", request_id)

    async def _reject_embedded_command(self, message: "SessionMessage") -> AdminActionResult:
        from src.config.config import global_config

        identity = OwnerIdentityResolver.from_message(message)
        action_id = "owner_admin.embedded_command"
        request = AdminActionRequest(
            action_id=action_id,
            actor_identity=identity,
            target=self._target_for(action_id, identity),
            request_id=self._request_id(identity, action_id, message.message_id),
            source_message_id=str(message.message_id),
        )
        decision = self.permission_guard.authorize(
            request,
            None,
            owner_user_id=global_config.bot.owner_qq,
            group_commands_enabled=global_config.bot.owner_group_commands_enabled,
        )
        try:
            is_new, audit_id = await asyncio.to_thread(
                self.audit_store.reserve,
                request,
                authorized=False,
                authorization_error=decision.error_code,
                parameter_summary={},
            )
        except Exception:
            logger.exception("主人管理拒绝审计存储不可用")
            return AdminActionResult(False, "unavailable", "管理审计暂不可用，操作未执行。", error_code="audit_unavailable")
        if not is_new:
            return AdminActionResult(
                False,
                "duplicate",
                "该请求正在处理或已经处理，未重复执行。",
                error_code="duplicate_request",
                audit_id=audit_id,
            )
        result = AdminActionResult(
            False,
            "rejected",
            "引用或转发中的管理命令不会执行。",
            error_code="embedded_command",
            audit_id=audit_id,
        )
        await self._complete_safely(request.request_id, audit_id, result)
        return result

    @staticmethod
    def _denial_message(error_code: Optional[str]) -> str:
        if error_code == "not_owner":
            return "这条管理命令只接受后台绑定的真实主人账号。"
        if error_code == "bot_mention_required":
            return "请在群聊中 @ 我后使用 /主人 命令。"
        if error_code == "group_commands_disabled":
            return "群聊管理命令已在后台关闭。"
        return "这条管理操作未获授权，未执行。"

    async def _help(self, request: AdminActionRequest) -> AdminActionResult:
        return AdminActionResult(
            success=True,
            status="succeeded",
            message="主人管理：/dfy help、/dfy whoami。主人私聊只读查询：status、tokens 1h/24h/7d、"
                    "stats [群ID] 24h、models、errors 24h、replay 消息ID、replay latest 群ID、"
                    "persona show、memory me [关键词]、learning status、chats、health。追加 --page N 翻页。",
        )

    async def _whoami(self, request: AdminActionRequest) -> AdminActionResult:
        from src.config.config import global_config

        is_owner = OwnerIdentityResolver.is_owner(request.actor_identity, global_config.bot.owner_qq)
        owner_label = "主人" if is_owner else "普通用户"
        account_label = "平台账号已验证" if request.actor_identity.bot_account_verified else "平台账号范围不可用"
        return AdminActionResult(
            success=True,
            status="succeeded",
            message=f"身份识别：{owner_label}；平台：QQ；聊天：{request.actor_identity.chat_type}；{account_label}。",
            data={"is_owner": is_owner, "account_scope_available": request.actor_identity.bot_account_verified},
        )

    async def _legacy_personality(self, request: AdminActionRequest) -> AdminActionResult:
        from src.config.config import global_config

        text = global_config.personality.personality.split("## 行为风格")[0].strip()
        return AdminActionResult(True, "succeeded", f"{global_config.bot.nickname}的简化人格：{text}")

    async def _legacy_models(self, request: AdminActionRequest) -> AdminActionResult:
        from src.config.config import config_manager

        model = config_manager.get_model_config()
        names = [item.name for item in model.models]
        return AdminActionResult(
            True,
            "succeeded",
            "可选模型：" + "、".join(names) + "。切换：/主人 模型 planner或replyer 模型名称",
        )

    async def _legacy_model_select(self, request: AdminActionRequest) -> AdminActionResult:
        from src.config import config as current_config
        from src.webui.utils.toml_utils import save_toml_with_format

        task = str(request.parameters.get("task", ""))
        model_name = str(request.parameters.get("model", ""))
        if task not in {"planner", "replyer"}:
            return AdminActionResult(False, "rejected", "仅允许切换 planner 或 replyer。", error_code="invalid_task")
        if not OwnerIdentityResolver.is_owner(
            request.actor_identity,
            current_config.global_config.bot.owner_qq,
        ):
            return AdminActionResult(False, "denied", "主人身份已变更，请重新发起命令。", error_code="owner_binding_changed")
        if not current_config.global_config.bot.owner_group_commands_enabled:
            return AdminActionResult(False, "denied", "群聊管理命令已在后台关闭。", error_code="group_commands_disabled")
        model = current_config.config_manager.get_model_config()
        if model_name not in {item.name for item in model.models}:
            return AdminActionResult(False, "rejected", "该模型不在后台已配置的模型列表中。", error_code="model_not_configured")
        await asyncio.to_thread(
            save_toml_with_format,
            {"model_task_config": {task: {"model_list": [model_name]}}},
            str(current_config.config_manager.model_config_path),
        )
        if not await current_config.config_manager.reload_config(
            changed_scopes=("model",),
            skip_if_unchanged=False,
        ):
            raise RuntimeError("模型配置已保存，但重载失败")
        return AdminActionResult(True, "succeeded", f"已将 {task} 切换为 {model_name}。")

    async def _legacy_help(self, request: AdminActionRequest) -> AdminActionResult:
        return AdminActionResult(
            True,
            "succeeded",
            "主人命令：/主人 人格；/主人 模型；/主人 模型 planner或replyer 模型名称。",
        )

    @staticmethod
    def _target_for(action_id: str, identity: AdminIdentity) -> AdminTarget:
        resource_type = "system_config" if action_id == "owner.models.select" else "chat"
        resource_id = "model_task_config" if resource_type == "system_config" else identity.chat_id
        return AdminTarget(
            resource_type=resource_type,
            resource_id=resource_id,
            platform=identity.platform,
            bot_account_id=identity.bot_account_id,
            chat_type=identity.chat_type,
            session_id=identity.session_id,
            chat_id=identity.chat_id,
        )

    @staticmethod
    def _request_id(identity: AdminIdentity, action_id: str, message_id: str) -> str:
        parts = (
            identity.platform,
            identity.bot_account_id or "",
            identity.user_id,
            identity.session_id,
            str(message_id).strip(),
            action_id,
        )
        return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()

    @staticmethod
    def _parameter_summary(spec: Optional[AdminActionSpec], parameters: Mapping[str, object]) -> Dict[str, str]:
        if spec is None:
            return {}
        return {
            key: OwnerAdminCommandRouter._safe_parameter_value(key, parameters[key])
            for key in spec.audit_parameter_keys
            if key in parameters
        }

    @staticmethod
    def _safe_parameter_value(key: str, value: object) -> str:
        normalized = str(value)
        if _SENSITIVE_PARAMETER_KEY.search(key) or _SECRET_VALUE.search(normalized):
            return "[redacted]"
        if key == "model":
            return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        return normalized[:80]

    @staticmethod
    def _parse(body: str) -> "_ParsedCommand":
        if body.startswith("/dfy"):
            args = body.split()
            from .read_commands import parse_read_command

            read_command = parse_read_command(args)
            if read_command is not None:
                return _ParsedCommand(*read_command)
            subcommand = args[1].lower() if len(args) > 1 else "help"
            if subcommand == "help" and len(args) == 2:
                return _ParsedCommand("owner_admin.help", {})
            if subcommand == "whoami" and len(args) == 2:
                return _ParsedCommand("owner_admin.whoami", {})
            return _ParsedCommand("owner_admin.unregistered", {})

        if body in {"输出简化人格模板", "输出简化人格", "请输出简化人格模板"}:
            return _ParsedCommand("owner.personality.read", {})
        if body in {"列出可选模型", "查看可选模型", "/主人 模型"}:
            return _ParsedCommand("owner.models.list", {})
        if body.startswith("选择模型 "):
            parts = body.split(maxsplit=2)
            if len(parts) == 3:
                return OwnerAdminCommandRouter._parse(f"/主人 模型 {parts[1]} {parts[2]}")
            return _ParsedCommand("owner.legacy.help", {})

        args = body.split()
        if args == ["/主人", "人格"]:
            return _ParsedCommand("owner.personality.read", {})
        if args == ["/主人", "模型"]:
            return _ParsedCommand("owner.models.list", {})
        if len(args) == 4 and args[:2] == ["/主人", "模型"]:
            return _ParsedCommand("owner.models.select", {"task": args[2], "model": args[3]})
        return _ParsedCommand("owner.legacy.help", {})


@dataclass(frozen=True)
class _ParsedCommand:
    action_id: str
    parameters: Mapping[str, object]


owner_admin_command_router = OwnerAdminCommandRouter()
