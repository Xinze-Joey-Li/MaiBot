"""只允许执行显式注册的主人管理动作。"""

from typing import Dict, Optional

from .models import AdminActionSpec


class ActionRegistry:
    """集中维护稳定 action_id 与 permission_id 的映射。"""

    def __init__(self) -> None:
        self._actions: Dict[str, AdminActionSpec] = {}

    def register(self, spec: AdminActionSpec) -> None:
        action_id = spec.action_id.strip()
        permission_id = spec.permission_id.strip()
        if not action_id or not permission_id:
            raise ValueError("管理动作必须声明稳定的 action_id 和 permission_id")
        if action_id in self._actions:
            raise ValueError(f"管理动作重复注册: {action_id}")
        if any(existing.permission_id == permission_id for existing in self._actions.values()):
            raise ValueError(f"管理权限重复注册: {permission_id}")
        self._actions[action_id] = spec

    def get(self, action_id: str) -> Optional[AdminActionSpec]:
        return self._actions.get(action_id)

    def list_actions(self) -> tuple[AdminActionSpec, ...]:
        return tuple(self._actions.values())
