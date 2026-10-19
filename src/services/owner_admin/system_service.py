"""M4 三项受限系统能力，统一复用 M0 授权审计和既有确认/生命周期。"""

from dataclasses import asdict, replace
from typing import Any
import asyncio
import logging

from . import overrides, plugin_control, recall_service
from .memory_access_service import OwnerMemoryAccessService
from .models import AdminActionRequest, AdminActionResult
from .read_service import format_time, safe_text

logger = logging.getLogger("owner_admin.system")


class OwnerSystemService:
    def __init__(self, confirmations: OwnerMemoryAccessService) -> None:
        self.confirmations = confirmations

    async def handle(self, request: AdminActionRequest) -> AdminActionResult:
        if not self.confirmations._authorize(request):
            return AdminActionResult(False, "denied", "主人授权已撤销。", error_code="owner_revoked")
        try:
            if request.parameters["command"] == "pause":
                return await self._pause(request)
            from src.plugin_runtime.integration import get_plugin_runtime_manager

            runtime = get_plugin_runtime_manager()
            if request.parameters["command"] == "plugin":
                return await self._plugin(request, runtime)
            return await self._recall(request, runtime)
        except (ValueError, PermissionError) as error:
            return AdminActionResult(False, "rejected", safe_text(str(error)), error_code="system_precondition_failed")
        except plugin_control.PluginChangeError as error:
            return AdminActionResult(False, "failed", safe_text(str(error)), error_code="plugin_transition_failed")

    async def _pause(self, request: AdminActionRequest) -> AdminActionResult:
        parameters = request.parameters
        manager = overrides.override_manager
        await manager.initialize(run_timer=False)
        account = request.actor_identity.bot_account_id
        if parameters["operation"] == "status":
            entries = [entry for entry in manager.active("bot", account) if entry.property == "soft_paused"]
            return AdminActionResult(True, "succeeded", "SOFT_PAUSED；到期=" + format_time(entries[0].expires_at) if entries else "RUNNING")
        if parameters["operation"] == "pause" and not parameters.get("_confirmed"):
            return self.confirmations._preview(request, f"当前Bot={account}；软暂停{parameters['duration']}秒；普通Planner/@/定时聊天发送停止，管理和恢复通道保留。")
        entry = await manager.change(property="soft_paused", target_scope="bot", target_id=account, value=True,
                                     created_by=request.actor_identity.user_id, request_id=request.request_id,
                                     duration=parameters.get("duration"), cancel=parameters["operation"] == "resume",
                                     authorize=lambda: self.confirmations._authorize(request))
        return AdminActionResult(True, "succeeded", "RUNNING；已取消软暂停，新消息按既有门控处理，旧轮不能补发。" if entry is None else
                                 f"SOFT_PAUSED；到期={format_time(entry.expires_at)}；管理查询和resume仍可用。")

    async def _plugin(self, request: AdminActionRequest, runtime: Any) -> AdminActionResult:
        parameters = request.parameters
        entries = await asyncio.to_thread(plugin_control.catalog, runtime)
        snapshot = await runtime.get_plugin_state_snapshot()
        if parameters["operation"] == "list":
            page = int(parameters.get("page", 1))
            rows = sorted(entries.items())
            lines = [f"{key} configured_enabled={row['enabled']} actual={snapshot['statuses'].get(key,'unknown')} type_protected={row['protected']}"
                     for key, row in rows[(page-1)*4:page*4]]
            return AdminActionResult(True, "succeeded", "\n".join(["插件状态（配置和实际加载分别展示）", *[safe_text(line) for line in lines],
                                     f"第{page}/{max(1,(len(rows)+3)//4)}页；plugin list --page N"]))
        plugin_id = parameters["plugin_id"]
        plugin_control.protected_target(runtime, entries, plugin_id, enabling=parameters["operation"] == "enable")
        if not parameters.get("_confirmed"):
            parameters = {**parameters, "digest": entries[plugin_id]["digest"]}
            return self.confirmations._preview(replace(request, parameters=parameters),
                                             f"目标插件={plugin_id}；拟{parameters['operation']}；持久修改enabled并同步实际生命周期，失败尝试回滚。")
        # 生命周期运行在主循环，文件扫描/持久化由其线程回调处理；取消时等待最终状态。
        task = asyncio.create_task(plugin_control.change_enabled(runtime, plugin_id, parameters["operation"] == "enable",
                                                               parameters["digest"], lambda: self.confirmations._authorize(request)))
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        return AdminActionResult(True, "succeeded", f"插件 {plugin_id}：持久enabled={parameters['operation']=='enable'}；实际状态={result}。")

    async def _recall(self, request: AdminActionRequest, runtime: Any) -> AdminActionResult:
        from src.platform_io import get_platform_io_manager

        parameters = request.parameters
        target = await recall_service.resolve_target(get_platform_io_manager(), parameters["message_id"], request.actor_identity.bot_account_id)
        recall_service.api_supervisor(runtime, target)
        if not parameters.get("_confirmed"):
            return self.confirmations._preview(replace(request, parameters={**parameters, "recall_target": asdict(target), "platform_message_id": target.platform_id,
                                                                          "target_session_id": target.session_id, "target_chat_id": target.chat_id,
                                                                          "plugin_id": target.plugin_id}),
                                             f"撤回自身消息：Bot={target.account_id}；聊天={target.chat_id}；平台ID={target.platform_id}；适配器={target.plugin_id}。确认时再次核验两分钟时限。")
        if asdict(target) != parameters["recall_target"]:
            raise ValueError("消息映射或归属已变化，必须重新预览")
        if not self.confirmations._authorize(request):
            raise PermissionError("主人授权已撤销")
        try:
            result = await recall_service.execute(runtime, target)
        except (TimeoutError, RuntimeError):
            logger.exception("撤回结果不确定，不自动重试")
            return AdminActionResult(False, "unknown", "撤回调用未得到明确结果，不自动重试；请检查平台。", error_code="recall_unknown")
        return AdminActionResult(result["success"], result["status"], result["message"],
                                 error_code=None if result["success"] else "platform_recall_unconfirmed")
