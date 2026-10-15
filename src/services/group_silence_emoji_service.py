"""独立 QQ 群静默表情：白天连续静默 45 分钟后随机发送。"""

from datetime import datetime, timedelta
from typing import Dict, TYPE_CHECKING

import asyncio
import random
import time

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.chat.message_receive.message import SessionMessage

SILENCE_SECONDS = 45 * 60
CHECK_INTERVAL_SECONDS = 30
logger = get_logger("group_silence_emoji")


class GroupSilenceEmojiService:
    def __init__(self) -> None:
        self._last_activity: Dict[str, float] = {}

    def record_activity(self, message: "SessionMessage") -> None:
        if message.platform != "qq" or message.is_notify:
            return
        if message.message_info.group_info is None:
            return
        self._last_activity[message.session_id] = time.time()

    async def check_once(self, now: datetime) -> None:
        from src.chat.message_receive.chat_manager import chat_manager
        from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence
        from src.common.utils.image_path import resolve_stored_image_path
        from src.emoji_system.emoji_manager import emoji_manager
        from src.services.send_service import send_group_silence_emoji

        timestamp = now.timestamp()
        check_started = time.monotonic()
        # 只使用已注册的真实群会话；首次见到的群从当前时刻开始，不补发离线期间的消息。
        sessions = {session.session_id: session for session in list(chat_manager.sessions.values())}
        for session in sessions.values():
            if session.platform == "qq" and session.is_group_session:
                self._last_activity.setdefault(session.session_id, timestamp)
        for session_id, last_activity in list(self._last_activity.items()):
            session = sessions.get(session_id)
            if session is None or not session.is_group_session or session.platform != "qq":
                del self._last_activity[session_id]
                continue
            if now.hour < 8:
                # 夜间不积累补发额度，08:00 后重新观察 45 分钟。
                self._last_activity[session_id] = now.replace(hour=8, minute=0, second=0, microsecond=0).timestamp()
                continue
            if timestamp - last_activity < SILENCE_SECONDS:
                continue
            # 发送失败也间隔一整轮才再尝试，不以巡检周期持续刷请求。
            self._last_activity[session_id] = timestamp
            from src.decision_replay.recorder import current_trace, new_trace, record
            replay_token = current_trace.set(new_trace(session_id, 'silence_emoji'))
            replay_sent = False
            record('scheduler', 'turn.started', 'started', 'silence_threshold',
                   detail={'silence_seconds':SILENCE_SECONDS,'cooldown_exempt':True})
            try:
                if not emoji_manager.emojis:
                    raise RuntimeError("没有可发送的已注册表情包")
                emoji = random.choice(list(emoji_manager.emojis))
                record('emoji', 'emoji.selection', 'selected', 'silence_random',
                       detail={'candidate_count':len(emoji_manager.emojis),'selected_hash':emoji.file_hash})
                data = await asyncio.to_thread(
                    lambda path=emoji.full_path: resolve_stored_image_path(path).read_bytes()
                )
                # 文件读取期间若有人发言，放弃这次发送。
                if self._last_activity.get(session_id) != timestamp:
                    continue
                # 跨到午夜后不能发送；使用本次检查的时钟加耗时，测试也可使用固定时间。
                if (now + timedelta(seconds=time.monotonic() - check_started)).hour < 8:
                    continue
                sequence = MessageSequence([EmojiComponent(binary_hash=emoji.file_hash, binary_data=data)])
                if not await send_group_silence_emoji(sequence, session_id):
                    raise RuntimeError("群静默表情发送失败")
                replay_sent = True
                logger.info(f"群静默表情已发送: session_id={session_id} 静默阈值=45分钟")
            except Exception:
                record('emoji', 'emoji.completed', 'failed', 'silence_send_failed')
                logger.exception(f"群静默表情失败: session_id={session_id}")
            finally:
                record('scheduler', 'turn.ended', 'completed' if replay_sent else 'not_sent',
                       'sent' if replay_sent else 'silence_attempt_not_sent')
                current_trace.reset(replay_token)

    async def run(self) -> None:
        logger.info("群静默表情巡检已启动: 静默45分钟，00:00–08:00停用，每群独立计时")
        try:
            while True:
                await self.check_once(datetime.now())
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        finally:
            self._last_activity.clear()


group_silence_emoji_service = GroupSilenceEmojiService()
