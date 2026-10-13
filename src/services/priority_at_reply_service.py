"""@ 机器人独立回复：绕过普通队列、wait、focus 和发言额度。"""

from collections import deque
from typing import Deque, Dict, List, Set, Tuple, TYPE_CHECKING

import asyncio
import random

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage
    from src.maisaka.context.messages import LLMContextMessage
    from src.maisaka.runtime import MaisakaHeartFlowChatting


class PriorityAtReplyService:
    """每条 @ 启动独立任务；仅去重平台重投，不设置发言数量或串行等待限制。"""

    def __init__(self) -> None:
        self._tasks: Dict[Tuple[str, str], asyncio.Task] = {}
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
        # 在保存和注册前标记归属，普通 Planner 不再重复调度这条消息。
        message.message_info.additional_config["priority_at_handled"] = True
        return True

    def start_reply(self, message: "SessionMessage", runtime: "MaisakaHeartFlowChatting") -> None:
        key = (message.session_id, message.message_id)
        if key in self._tasks or key in self._seen:
            return
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > 2000:
            self._seen.discard(self._seen_order.popleft())
        # 每次回复使用独立上下文快照，不共享 Replyer 可变状态，也不加入普通 turn 队列。
        history = list(runtime._chat_history)
        task = asyncio.create_task(self._reply(message, runtime, history), name=f"priority_at:{message.message_id}")
        self._tasks[key] = task
        task.add_done_callback(lambda completed: self._finish(key, completed))

    def _finish(self, key: Tuple[str, str], task: asyncio.Task) -> None:
        from src.common.logger import get_logger

        self._tasks.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            get_logger("priority_at_reply").error(
                f"@ 独立回复失败: session_id={key[0]} message_id={key[1]}",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _reply(
        self,
        message: "SessionMessage",
        runtime: "MaisakaHeartFlowChatting",
        history: List["LLMContextMessage"],
    ) -> None:
        if self._is_standalone_bot_at(message) and random.random() < 0.5:
            await self._send_emoji(message)
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
        from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        from src.services.send_service import send_priority_at_reply

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

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._seen.clear()
        self._seen_order.clear()


priority_at_reply_service = PriorityAtReplyService()
