"""群聊纯文本复读检测；发送独立于 Planner 和回复频率门控。"""

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage


@dataclass
class RepeatState:
    text: str = ""
    count: int = 0
    senders: Set[str] = field(default_factory=set)
    triggered: bool = False
    message_ids: Deque[str] = field(default_factory=lambda: deque(maxlen=64))


class AutoRepeatDetector:
    """按真实会话隔离计数，同一轮连续复读只触发一次。"""

    def __init__(self) -> None:
        self._states: Dict[str, RepeatState] = {}

    def clear(self) -> None:
        self._states.clear()

    def observe(self, *, session_id: str, message_id: str, user_id: str, text: Optional[str]) -> bool:
        state = self._states.setdefault(session_id, RepeatState())
        # 平台重投同一条消息不能增加复读数，也不能打断当前序列。
        if message_id in state.message_ids:
            return False
        state.message_ids.append(message_id)
        normalized_text = text.strip() if text is not None else ""
        if not normalized_text or normalized_text != state.text:
            state.text = normalized_text
            state.count = 1 if normalized_text else 0
            state.senders = {user_id} if normalized_text else set()
            state.triggered = False
        else:
            state.count += 1
            state.senders.add(user_id)
        if state.count > 3 and len(state.senders) >= 2 and not state.triggered:
            # 在 await 发送前占用本轮触发，避免并发入站重复发送。
            state.triggered = True
            return True
        return False


class AutoRepeatService:
    def __init__(self) -> None:
        self.detector = AutoRepeatDetector()

    async def on_incoming_message(self, message: "SessionMessage") -> None:
        from src.common.data_models.message_component_data_model import TextComponent
        from src.config.config import global_config
        from src.services.bot_account_service import is_bot_self
        from src.services.send_service import auto_repeat_to_stream

        if not global_config.chat.auto_repeat_enabled:
            self.detector.clear()
            return
        if message.message_info.group_info is None:
            return
        user_id = message.message_info.user_info.user_id
        if is_bot_self(message.platform, user_id):
            return
        if message.is_notify:
            return
        components = message.raw_message.components
        text: Optional[str] = None
        if components and all(isinstance(component, TextComponent) for component in components) and not message.is_command:
            text = "".join(component.text for component in components)
        if self.detector.observe(
            session_id=message.session_id, message_id=message.message_id, user_id=user_id, text=text
        ):
            assert text is not None  # 只有非空纯文本能达到复读阈值。
            if not await auto_repeat_to_stream(text, message.session_id):
                raise RuntimeError(f"自动复读发送失败: session_id={message.session_id}")


auto_repeat_service = AutoRepeatService()
