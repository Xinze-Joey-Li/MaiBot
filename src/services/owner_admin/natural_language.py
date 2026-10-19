"""受控自然语言模板 → 已注册 /dfy 语法；不向模型开放执行器或后台 API。"""

from typing import Any, Dict, Optional
import re

from .control_commands import CONTROL_ACTIONS
from .memory_access_commands import M3_ACTIONS
from .read_commands import READ_ACTIONS
from .read_data import known_chats
from .system_commands import SYSTEM_ACTIONS

# 所有25项能力都有固定映射；没有 /pm、安装、模型授权、任意API或文件路径。
ALIASES = {
    "查询运行状态": "status", "查询Token消耗": "tokens", "查询发言统计": "stats",
    "查看模型状态": "models", "查询异常": "errors", "决策回放": "replay",
    "群聊静音": "mute", "群聊恢复": "unmute", "调整发言频率": "frequency", "调整发言阈值": "threshold",
    "设置行为模式": "mode", "设置表达风格": "style", "切换人格预设": "persona", "查看人格配置": "persona show",
    "查看我的记忆": "memory me", "添加我的记忆": "memory add", "更正我的记忆": "memory edit",
    "撤回我的记忆": "memory delete", "查看学习状态": "learning status", "查看聊天列表": "chats",
    "查看群聊配置": "chat config", "设置群聊配置": "chat set", "查看接入名单": "access list",
    "允许群接入": "access allow", "禁止群接入": "access deny", "重置群接入": "access reset",
    "添加群聊白名单": "access allow group", "添加私聊白名单": "access allow private",
    "屏蔽用户": "ignore", "解除用户屏蔽": "unignore", "查看用户屏蔽": "ignore list",
    "撤回机器人消息": "recall", "查询服务健康": "health", "软暂停机器人": "pause", "恢复机器人": "resume",
    "查看暂停状态": "pause status", "查看插件列表": "plugin list", "启用插件": "plugin enable", "禁用插件": "plugin disable",
}


def top_level_text(message: Any) -> Optional[str]:
    from src.common.data_models.message_component_data_model import TextComponent

    components = message.raw_message.components
    if not components or any(not isinstance(component, TextComponent) for component in components):
        return None
    return "".join(component.text for component in components).strip()


def candidate(text: str) -> Optional[str]:
    if len(text) > 600:
        return None
    addressed = re.fullmatch(r"(?:大肥鱼[，,：:]\s*|管理[：:]\s*)(.+?)[。！!]?", text)
    body = addressed[1].strip() if addressed else text.rstrip("。！!")
    # 只识别完整添加句式与明确QQ数字ID；权限、目标核验及预览确认仍走原E03。
    whitelist = re.fullmatch(
        r"(?:请|帮我)?添加\s*(群聊|群|账号|QQ账号|QQ号|私聊)\s*([1-9][0-9]{4,19})\s*"
        r"(?:进入|进|加入|添加到|到)白名单[。！!]?", body,
    )
    if whitelist:
        kind = "group" if whitelist[1] in {"群聊", "群"} else "private"
        return f"/dfy access allow {kind} {whitelist[2]}"
    # 完整问句模板，不能仅因出现Token关键词而触发；相对“这几天”明确采用7d窗口。
    token_body = re.sub(r"\s+", "", body).rstrip("。！!？?")
    token_queries = {
        "今天用了多少Token": "today", "看看今天用了多少Token": "today",
        "看看最近一小时的Token消耗": "1h", "看看最近一小时用了多少Token": "1h",
        "今天调用了多少次模型": "today", "最近24小时的缓存命中率是多少": "24h",
        "这几天Token用量怎么样": "7d",
    }
    if token_body in token_queries and (addressed or token_body == "今天调用了多少次模型"):
        return "/dfy tokens " + token_queries[token_body]
    if addressed and token_body in {"今天API花了多少钱", "今天花了多少API费用", "今天的Token费用是多少"}:
        return "token-private"
    common = {"看看运行状态": "status", "看看今天用了多少Token": "tokens today", "看看今天用了多少 Token": "tokens today",
              "看看最近一小时用了多少Token": "tokens 1h", "查看健康状态": "health"}
    if addressed and body in common:
        return "/dfy " + common[body]
    if addressed:
        for alias, action in ALIASES.items():
            if body == alias or body.startswith(alias + " "):
                return "/dfy " + action + body[len(alias):]
    if re.fullmatch(r"(?:请|帮我)?添加\s*(?:群聊|群|账号|QQ账号|QQ号|私聊).*白名单.*", body):
        return "clarify"
    # 只认可完整、明确的群静音请求，不用“闭嘴/别说了/睡觉”等聊天调侃猜测暂停。
    quiet = re.fullmatch(r"(.{1,60}?群)最近太吵了[，,]先安静(一|两|二|三|四|五|六|七|八|九|十|[1-9][0-9]?)个?小时", body)
    if quiet:
        number = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        hours = number[quiet[2]] if quiet[2] in number else int(quiet[2])
        return "group-name:" + quiet[1] + ":" + str(hours)
    if (addressed and re.search(r"查询|查看|设置|切换|更正|撤回|屏蔽|禁用|启用|暂停|恢复|记忆|安静|温柔|Token|为什么没有回", body, re.I)) or re.fullmatch(r"(?:今天说话温柔一点|帮我查一下刚才为什么没有回我|把我刚才让你记住的那条信息改一下)", body):
        return "clarify"
    return None


def resolve_group_name(account: str, value: str) -> str:
    _, name, hours = value.split(":")
    matches = [chat for chat in known_chats(account) if chat.group_id and chat.group_name == name]
    if len(matches) != 1:
        raise ValueError("群名不存在、重名或不属于当前账号；请提供群ID")
    return f"/dfy mute {matches[0].group_id} {hours}h"


def validate_candidate(payload: Dict[str, Any], parse) -> Any:
    """即使未来接入 LLM，其结果也只能是这一字段，经现有确定性解析器重新校验。"""
    if set(payload) != {"command"} or not isinstance(payload["command"], str) or len(payload["command"]) > 600:
        raise ValueError("拒绝模型授权字段、任意参数或非结构化动作")
    parsed = parse(payload["command"])
    allowed = {*("owner_admin.read." + key for key in READ_ACTIONS),
               *("owner_admin.control." + key for key in CONTROL_ACTIONS),
               *("owner_admin.m3." + key for key in M3_ACTIONS if key != "confirm"),
               *("owner_admin.system." + key for key in SYSTEM_ACTIONS)}
    if not payload["command"].startswith("/dfy ") or parsed.action_id not in allowed:
        raise ValueError("动作未注册或不属于选定管理能力")
    return parsed


def mutating(parsed: Any) -> bool:
    if parsed.action_id.startswith("owner_admin.read."):
        return False
    p = parsed.parameters
    return not (p["command"] == "overrides" or (p["command"] == "chat" and p["operation"] == "config")
                or (p["command"] in {"plugin", "access", "ignore"} and p.get("operation") == "list")
                or (p["command"] == "pause" and p.get("operation") == "status"))
