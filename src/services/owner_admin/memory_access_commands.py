"""M3 固定命令语法；内容是数据，不能选择 person_id、账号或系统提示。"""

from typing import Any, Dict, List, Optional, Tuple
import re

from .control_commands import duration_seconds

M3_ACTIONS = {"memory_add": "D02", "memory_edit": "D03.edit", "memory_delete": "D03.delete",
              "access": "E03", "ignore": "E04", "confirm": "owner_admin.m3.confirm"}


def parse_m3_command(args: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    if len(args) < 2:
        return None
    parts = args[2:]
    command = args[1]
    data: Dict[str, Any] = {}
    try:
        if command in {"access", "ignore"} and len(parts) == 3 and parts[0] == "list" and parts[1] == "--page":
            data["page"] = int(parts[2])
            if not 1 <= data["page"] <= 100:
                return None
            parts = ["list"]
        if command == "memory" and parts and parts[0] in {"add", "edit", "delete"}:
            operation = parts.pop(0)
            command = "memory_" + operation
            if operation == "add" and parts:
                data["content"] = " ".join(parts)
            elif operation in {"edit", "delete"} and parts and re.fullmatch(r"[a-f0-9]{32}", parts[0]):
                data["memory_id"] = parts[0]
                if operation == "edit" and len(parts) > 1:
                    data["content"] = " ".join(parts[1:])
                elif len(parts) != 1:
                    return None
            else:
                return None
        elif command == "access" and parts:
            data["operation"] = parts[0]
            if parts == ["list"]:
                pass
            elif len(parts) == 2 and parts[0] in {"allow", "deny", "reset"} and re.fullmatch(r"[1-9][0-9]{4,19}", parts[1]):
                data["group_id"] = parts[1]
            else:
                return None
        elif command in {"ignore", "unignore"}:
            data["cancel"] = command == "unignore"
            command = "ignore"
            if parts == ["list"] and not data["cancel"]:
                data["operation"] = "list"
            else:
                data["operation"] = "set"
                if not parts or not re.fullmatch(r"[1-9][0-9]{4,19}", parts[0]):
                    return None
                data["user_id"] = parts.pop(0)
                if not data["cancel"]:
                    data["duration"] = duration_seconds(parts.pop(0))
                if parts == ["--global"]:
                    data["global"] = True
                elif len(parts) == 2 and parts[0] == "--group" and re.fullmatch(r"[1-9][0-9]{4,19}", parts[1]):
                    data["group_id"] = parts[1]
                elif parts:
                    return None
        elif command == "confirm" and len(parts) == 1 and re.fullmatch(r"[a-f0-9]{32}", parts[0]):
            data["token"] = parts[0]
        else:
            return None
    except (IndexError, ValueError):
        return None
    return f"owner_admin.m3.{command}", {"command": command, **data}
