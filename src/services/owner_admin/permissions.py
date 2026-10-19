"""主人管理动作唯一的本地授权入口。"""

from .models import AdminActionRequest, AdminActionSpec, PermissionDecision


class OwnerPermissionGuard:
    """基于可信操作者、注册动作、资源目标及聊天范围执行默认拒绝授权。"""

    def authorize(
        self,
        request: AdminActionRequest,
        spec: AdminActionSpec | None,
        *,
        owner_user_id: str,
        group_commands_enabled: bool,
    ) -> PermissionDecision:
        actor = request.actor_identity
        target = request.target
        if spec is None or not spec.permission_id or spec.action_id != request.action_id:
            return PermissionDecision(False, "unregistered_action")
        if not actor.trusted_platform_metadata:
            return PermissionDecision(False, "untrusted_identity")
        if actor.platform != "qq" or not actor.user_id or not actor.session_id:
            return PermissionDecision(False, "invalid_actor_scope")
        if actor.chat_type not in spec.allowed_chat_types:
            return PermissionDecision(False, "chat_type_denied")
        if spec.require_bot_account and (not actor.bot_account_id or not actor.bot_account_verified):
            return PermissionDecision(False, "bot_account_unavailable")
        if (
            target.platform != actor.platform
            or target.bot_account_id != actor.bot_account_id
            or target.chat_type != actor.chat_type
            or target.session_id != actor.session_id
            or target.chat_id != actor.chat_id
        ):
            return PermissionDecision(False, "target_scope_mismatch")
        if target.resource_type not in spec.allowed_target_types or not target.resource_id:
            return PermissionDecision(False, "target_not_allowed")
        if spec.require_bot_mention and not actor.bot_mentioned:
            return PermissionDecision(False, "bot_mention_required")
        if spec.require_group_commands_enabled and (actor.chat_type != "group" or not group_commands_enabled):
            return PermissionDecision(False, "group_commands_disabled")
        if spec.owner_required and actor.user_id != str(owner_user_id).strip():
            return PermissionDecision(False, "not_owner")
        return PermissionDecision(True)
