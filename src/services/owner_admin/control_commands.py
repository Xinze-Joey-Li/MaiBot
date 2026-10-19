"""M2 的固定管理语法；没有任意配置键或自由 Prompt 参数。"""

from typing import Any, Dict, List, Optional, Tuple
import re

CONTROL_ACTIONS = {
    "mute": "B01", "frequency": "B02", "threshold": "B03", "mode": "B04",
    "style": "C01", "persona": "C02", "chat": "E02", "overrides": "owner_admin.overrides.read",
}
MODES = {"quiet", "normal", "active", "mention_only"}


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]{0,3})(m|h|d)", value)
    if match is None:
        raise ValueError("时长格式应为1m到7d")
    result = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if not 60 <= result <= 604800:
        raise ValueError("时长超限")
    return result


def field_value(property: str, text: str) -> Any:
    if property == "frequency" and re.fullmatch(r"(?:0(?:\.[0-9]{1,4})?|1(?:\.0{1,4})?)", text):
        return float(text)
    if property == "threshold" and re.fullmatch(r"[+-](?:[0-9]|[1-4][0-9]|50)(?:\.[0-9])?%", text):
        value = float(text[:-1]) / 100
        if abs(value) <= 0.5:
            return value
    if property == "mode" and text in MODES:
        return text
    raise ValueError("字段或值不在白名单")


def parse_control_command(args: List[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    if len(args) < 2:
        return None
    command = "mute" if args[1] == "unmute" else args[1]
    if command not in CONTROL_ACTIONS:
        return None
    parameters: Dict[str, Any] = {"command": command, "page": 1}
    parts = args[2:]
    try:
        if "--page" in parts:
            if command not in {"overrides", "chat"} or len(parts) < 2 or parts[-2] != "--page":
                return None
            parameters["page"] = int(parts[-1])
            if not 1 <= parameters["page"] <= 100:
                return None
            parts = parts[:-2]
        if command in {"mute", "frequency", "threshold", "mode", "chat"}:
            group = parts[1] if command == "chat" and len(parts) >= 2 else parts[0]
            if not re.fullmatch(r"[1-9][0-9]{4,19}", group):
                return None
            parameters["group_id"] = group
        if command == "mute":
            if args[1] == "unmute" and len(parts) == 1:
                parameters.update(property="muted", cancel=True)
            elif args[1] == "mute" and len(parts) == 2:
                parameters.update(property="muted", value=True, duration=duration_seconds(parts[1]))
            else:
                return None
        elif command in {"frequency", "threshold", "mode"}:
            parameters["property"] = command
            if len(parts) == 2 and parts[1] == ("normal" if command == "mode" else "reset"):
                parameters["cancel"] = True
            elif len(parts) == 3 and not (command == "mode" and parts[1] == "normal"):
                parameters.update(value=field_value(command, parts[1]), duration=duration_seconds(parts[2]))
            else:
                return None
        elif command in {"style", "persona"}:
            parameters["property"] = command
            if len(parts) == 1 and parts[0] == ("reset" if command == "style" else "default"):
                parameters["cancel"] = True
            elif len(parts) == 2 and parts[0] in ({"gentle", "calm"} if command == "style" else {"gentle", "playful", "calm"}):
                parameters.update(value=parts[0], duration=duration_seconds(parts[1]))
            else:
                return None
        elif command == "chat":
            parameters["operation"] = parts[0]
            if parts[0] == "config" and len(parts) == 2:
                pass
            elif parts[0] == "set" and len(parts) == 4 and parameters["page"] == 1:
                parameters.update(property=parts[2], priority=10)
                if parts[3] == "reset" or (parts[2] == "mode" and parts[3] == "normal"):
                    if parts[2] not in {"frequency", "threshold", "mode"}:
                        return None
                    parameters["cancel"] = True
                else:
                    parameters["value"] = field_value(parts[2], parts[3])
            else:
                return None
        elif command == "overrides":
            if len(parts) > 1 or (parts and not re.fullmatch(r"[1-9][0-9]{4,19}", parts[0])):
                return None
            if parts:
                parameters["group_id"] = parts[0]
    except (ValueError, IndexError):
        return None
    return f"owner_admin.control.{command}", parameters
