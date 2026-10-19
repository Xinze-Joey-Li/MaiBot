"""可信主人管理命令的基础设施。"""

from .command_router import OwnerAdminCommandRouter, owner_admin_command_router
from .identity import OwnerIdentityResolver
from .models import AdminActionRequest, AdminActionResult, AdminIdentity, AdminTarget
from .permissions import OwnerPermissionGuard

__all__ = [
    "AdminActionRequest",
    "AdminActionResult",
    "AdminIdentity",
    "AdminTarget",
    "OwnerAdminCommandRouter",
    "OwnerIdentityResolver",
    "OwnerPermissionGuard",
    "owner_admin_command_router",
]
