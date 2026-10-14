"""Maisaka Planner-only 模式策略。"""

from src.config.config import global_config
from src.common.logger import get_logger

logger = get_logger("maisaka_mode_policy")

IDLE_CYCLE_REASONS = {
    "planner_no_tool_end",
    "planner_wait_rest",
    "tool_pause:wait",
    "tool_stop_after_execution",
}


def get_reply_trigger_mode() -> str:
    """读取当前回复触发模式。"""

    return global_config.chat.reply_timing.reply_trigger_mode


def is_dynamic_reply_trigger_enabled() -> bool:
    """判断是否启用动态回复触发门。"""

    return get_reply_trigger_mode() == "dynamic"


def log_reply_trigger_mode(context: str = "启动") -> None:
    """显示实际生效模式；不覆盖旧配置或将本地动态设置当作仓库默认。"""
    mode = get_reply_trigger_mode()
    enabled = "启用" if mode == "dynamic" else "停用"
    logger.info(f"{context} 回复触发模式: mode={mode} 权重评分={enabled} 动态门控={enabled}")


def is_idle_cycle_reason(cycle_end_reason: str) -> bool:
    """判断整轮结束原因是否属于空闲退避。"""

    return str(cycle_end_reason).strip() in IDLE_CYCLE_REASONS
