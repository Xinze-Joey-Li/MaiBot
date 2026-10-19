"""现有主人社交身份与旧命令兼容入口。"""

from typing import Optional, TYPE_CHECKING

from src.services.owner_admin.command_router import OwnerAdminCommandRouter, owner_admin_command_router
from src.services.owner_admin.identity import OwnerIdentityResolver

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage


def owner_security_prompt() -> str:
    # 兼容旧自定义模板中的占位符，但绝不将真实账号发送给模型或 Prompt 日志。
    from src.common.prompt_i18n import load_prompt

    return load_prompt("owner_security", owner_qq="[private host-verified identity]")


def is_owner(message: "SessionMessage", owner_qq: str) -> bool:
    """通过共享解析器检查可信 QQ 用户 ID，不信任昵称、正文或模型声明。"""

    if message.platform.strip().lower() != "qq" or not str(owner_qq).strip():
        return False
    if str(message.message_info.user_info.user_id).strip() != str(owner_qq).strip():
        return False
    identity = OwnerIdentityResolver.from_message(message)
    return OwnerIdentityResolver.is_owner(identity, owner_qq)


def command_body(message: "SessionMessage") -> Optional[str]:
    """兼容旧主人命令解析接口；仅识别消息顶层的纯文本与 @ 组件。"""

    body = OwnerAdminCommandRouter.command_body(message)
    if body in {"输出简化人格模板", "输出简化人格", "请输出简化人格模板"}:
        return "/主人 人格"
    if body in {"列出可选模型", "查看可选模型"}:
        return "/主人 模型"
    if body and body.startswith("选择模型 "):
        return "/主人 模型 " + body.removeprefix("选择模型 ")
    return body if body and body.startswith("/主人") else None


async def handle_owner_command(message: "SessionMessage") -> Optional[str]:
    """旧调用方委托统一动作路由、授权和审计；普通消息返回 None。"""

    if not OwnerAdminCommandRouter.is_legacy_command(message):
        return None
    result = await owner_admin_command_router.handle_message(message)
    return result.message if result is not None else None
