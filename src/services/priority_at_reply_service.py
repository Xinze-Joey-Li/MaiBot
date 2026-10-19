"""@ 机器人独立回复：绕过普通队列、wait、focus 和发言额度。"""

from collections import deque
from typing import Deque, Dict, List, Set, Tuple, TYPE_CHECKING

import asyncio
import random

from src.decision_replay.replies import DeliveryUncertain, priority_action
from src.services.owner_admin.override_runtime import priority_reply
from src.decision_replay.recorder import Trace, priority_cancel_queued, priority_queued, traced_priority

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage
    from src.maisaka.context.messages import LLMContextMessage
    from src.maisaka.runtime import MaisakaHeartFlowChatting


class PriorityAtReplyService:
    """独立于 Planner 的有界优先队列；正常流量保留并发及平台重投去重。"""

    def __init__(
        self, *, max_concurrent: int = 8, max_per_session: int = 2,
        max_pending: int = 128, max_pending_per_session: int = 32,
    ) -> None:
        if min(max_concurrent, max_per_session, max_pending, max_pending_per_session) < 1:
            raise ValueError("@ 并发和队列容量必须为正数")
        self._max_concurrent = max_concurrent
        self._max_per_session = max_per_session
        self._max_pending = max_pending
        self._max_pending_per_session = max_pending_per_session
        self._pending: Deque[Tuple[SessionMessage, MaisakaHeartFlowChatting, List[LLMContextMessage]]] = deque()
        self._pending_keys: Set[Tuple[str, str]] = set()
        self._running_per_session: Dict[str, int] = {}
        self._closing = False
        self._draining = False
        self._tasks: Dict[Tuple[str, str], asyncio.Task] = {}
        self._replay_traces: Dict[Tuple[str, str], Trace] = {}
        self._seen: Set[Tuple[str, str]] = set()
        self._seen_order: Deque[Tuple[str, str]] = deque()

    def claim(self, message: "SessionMessage") -> bool:
        from src.chat.utils.utils import is_mentioned_bot_in_message
        from src.config.config import global_config
        from src.services.bot_account_service import is_bot_self

        if not global_config.chat.priority_at_reply_enabled or message.is_notify or message.is_command:
            return False
        if is_bot_self(message.platform, message.message_info.user_info.user_id):
            return False
        _, is_at, _ = is_mentioned_bot_in_message(message)
        if not is_at:
            return False
        message.is_at = True
        message.is_mentioned = True
        # 这里只识别候选，实际接纳成功前不转移普通 Planner 的处理资格。
        message.message_info.additional_config["priority_at_requested"] = True
        return True

    def owns(self, message: "SessionMessage") -> bool:
        """已接纳的消息（含运行、排队和近期完成）不能退回普通 Planner。"""
        key = (message.session_id, message.message_id)
        return key in self._tasks or key in self._pending_keys or key in self._seen

    def start_reply(
        self, message: "SessionMessage", runtime: "MaisakaHeartFlowChatting",
        *, history: List["LLMContextMessage"] | None = None,
    ) -> bool:
        key = (message.session_id, message.message_id)
        if self._closing or self._draining or key in self._tasks or key in self._pending_keys or key in self._seen:
            return False
        queued_for_session = sum(item[0].session_id == message.session_id for item in self._pending)
        can_start = (len(self._tasks) < self._max_concurrent
                     and self._running_per_session.get(message.session_id, 0) < self._max_per_session)
        if not can_start and (len(self._pending) >= self._max_pending
                              or queued_for_session >= self._max_pending_per_session):
            from src.common.logger import get_logger

            get_logger("priority_at_reply").warning(
                f"@ 优先回复过载，拒绝本次入队: session_id={message.session_id} message_id={message.message_id} "
                f"running={len(self._tasks)} pending={len(self._pending)} session_pending={queued_for_session}"
            )
            priority_queued(self, message, overloaded=True)
            # 拒绝项不取得处理权，由本次入站的普通 Planner 路径继续处理。
            return False
        priority_queued(self, message)
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 2000:
            self._seen.discard(self._seen_order.popleft())
        # 每次回复使用独立上下文快照，不共享 Replyer 可变状态，也不加入普通 turn 队列。
        self._pending.append((message, runtime, list(runtime._chat_history if history is None else history)))
        self._pending_keys.add(key)
        self._dispatch()
        return True

    def _dispatch(self) -> None:
        # 跳过已用满单群额度的队列项，避免一个群占住所有其它群的可用槽位。
        while not self._closing and len(self._tasks) < self._max_concurrent:
            for _ in range(len(self._pending)):
                message, runtime, history = self._pending.popleft()
                session_id = message.session_id
                if self._running_per_session.get(session_id, 0) >= self._max_per_session:
                    self._pending.append((message, runtime, history))
                    continue
                key = (session_id, message.message_id)
                self._pending_keys.remove(key)
                self._running_per_session[session_id] = self._running_per_session.get(session_id, 0) + 1
                task = asyncio.create_task(self._reply(message, runtime, history), name=f"priority_at:{message.message_id}")
                self._tasks[key] = task
                task.add_done_callback(lambda completed, reply_key=key: self._finish(reply_key, completed))
                break
            else:
                break

    def _finish(self, key: Tuple[str, str], task: asyncio.Task) -> None:
        from src.common.logger import get_logger

        self._tasks.pop(key, None)
        self._replay_traces.pop(key, None)
        running = self._running_per_session[key[0]] - 1
        if running:
            self._running_per_session[key[0]] = running
        else:
            del self._running_per_session[key[0]]
        self._dispatch()
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            get_logger("priority_at_reply").error(
                f"@ 独立回复失败: session_id={key[0]} message_id={key[1]}",
                exc_info=(type(error), error, error.__traceback__),
            )

    @traced_priority
    @priority_action
    @priority_reply
    async def _reply(
        self,
        message: "SessionMessage",
        runtime: "MaisakaHeartFlowChatting",
        history: List["LLMContextMessage"],
    ) -> None:
        from src.decision_replay.recorder import record
        standalone = self._is_standalone_bot_at(message)
        emoji_branch = standalone and random.random() < 0.5
        record('emoji', 'emoji.priority_branch', 'emoji' if emoji_branch else 'text',
               'priority_at_random' if standalone else 'priority_at_text',
               detail={'probability':0.5 if standalone else None,'hit':emoji_branch,'cooldown_exempt':True})
        if emoji_branch:
            try:
                await self._send_emoji(message)
                return
            except DeliveryUncertain:
                raise
            except Exception:
                record("emoji", "emoji.priority_fallback", "failed", "fixed_emoji_unavailable")
                from src.common.logger import get_logger

                get_logger("priority_at_reply").exception(
                    f"@ 固定表情不可用或发送失败，改为文字回复: session_id={message.session_id}"
                )
            await self._send_text(message, runtime, history)
        else:
            await self._send_text(message, runtime, history)

    @staticmethod
    def _is_standalone_bot_at(message: "SessionMessage") -> bool:
        """只允许单个机器人 @ 和空白，不把显示名或媒体误当成空正文。"""
        from src.common.data_models.message_component_data_model import AtComponent, TextComponent
        from src.services.bot_account_service import is_bot_self

        at_count = 0
        for component in message.raw_message.components:
            if isinstance(component, AtComponent):
                if not is_bot_self(message.platform, component.target_user_id):
                    return False
                at_count += 1
            elif isinstance(component, TextComponent):
                if component.text.strip():
                    return False
            else:
                return False
        return at_count == 1

    async def _send_emoji(self, message: "SessionMessage") -> None:
        from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence
        from src.common.utils.image_path import resolve_stored_image_path
        from src.config.config import global_config
        from src.emoji_system.emoji_manager import emoji_manager
        from src.services.send_service import send_priority_at_reply

        # 配置候选先去重，再等概率抽取；不可用的固定候选明确报错，不替换成其它表情。
        hashes = list(dict.fromkeys(value.strip() for value in global_config.chat.priority_at_reply_emoji_hashes))
        if not hashes:
            raise RuntimeError("@ 即时回复固定表情列表尚未配置")
        emojis = []
        for emoji_hash in hashes:
            emoji = emoji_manager.get_emoji_by_hash(emoji_hash)
            if emoji is None:
                raise RuntimeError(f"@ 固定表情未注册或不可用: {emoji_hash}")
            emojis.append(emoji)
        emoji = random.choice(emojis)
        from src.decision_replay.recorder import record
        record('emoji', 'emoji.selection', 'selected', 'priority_fixed_random',
               detail={'candidate_count':len(emojis),'selected_hash':emoji.file_hash,'cooldown_exempt':True})
        image_bytes = await asyncio.to_thread(lambda: resolve_stored_image_path(emoji.full_path).read_bytes())
        sequence = MessageSequence([EmojiComponent(binary_hash=emoji.file_hash, binary_data=image_bytes)])
        if not await send_priority_at_reply(sequence, message, set_reply=False):
            raise RuntimeError("@ 表情回复发送失败")

    async def _send_text(
        self,
        message: "SessionMessage",
        runtime: "MaisakaHeartFlowChatting",
        history: List["LLMContextMessage"],
    ) -> None:
        from src.services.owner_control_service import handle_owner_command
        from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        from src.emoji_system.reply_attachment import send_after_text
        from src.services.send_service import send_priority_at_reply

        owner_response = await handle_owner_command(message)
        if owner_response is not None:
            if not await send_priority_at_reply(MessageSequence([TextComponent(owner_response)]), message):
                raise RuntimeError("主人管理命令回执发送失败")
            return
        replyer = MaisakaReplyGenerator(chat_stream=runtime.chat_stream, request_type="priority_at.replyer")
        success, result = await replyer.generate_reply_with_context(
            stream_id=message.session_id, reply_message=message, chat_history=history,
            reply_tool_args={}, log_reply=False,
        )
        if not success or not result.completion.response_text.strip():
            raise RuntimeError(f"@ 模型回复生成失败: {result.error_message}")
        sequence = MessageSequence([TextComponent(result.completion.response_text.strip())])
        if not await send_priority_at_reply(sequence, message):
            raise RuntimeError("@ 模型回复发送失败")
        await send_after_text(message.session_id, result.selected_emoji_tag,
                              (message.processed_plain_text or "") + "\n" + result.completion.response_text)

    async def shutdown(self, *, drain: bool = False) -> None:
        self._draining = True
        if drain:
            # 拒绝新任务，继续按原并发限制派发已接纳的内存队列。
            while self._tasks or self._pending:
                self._dispatch()
                await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
                await asyncio.sleep(0)  # 让完成回调释放额度并派发下一批。
        self._closing = True
        priority_cancel_queued(self)
        self._pending.clear()
        self._pending_keys.clear()
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._running_per_session.clear()
        self._seen.clear()
        self._seen_order.clear()
        self._replay_traces.clear()


priority_at_reply_service = PriorityAtReplyService()
