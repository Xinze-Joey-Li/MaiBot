"""M4 固定系统操作；无任意 API、Shell、配置键入口。"""

from typing import Any, Dict, List, Optional, Tuple
import re

from .control_commands import duration_seconds

SYSTEM_ACTIONS = {"recall": "E06", "pause": "F02", "plugin": "F03"}


def parse_system_command(args: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    if len(args) < 2:
        return None
    command, parts = args[1], args[2:]
    parameters: Dict[str, Any] = {}
    try:
        if command == "recall" and len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", parts[0]):
            parameters["message_id"] = parts[0]
        elif command == "pause" and len(parts) == 1:
            parameters["operation"] = "status" if parts[0] == "status" else "pause"
            if parts[0] != "status":
                parameters["duration"] = duration_seconds(parts[0])
        elif command == "resume" and not parts:
            command, parameters = "pause", {"operation": "resume"}
        elif command == "plugin" and parts:
            if parts == ["list"]:
                parameters["operation"] = "list"
            elif len(parts) == 3 and parts[:2] == ["list", "--page"] and parts[2].isdigit() and 1 <= int(parts[2]) <= 100:
                parameters.update(operation="list", page=int(parts[2]))
            elif len(parts) == 2 and parts[0] in {"enable", "disable"} and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", parts[1]):
                parameters.update(operation=parts[0], plugin_id=parts[1])
            else:
                return None
        else:
            return None
    except ValueError:
        return None
    return "owner_admin.system." + command, {"command": command, **parameters}
