"""可信平台消息身份解析；昵称和消息正文不参与授权。"""

from typing import TYPE_CHECKING

from src.platform_io.route_key_factory import RouteKeyFactory

from .models import AdminIdentity

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage


class OwnerIdentityResolver:
    """将入站消息中的平台身份字段收敛成统一身份对象。"""

    @staticmethod
    def from_message(message: "SessionMessage", *, bot_mentioned: bool = False) -> AdminIdentity:
        group_info = message.message_info.group_info
        route_key = RouteKeyFactory.from_session_message(message)
        return AdminIdentity(
            platform=str(message.platform).strip().lower(),
            bot_account_id=route_key.account_id,
            user_id=str(message.message_info.user_info.user_id).strip(),
            chat_type="group" if group_info is not None else "private",
            session_id=str(message.session_id).strip(),
            chat_id=str(group_info.group_id if group_info is not None else message.message_info.user_info.user_id).strip(),
            trusted_platform_metadata=True,
            bot_mentioned=bot_mentioned,
        )

    @staticmethod
    def is_owner(identity: AdminIdentity, owner_user_id: str) -> bool:
        """校验后台绑定的 QQ 用户 ID；拒绝昵称、正文或模型自述。"""

        normalized_owner_id = str(owner_user_id).strip()
        return (
            identity.trusted_platform_metadata
            and identity.platform == "qq"
            and bool(normalized_owner_id)
            and identity.user_id == normalized_owner_id
        )
