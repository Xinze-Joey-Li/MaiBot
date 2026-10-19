"""对现有适配器策略的账号限定接入；查询和写入均在线程中执行。"""

from typing import Any, Callable, List, Tuple
import hashlib

from src.platform_io.adapter_policy import (
    AdapterIdentity, AdapterPolicyResult, get_adapter_policy_manager, serialized_policy_write,
)


def access_targets(chat: Any) -> List[Tuple[AdapterIdentity, AdapterPolicyResult]]:
    from src.platform_io import DriverKind, RouteKey, get_platform_io_manager

    manager = get_platform_io_manager()
    route = RouteKey(platform=chat.platform, account_id=chat.account_id, scope=chat.scope or None)
    results = []
    for driver in manager.driver_registry.list(kind=DriverKind.PLUGIN):
        descriptor = driver.descriptor
        metadata = descriptor.metadata
        if (not isinstance(metadata, dict) or metadata.get("plugin_type") != "adapter"
                or descriptor.platform != chat.platform or descriptor.account_id != chat.account_id
                or not manager.receive_route_table.has_binding_for_driver(route, descriptor.driver_id)):
            continue
        identity = AdapterIdentity(descriptor.driver_id, descriptor.plugin_id or "",
                                   str(metadata.get("gateway_name") or "").strip(),
                                   descriptor.platform, descriptor.account_id, descriptor.scope)
        results.append((identity, get_adapter_policy_manager().evaluate(
            identity, chat_type="group" if chat.group_id else "private", target_id=chat.group_id or chat.user_id)))
    return results


def policy_digest() -> str:
    path = get_adapter_policy_manager().policy_path
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


@serialized_policy_write
def apply_access(identity: AdapterIdentity, group_id: str, operation: str, expected_digest: str,
                 authorize: Callable[[], bool], *, chat_type: str = "group") -> AdapterPolicyResult:
    if not authorize():
        raise PermissionError("主人授权已撤销")
    if operation not in {"deny", "allow", "reset"} or not identity.account_id or identity.platform != "qq":
        raise ValueError("账号或操作无效")
    if chat_type not in {"group", "private"} or (chat_type == "private" and operation != "allow"):
        raise ValueError("私聊仅支持添加放行，不允许阻断主人控制通道")
    if policy_digest() != expected_digest:
        raise ValueError("基础策略已经变化，必须重新预览")
    manager = get_adapter_policy_manager()
    manager.set_chat_override(identity, chat_type=chat_type, target_id=group_id,
                              action={"deny": "block", "allow": "allow", "reset": "inherit"}[operation],
                              exact_identity=True, verify=True)
    return manager.evaluate(identity, chat_type=chat_type, target_id=group_id)
