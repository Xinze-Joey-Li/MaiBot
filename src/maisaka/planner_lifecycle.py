"""本地回合结束策略；未知工具结果保守地继续 Planner。"""

from typing import Sequence

from src.config.config import global_config
from src.core.tooling import ToolExecutionResult


def lifecycle_mode() -> str:
    return global_config.chat.reply_timing.planner_lifecycle_mode


def local_end_reason(results: Sequence[ToolExecutionResult]) -> str:
    """只有所有动作都声明完成且不需分析时才终止，不按工具名称推断。"""
    if not results:
        return ""
    for result in results:
        if not result.success:
            return ""
        if result.metadata.get("planner_task_completed") is not True:
            return ""
        if result.metadata.get("planner_continuation_required", True) is not False:
            return ""
        if result.post_history_messages or result.stop_after_execution:
            return ""
    return "completed_actions"
