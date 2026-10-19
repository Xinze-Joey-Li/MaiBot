"""用户级触发过滤：保留消息解析、持久历史与管理认证，不赋予模型权限。"""

from typing import Any, FrozenSet, List, Optional, Tuple

from . import overrides


def ignore_rules() -> FrozenSet[Tuple[str, str, str]]:
    return frozenset((entry.target_scope, entry.target_id, entry.property[7:])
                     for entry in overrides.override_manager.active() if entry.property.startswith("ignore:"))


def is_ignored(message: Any, *, rules: Optional[FrozenSet[Tuple[str, str, str]]] = None) -> bool:
    if rules is None:
        rules = ignore_rules()
    if not rules:
        return False
    from src.config import config as current_config
    from .identity import OwnerIdentityResolver

    identity = OwnerIdentityResolver.from_message(message)
    if identity.platform != "qq" or OwnerIdentityResolver.is_owner(identity, current_config.global_config.bot.owner_qq):
        return False
    return (("chat", identity.session_id, identity.user_id) in rules
            or ("bot", identity.bot_account_id, identity.user_id) in rules)


def pending_trigger_messages(runtime: Any) -> List[Any]:
    rules = ignore_rules()
    messages = runtime.message_cache[runtime._last_processed_index:]
    return [message for message in messages if not is_ignored(message, rules=rules)] if rules else messages
