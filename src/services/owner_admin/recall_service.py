"""只撤回近期真实成功发送且归属可核验的自身 QQ 消息，不修改聊天数据库。"""

from dataclasses import asdict, dataclass
from typing import Any, Dict
import asyncio
import time

from .read_data import known_chats

API_NAME = "adapter.napcat.message.delete_msg"


@dataclass(frozen=True)
class RecallTarget:
    platform_id: str
    internal_id: str
    session_id: str
    chat_id: str
    account_id: str
    driver_id: str
    plugin_id: str
    scope: str
    sent_at: float


async def resolve_target(manager: Any, message_id: str, account_id: str) -> RecallTarget:
    from src.common.message_repository import find_messages
    from src.platform_io import DeliveryStatus, DriverKind

    receipts = [receipt for receipt in manager.recent_confirmed_receipts(message_id)
                if receipt.route_key.platform == "qq" and receipt.route_key.account_id == account_id
                and receipt.status == DeliveryStatus.SENT and receipt.driver_kind == DriverKind.PLUGIN
                and receipt.external_message_id and receipt.external_message_id.lstrip("-").isdigit()]
    if len(receipts) != 1:
        raise ValueError("缺少本进程两分钟内唯一真实成功回执；不能可靠映射内部/平台ID，未撤回")
    receipt = receipts[0]
    driver = manager.driver_registry.get(receipt.driver_id)
    if (driver is None or not driver.descriptor.plugin_id or driver.descriptor.account_id != account_id
            or driver.descriptor.platform != "qq" or driver.descriptor.scope != receipt.route_key.scope
            or driver.descriptor.metadata.get("plugin_type") != "adapter"):
        raise ValueError("发送适配器或账号已变化，未撤回")

    def query():
        messages: Dict[str, Any] = {}
        for mid in {receipt.internal_message_id, receipt.external_message_id}:
            for message in find_messages(platform="qq", user_id=account_id, message_id=mid, limit=2):
                messages[message.session_id + ":" + message.message_id] = message
        matches = []
        for message in messages.values():
            group = message.message_info.group_info
            chats = known_chats(account_id, **({"group_id": group.group_id} if group else {}))
            if any(chat.session_id == message.session_id and (chat.scope or "") == (receipt.route_key.scope or "") for chat in chats):
                matches.append(message)
        if len(matches) != 1:
            raise ValueError("找不到唯一且属于本账号真实聊天目标的自身发送记录；不接受其他用户消息")
        return matches[0]

    message = await asyncio.to_thread(query)
    timestamp = message.timestamp.timestamp()
    if not 0 <= time.time() - timestamp < 120:
        raise ValueError("消息超过保守的两分钟撤回窗口或时间不可核验，未撤回")
    group = message.message_info.group_info
    return RecallTarget(receipt.external_message_id, receipt.internal_message_id, message.session_id,
                        group.group_id if group else message.message_info.user_info.user_id, account_id,
                        receipt.driver_id, driver.descriptor.plugin_id, receipt.route_key.scope or "", timestamp)


def api_supervisor(runtime: Any, target: RecallTarget) -> Any:
    matches = []
    for supervisor in runtime.supervisors:
        entry = supervisor.api_registry.get_api(target.plugin_id, API_NAME, enabled_only=True, session_id=target.session_id)
        if entry is not None and entry.public:
            matches.append(supervisor)
    if len(matches) != 1:
        raise ValueError("该发送适配器未提供唯一可用的 NapCat 撤回API，平台能力不支持或当前不可用")
    return matches[0]


async def execute(runtime: Any, target: RecallTarget) -> Dict[str, Any]:
    supervisor = api_supervisor(runtime, target)
    response = await supervisor.invoke_api(target.plugin_id, API_NAME, {"message_id": int(target.platform_id)}, timeout_ms=15000)
    if response.error is not None or response.payload.get("success") is not True:
        return {"success": False, "status": "unknown", "message": "适配器未确认撤回成功；结果可能不确定，不自动重试"}
    result = response.payload.get("result")
    if not isinstance(result, dict) or result.get("status") != "ok" or type(result.get("retcode")) is not int or result["retcode"] != 0:
        return {"success": False, "status": "failed", "message": "平台未返回明确成功，未声明撤回成功"}
    return {"success": True, "status": "succeeded", "message": "平台已确认撤回自身消息。", "target": asdict(target)}
