"""真实平台身份授权的有限管理命令；LLM 输出不能变成管理调用。"""

from typing import Optional, TYPE_CHECKING

import asyncio
import json

from src.common.data_models.message_component_data_model import AtComponent, TextComponent
from src.common.logger import get_logger

logger = get_logger("owner_control")
_command_lock = asyncio.Lock()

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage


def owner_security_prompt() -> str:
    from src.common.prompt_i18n import load_prompt
    from src.config.config import global_config

    return load_prompt("owner_security", owner_qq=global_config.bot.owner_qq)


def is_owner(message: "SessionMessage", owner_qq: str) -> bool:
    """不信任昵称、正文、引用、转发或模型声明的身份。"""
    return message.platform == "qq" and str(message.message_info.user_info.user_id) == owner_qq


def command_body(message: "SessionMessage") -> Optional[str]:
    parts = []
    for component in message.raw_message.components:
        if isinstance(component, TextComponent):
            parts.append(component.text)
        elif not isinstance(component, AtComponent):
            return None
    body = "".join(parts).strip()
    if body in {"输出简化人格模板", "输出简化人格", "请输出简化人格模板"}:
        return "/主人 人格"
    if body in {"列出可选模型", "查看可选模型"}:
        return "/主人 模型"
    if body.startswith("选择模型 "):
        return "/主人 模型 " + body.removeprefix("选择模型 ")
    return body if body.startswith("/主人") else None


async def handle_owner_command(message: "SessionMessage") -> Optional[str]:
    """返回 None 表示普通聊天；管理文本始终由本地生成，不经过 LLM。"""
    from src.chat.utils.utils import is_mentioned_bot_in_message
    from src.config.config import config_manager, global_config

    body = command_body(message)
    if body is None:
        return None
    if not is_owner(message, global_config.bot.owner_qq):
        logger.warning(f"拒绝管理命令: session={message.session_id} reason=not_owner")
        return "这条管理命令只接受后台绑定的真实主人账号。"
    if not global_config.bot.owner_group_commands_enabled:
        return "群聊管理命令已在后台关闭。"
    if message.message_info.group_info is None or not is_mentioned_bot_in_message(message):
        return "请在群聊中 @ 我后使用 /主人 命令。"
    args = body.split()
    async with _command_lock:
        # 排队后再校验最新身份；撤销主人不能因已进入队列而继续生效。
        from src.config import config as current_config

        if not is_owner(message, current_config.global_config.bot.owner_qq):
            return "主人身份已变更，请重新发起命令。"
        if not current_config.global_config.bot.owner_group_commands_enabled:
            return "群聊管理命令已在后台关闭。"
        if args == ["/主人", "人格"]:
            return f"{global_config.bot.nickname}的简化人格：" + global_config.personality.personality.split("## 行为风格")[0].strip()
        if args == ["/主人", "模型"]:
            model = config_manager.get_model_config()
            names = [m.name for m in model.models]
            return "可选模型：" + "、".join(names) + "。切换：/主人 模型 planner或replyer 模型名称"
        if len(args) == 4 and args[:2] == ["/主人", "模型"]:
            task, name = args[2:]
            if task not in {"planner", "replyer"}:
                return "仅允许切换 planner 或 replyer。"
            model = config_manager.get_model_config()
            if name not in {m.name for m in model.models}:
                return "该模型不在后台已配置的模型列表中。"
            from src.webui.utils.toml_utils import save_toml_with_format

            await asyncio.to_thread(save_toml_with_format,
                {"model_task_config": {task: {"model_list": [name]}}}, str(config_manager.model_config_path))
            if not await config_manager.reload_config(changed_scopes=("model",), skip_if_unchanged=False):
                raise RuntimeError("模型配置已保存，但重载失败；请在后台检查")
            logger.info(f"主人模型切换: session={message.session_id} task={task} model={json.dumps(name)}")
            return f"已将 {task} 切换为 {name}。"
    return "主人命令：/主人 人格；/主人 模型；/主人 模型 planner或replyer 模型名称。"
