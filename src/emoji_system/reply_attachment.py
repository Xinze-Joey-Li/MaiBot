"""Replyer 同次请求选择文字标签；概率及图片发送全部在本地完成。"""

from typing import Dict, List, Tuple

import asyncio
import json
import random
import re

from src.common.data_models.image_data_model import MaiEmoji
from src.common.data_models.message_component_data_model import EmojiComponent, MessageSequence
from src.common.logger import get_logger
from src.common.prompt_i18n import load_prompt
from src.common.utils.image_path import resolve_stored_image_path

from .emoji_manager import _get_emoji_emotions, emoji_manager

logger = get_logger("reply_emoji")
EMOJI_TAG_PATTERN = re.compile(r"\[\[emoji_tag:([^\r\n]*?)\]\]")


def get_tag_candidates() -> Dict[str, List[MaiEmoji]]:
    candidates: Dict[str, List[MaiEmoji]] = {}
    for emoji in list(emoji_manager.emojis):
        for tag in _get_emoji_emotions(emoji):
            candidates.setdefault(tag, []).append(emoji)
    return candidates


def build_tag_prompt(candidates: Dict[str, List[MaiEmoji]]) -> str:
    if not candidates:
        return ""
    return load_prompt("reply_emoji_tag", tags=json.dumps(sorted(candidates), ensure_ascii=False))


def extract_tag(text: str, candidates: Dict[str, List[MaiEmoji]]) -> Tuple[str, str]:
    """移除私有输出标记；未知 tag 不匹配、不重试，不增加模型请求。"""
    matches = EMOJI_TAG_PATTERN.findall(text)
    selected_tag = matches[-1].strip() if matches else ""
    if selected_tag and selected_tag not in candidates:
        logger.warning(f"Replyer 选择了表情库中不存在的 tag: {selected_tag!r}")
        selected_tag = ""
    return EMOJI_TAG_PATTERN.sub("", text).strip(), selected_tag


async def send_after_text(stream_id: str, tag: str) -> None:
    """每条成功文字独立抽概率，再抽是否命中；没有匹配表情则跳过。"""
    probability = random.uniform(0.30, 0.50)
    if random.random() >= probability or not tag:
        return
    candidates = get_tag_candidates().get(tag, [])
    if not candidates:
        return
    emoji = random.choice(candidates)
    from src.services import send_service

    # 仅在确定发送时读取文件，不识图、不调用表情选择模型。失败单独记录，文字已成功。
    try:
        data = await asyncio.to_thread(resolve_stored_image_path(emoji.full_path).read_bytes)
        await send_service._send_to_target_with_message(
            message_sequence=MessageSequence([
                EmojiComponent(binary_hash=emoji.file_hash, binary_data=data, content=emoji.description),
            ]),
            stream_id=stream_id,
            set_reply=False,
            sync_to_maisaka_history=True,
            maisaka_source_kind="guided_reply",
        )
    except Exception:
        logger.exception(f"文字后的表情发送异常: session_id={stream_id} tag={tag!r}")
