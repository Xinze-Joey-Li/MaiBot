"""M1 只读命令的严格语法；权限仍由 M0 注册表和 Guard 决定。"""

from typing import Any, Dict, List, Optional, Tuple
import re

READ_ACTIONS = {
    "status": ("A01", "运行状态"), "tokens": ("A02", "Token 消耗"),
    "stats": ("A03", "发言统计"), "models": ("A04", "模型状态"),
    "errors": ("A05", "结构化异常"), "replay": ("A06", "决策回放"),
    "persona": ("C04", "人格配置"), "memory": ("D01", "主人自身记忆"),
    "learning": ("D04", "学习状态"), "chats": ("E01", "聊天列表"),
    "health": ("F01", "服务健康"),
}
PUBLIC_GROUP_READ_ACTIONS = frozenset({"owner_admin.read.tokens"})

WINDOWS = {"1h": 1, "24h": 24, "7d": 168, "today": 0}


def parse_read_command(args: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """只接受固定窗口和分页；不接收 person_id、SQL、任意路径或写入动作。"""
    if len(args) < 2 or args[1] not in READ_ACTIONS:
        return None
    parts = args[2:]
    page = 1
    if "--page" in parts:
        if len(parts) < 2 or parts[-2] != "--page" or not parts[-1].isdigit():
            return None
        page = int(parts[-1])
        parts = parts[:-2]
    if not 1 <= page <= 100:
        return None
    command = args[1]
    parameters: Dict[str, Any] = {"command": command, "page": page}
    if command in {"status", "models", "chats", "health"}:
        valid = not parts
    elif command in {"tokens", "errors"}:
        if command == "tokens" and not parts:
            parts = ["24h"]
        valid = len(parts) == 1 and parts[0] in WINDOWS
        if valid:
            parameters["window"] = parts[0]
    elif command == "stats":
        valid = len(parts) in {1, 2} and parts[-1] in WINDOWS
        if len(parts) == 2:
            valid = valid and bool(re.fullmatch(r"[0-9]{1,20}", parts[0]))
            parameters["group_id"] = parts[0]
        if valid:
            parameters["window"] = parts[-1]
    elif command == "replay":
        valid = (len(parts) == 1 and bool(re.fullmatch(r"[\w.-]{1,128}", parts[0]))
                 and parts[0] != "latest")
        if len(parts) == 2:
            valid = parts[0] == "latest" and bool(re.fullmatch(r"[0-9]{1,20}", parts[1]))
            parameters["group_id"] = parts[1]
        elif valid:
            parameters["message_id"] = parts[0]
    elif command == "persona":
        valid = parts == ["show"]
    elif command == "learning":
        valid = parts == ["status"]
    else:
        valid = 1 <= len(parts) <= 2 and parts[0] == "me"
        if len(parts) == 2:
            valid = valid and 1 <= len(parts[1]) <= 80 and not parts[1].startswith("-")
            parameters["keyword"] = parts[1]
    return (f"owner_admin.read.{command}", parameters) if valid else None
