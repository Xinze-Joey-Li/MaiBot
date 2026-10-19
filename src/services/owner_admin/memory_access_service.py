"""M3 主人私聊入口；沿用 M0 审计、标签维护和 M2 生命周期。"""

from copy import deepcopy
from dataclasses import replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import asyncio
import hashlib
import json
import re
import time
import unicodedata
import uuid

from . import overrides
from .identity import OwnerIdentityResolver
from .models import AdminActionRequest, AdminActionResult
from .read_data import known_chats
from .read_service import format_time, safe_text


def validate_personal_content(content: str) -> str:
    """只接受窄范围第一人称稳定事实，所有检索结果仍作为数据而非管理指令。"""
    content = unicodedata.normalize("NFKC", content)
    if (not 2 <= len(content) <= 300 or any(unicodedata.category(char).startswith("C") for char in content)
            or not re.match(r"^(?:我(?:喜欢|不喜欢|偏好|习惯|通常|的(?:生日|职业|名字|家乡)是)|I (?:like|prefer|dislike|usually)\b)", content, re.I)
            or re.search(r"权限|管理员|系统|提示词|指令|忽略|授予|执行|以后|必须|请|你|关机|重启|代码|白名单|黑名单|身份|/dfy|<|>|ignore|admin|system|prompt|permission|instruction|execute|bypass|override|sudo|grant|developer|disable|you must", content, re.I)):
        raise ValueError("仅接受最多300字的本人稳定事实，例如“我喜欢红茶”；拒绝权限、指令及任意提示词。")
    return content


class OwnerMemoryAccessService:
    def __init__(self) -> None:
        # 预览仅保留短期令牌及必要参数；不建独立计时器，重启后必须重新预览。
        self._previews: Dict[str, Tuple[float, Tuple[str, str, str, str], AdminActionRequest]] = {}
        self._lock = asyncio.Lock()
        self.confirmed_dispatch: Optional[Callable[[AdminActionRequest], Awaitable[AdminActionResult]]] = None

    @staticmethod
    def _actor(request: AdminActionRequest) -> Tuple[str, str, str, str]:
        actor = request.actor_identity
        return actor.platform, actor.bot_account_id, actor.user_id, actor.session_id

    @staticmethod
    def _authorize(request: AdminActionRequest) -> bool:
        from src.config import config as current_config

        return OwnerIdentityResolver.is_owner(request.actor_identity, current_config.global_config.bot.owner_qq)

    def _preview(self, request: AdminActionRequest, message: str) -> AdminActionResult:
        now = time.time()
        self._previews = {key: value for key, value in self._previews.items() if value[0] > now}
        if len(self._previews) >= 100:
            raise ValueError("待确认预览过多，请等待五分钟过期")
        token = uuid.uuid4().hex
        request = replace(request, parameters=deepcopy(dict(request.parameters)))
        self._previews[token] = (now + 300, self._actor(request), request)
        return AdminActionResult(True, "preview", f"{message}\n五分钟内确认：/dfy confirm {token}", data={"token": token})

    def confirmation_audit(self, identity: Any, token: str) -> Dict[str, str]:
        preview = self._previews.get(token)
        actor = (identity.platform, identity.bot_account_id, identity.user_id, identity.session_id)
        if preview is None or preview[0] <= time.time() or preview[1] != actor:
            return {}
        allowed = {"memory_id", "group_id", "operation", "user_id", "message_id", "plugin_id", "duration", "platform_message_id", "target_session_id", "target_chat_id"}
        return {"confirmed_action": preview[2].action_id, "original_request_id": preview[2].request_id,
                "expires_at": str(preview[0]),
                "parameter_digest": hashlib.sha256(json.dumps(dict(preview[2].parameters), sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                **{key: value for key, value in preview[2].parameters.items() if key in allowed}}

    async def handle(self, request: AdminActionRequest) -> AdminActionResult:
        async with self._lock:
            if not self._authorize(request):
                return AdminActionResult(False, "denied", "主人授权已撤销，未执行。", error_code="owner_revoked")
            confirmed = False
            if request.parameters["command"] == "confirm":
                preview = self._previews.get(request.parameters["token"])
                if preview is None or preview[0] <= time.time() or preview[1] != self._actor(request):
                    return AdminActionResult(False, "rejected", "预览已失效或不属于当前身份与通道，请重新预览。", error_code="preview_invalid")
                del self._previews[request.parameters["token"]]
                # 执行使用确认消息的审计关联，权限仍由本次 M0 Guard 及当前主人配置检查。
                request = replace(preview[2], request_id=request.request_id, actor_identity=request.actor_identity)
                if not request.action_id.startswith("owner_admin.m3."):
                    if self.confirmed_dispatch is None:
                        raise RuntimeError("确认动作未连接到统一注册表")
                    return await self.confirmed_dispatch(replace(request, parameters={**request.parameters, "_confirmed": True}))
                confirmed = True
            try:
                command = request.parameters["command"]
                if command.startswith("memory_"):
                    return await self._memory(request, confirmed)
                if command == "access":
                    return await self._access(request, confirmed)
                return await self._ignore(request)
            except (ValueError, PermissionError) as error:
                return AdminActionResult(False, "rejected", safe_text(str(error)), error_code="invalid_scope_or_data")

    async def _write(self, request: AdminActionRequest, function: Callable[..., Any], *args, **kwargs) -> Any:
        def write():
            if not self._authorize(request):
                raise PermissionError("排队期间主人授权已撤销")
            return function(*args, **kwargs)
        task = asyncio.create_task(asyncio.to_thread(write))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 线程里的事务不可中途取消；等其提交/回滚再退出，M0 pending 防止盲目重放。
            await task
            raise

    async def _memory(self, request: AdminActionRequest, confirmed: bool) -> AdminActionResult:
        from src.services.tag_memory_service import tag_memory_service
        from src.services.tag_memory_store import TagCandidate

        actor, parameters = request.actor_identity, dict(request.parameters)
        store = await tag_memory_service._get_store()
        chats = await asyncio.to_thread(known_chats, actor.bot_account_id)
        command = parameters["command"]
        if command == "memory_add":
            content = validate_personal_content(parameters["content"])
            matches = [chat for chat in chats if chat.session_id == actor.session_id and not chat.group_id and chat.user_id == actor.user_id]
            if len(matches) != 1 or not request.source_message_id or not request.request_id:
                raise ValueError("需要本账号下已存在且归属明确的主人私聊与可信来源消息")
            chat = matches[0]
            external_id = f"owner_explicit:{actor.bot_account_id}:{actor.user_id}:{request.request_id}"
            result = await self._write(request,
                store.record_batch, session_id=chat.session_id, session_name=chat.user_nickname or "主人私聊",
                external_id=external_id, summary="主人显式声明的本人稳定事实；内容以当前有效标签为准",
                message_ids=[request.source_message_id], owner_explicit_by=(actor.bot_account_id, actor.user_id),
                tags=[TagCandidate("本人稳定事实", content, ("人物", actor.user_id, "owner_explicit"),
                                   source_message_ids=[request.source_message_id], requires_long_term=True)],
            )
            row = result["owner_tags"][0]
            if row["status"] != "valid":
                return AdminActionResult(False, "rejected", "同一来源记忆已被撤回或替代，未重新激活。", error_code="source_retired")
            return AdminActionResult(True, "succeeded", f"已保存本人记忆 ID={row['id']}；source=owner_explicit；"
                                     f"创建={format_time(row['created_at'])}；范围=当前主人私聊。", data={"memory_id": row["id"]})
        detail = await asyncio.to_thread(store.owner_declaration, [chat.session_id for chat in chats],
                                        actor.bot_account_id, actor.user_id, parameters["memory_id"])
        if not confirmed:
            if command == "memory_edit" and "content" not in parameters:
                return AdminActionResult(True, "preview", f"{safe_text(detail['summary'])}\n"
                                         f"更正：/dfy memory edit {detail['id']} <新的本人稳定事实>；再次预览后确认。")
            if command == "memory_edit":
                validate_personal_content(parameters["content"])
            parameters["revision"] = detail["revision"]
            return self._preview(replace(request, parameters=parameters),
                                 f"目标={detail['id']}；原文={safe_text(detail['summary'])}；"
                                 + (f"更正为={safe_text(parameters['content'])}" if command == "memory_edit" else
                                    "执行逻辑撤回，停止有效标签、关键词及会话画像检索；证据和审计仍保留，不是永久删除。"))
        if detail["revision"] != parameters["revision"]:
            raise ValueError("记忆已变化，必须重新预览")
        if command == "memory_edit":
            await self._write(request, store.edit_tag, detail["session_id"], detail["id"], revision=parameters["revision"],
                                    reason=f"owner_explicit correction:{request.request_id}", name=detail["name"],
                                    summary=validate_personal_content(parameters["content"]), is_active=False, requires_long_term=True)
        else:
            await self._write(request, store.delete_tag, detail["session_id"], detail["id"], parameters["revision"],
                                    f"owner_explicit withdrawal:{request.request_id}")
        return AdminActionResult(True, "succeeded", "本人记忆已更正。" if command == "memory_edit" else
                                 "本人记忆已逻辑撤回；原始消息、证据和审计未永久删除。")

    async def _ignore(self, request: AdminActionRequest) -> AdminActionResult:
        actor, parameters = request.actor_identity, request.parameters
        manager = overrides.override_manager
        await manager.initialize(run_timer=False)
        chats = await asyncio.to_thread(known_chats, actor.bot_account_id,
                                       **({"group_id": parameters["group_id"]} if parameters.get("group_id") else {}))
        if parameters["operation"] == "list":
            sessions = {chat.session_id for chat in chats}
            entries = [entry for entry in manager.active() if entry.property.startswith("ignore:") and
                       ((entry.target_scope == "chat" and entry.target_id in sessions) or
                        (entry.target_scope == "bot" and entry.target_id == actor.bot_account_id))]
            page = int(parameters.get("page", 1))
            lines = [f"{entry.property[7:]} scope={entry.target_scope}:{entry.target_id} expires={format_time(entry.expires_at)}"
                     for entry in entries[(page-1)*4:page*4]]
            return AdminActionResult(True, "succeeded", "\n".join(["有效用户屏蔽", *lines, f"共{len(entries)}项，第{page}/{max(1,(len(entries)+3)//4)}页；ignore list --page N。群范围使用 --group 群ID；全局需 --global。"]))
        if parameters["user_id"] == actor.user_id:
            raise ValueError("不能屏蔽主人控制身份")
        scope, target = "bot", actor.bot_account_id
        if not parameters.get("global"):
            if not parameters.get("group_id") or len(chats) != 1:
                raise ValueError("私聊不能推断目标群：请追加 --group 群ID；显式全局范围使用 --global")
            scope, target = "chat", chats[0].session_id
        entry = await manager.change(property="ignore:" + parameters["user_id"], target_scope=scope, target_id=target,
                                     value=True, duration=parameters.get("duration"), cancel=parameters["cancel"],
                                     created_by=actor.user_id, request_id=request.request_id,
                                     authorize=lambda: self._authorize(request))
        return AdminActionResult(True, "succeeded", "已取消该范围的屏蔽。" if entry is None else
                                 f"已屏蔽用户 {parameters['user_id']} 的回复触发；范围={scope}:{target}；到期={format_time(entry.expires_at)}。")

    async def _access(self, request: AdminActionRequest, confirmed: bool) -> AdminActionResult:
        from .access_policy_service import access_targets, apply_access, policy_digest

        actor, parameters = request.actor_identity, dict(request.parameters)
        chats = await asyncio.to_thread(known_chats, actor.bot_account_id,
                                       **({"group_id": parameters["group_id"]} if parameters.get("group_id") else {}))
        if parameters["operation"] == "list":
            lines: List[str] = []
            page = int(parameters.get("page", 1))
            for chat in chats[(page-1)*4:page*4]:
                targets = await asyncio.to_thread(access_targets, chat)
                lines.extend(f"{chat.group_name or chat.user_nickname} {chat.group_id or '私聊'} "
                             f"adapter={identity.adapter_id} account={identity.account_id} allowed={result.allowed} source={result.source}"
                             for identity, result in targets)
            return AdminActionResult(True, "succeeded", "\n".join(["当前账号接入策略（历史流不代表在线；最多最近501流）", *[safe_text(line) for line in lines[:4]], f"第{page}/{max(1,(len(chats)+3)//4)}页；access list --page N。多路由最多显示4行。 "]))
        if len(chats) != 1 or not chats[0].group_id:
            raise ValueError("目标必须是当前Bot账号下唯一已知群聊")
        targets = await asyncio.to_thread(access_targets, chats[0])
        if len(targets) != 1:
            raise ValueError("目标群必须唯一匹配当前账号的适配器接收路由；多适配器请从本地管理")
        identity, current = targets[0]
        if not confirmed:
            parameters["digest"] = await asyncio.to_thread(policy_digest)
            parameters["adapter_id"] = identity.adapter_id
            return self._preview(replace(request, parameters=parameters),
                                 f"目标群={safe_text(chats[0].group_name)}({chats[0].group_id})；适配器={identity.adapter_id}；"
                                 f"Bot={actor.bot_account_id}；当前允许={current.allowed}；拟执行={parameters['operation']}。"
                                 "仅更改该群；主人私聊管理接入策略保持当前值。")
        if parameters["adapter_id"] != identity.adapter_id:
            raise ValueError("适配器路由已变化，请重新预览")
        result = await self._write(request, apply_access, identity, chats[0].group_id, parameters["operation"],
                                        parameters["digest"], lambda: self._authorize(request))
        return AdminActionResult(True, "succeeded", f"群 {chats[0].group_id} 实际接入允许={result.allowed}；"
                                 f"source={result.source}；reason={result.reason}。已运行回合不回收。")
