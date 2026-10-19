"""M2 已授权动作处理；目标必须是当前 Bot 账号下真实存在的 QQ 群聊天流。"""

from dataclasses import asdict
from typing import List
import asyncio

from . import overrides
from .models import AdminActionRequest, AdminActionResult
from .read_data import known_chats
from .read_service import format_time, safe_text


class OwnerControlService:
    async def handle(self, request: AdminActionRequest) -> AdminActionResult:
        parameters = request.parameters
        command = parameters["command"]
        manager = overrides.override_manager
        await manager.initialize(run_timer=False)
        group = parameters.get("group_id")
        chats = await asyncio.to_thread(known_chats, request.actor_identity.bot_account_id,
                                       **({"group_id": group} if group else {}))
        own_chats = {chat.session_id: chat for chat in chats}
        matches = [chat for chat in chats if chat.group_id == group] if group else []
        if group and len(matches) != 1:
            return AdminActionResult(False, "rejected", "群不存在、Bot账号归属不明或匹配不唯一，未执行。",
                                     error_code="invalid_group_scope")
        target_id = matches[0].session_id if matches else request.actor_identity.bot_account_id
        if command == "overrides" or (command == "chat" and parameters["operation"] == "config"):
            snapshot = manager.snapshot(target_id, request.actor_identity.bot_account_id)
            entries = [entry for entry in manager.active() if
                       (entry.target_scope == "bot" and entry.target_id == request.actor_identity.bot_account_id)
                       or (entry.target_scope == "chat" and entry.target_id in own_chats
                           and (not group or entry.target_id == target_id))]
            lines: List[str] = []
            if command == "chat":
                from src.common.utils.utils_config import ChatConfigUtils
                from src.maisaka.mode_policy import get_reply_trigger_mode

                base = await asyncio.to_thread(ChatConfigUtils.get_talk_value, target_id, is_group_chat=True)
                lines.append(f"当前基础频率={base:.4f}；覆盖后频率={snapshot.frequency(base):.4f}；"
                             f"阈值偏移={snapshot.threshold_delta:+.3f}；模式={snapshot.mode}；"
                             f"静音={snapshot.muted}；判定系统={get_reply_trigger_mode()}；概率阈值偏移仅dynamic生效")
            for entry in entries[:400]:
                label = own_chats[entry.target_id].group_name if entry.target_scope == "chat" else "当前Bot"
                lines.append(f"{label} {entry.property}={entry.value}；priority={entry.priority}；"
                             f"expires_at={format_time(entry.expires_at) if entry.expires_at is not None else '持久'}；"
                             f"status=active；override_id={entry.override_id}")
            page = int(parameters.get("page", 1))
            lines = lines[:400]
            message = "\n".join(["当前有效覆盖", *[safe_text(line) for line in lines[(page-1)*4:page*4]],
                                 f"第{page}/{max(1,(len(lines)+3)//4)}页；最多400行，--page N翻页。空列表表示继承当前基础配置。"])
            return AdminActionResult(True, "succeeded", message, data={"effective": asdict(snapshot)})
        property = parameters["property"]
        cancel = bool(parameters.get("cancel"))
        if property == "threshold" and not cancel:
            from src.maisaka.mode_policy import is_dynamic_reply_trigger_enabled

            if not is_dynamic_reply_trigger_enabled():
                return AdminActionResult(False, "unsupported", "当前不是dynamic权重门控；独立概率阈值不适用，未写入。",
                                         error_code="threshold_mode_unsupported")
        from src.config import config as current_config
        from .identity import OwnerIdentityResolver

        try:
            entry = await manager.change(
                property=property, target_scope="bot" if property in {"style", "persona"} else "chat",
                target_id=target_id, value=parameters.get("value"), created_by=request.actor_identity.user_id,
                request_id=request.request_id, duration=parameters.get("duration"),
                priority=int(parameters.get("priority", 100)), cancel=cancel,
                authorize=lambda: OwnerIdentityResolver.is_owner(request.actor_identity, current_config.global_config.bot.owner_qq),
            )
        except PermissionError:
            return AdminActionResult(False, "denied", "排队期间主人授权已撤销，未写入覆盖。", error_code="owner_revoked")
        message = ("已取消该层覆盖，继承当前有效配置。" if cancel else
                   f"已应用 {property}={parameters.get('value')}；override_id={entry.override_id}；"
                   f"expires_at={format_time(entry.expires_at) if entry.expires_at is not None else '持久'}。")
        message = (f"群 {group}：" if group else "当前Bot：") + message
        if property in {"style", "persona"}:
            message += "Planner与Replyer共用同轮快照；已开始的模型请求保留原快照，新逻辑轮使用新预设。"
        if property == "mode":
            message += "quiet=继承频率×0.5/阈值+0.05；active=×1.25/阈值-0.05；mention_only=仅真实@；阈值分量仅dynamic生效。"
        return AdminActionResult(True, "succeeded", message, data={"override": asdict(entry) if entry else None})
