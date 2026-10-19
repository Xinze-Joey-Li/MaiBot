"""主人只读查询业务层：有界、可分页，缺失证据明确标记，不进行模型生成。"""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
import asyncio
import json
import os
import re
import sqlite3
import time

from . import read_data
from .models import AdminActionRequest, AdminActionResult
from .read_commands import READ_ACTIONS, WINDOWS

LOCAL_TZ = timezone(timedelta(hours=8))
PAGE_LINES = 4
MAX_LINE = 230
MAX_QUERY_EVENTS = 50000
MAX_SESSIONS = 500
_SECRET = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{12,}|bearer\s+\S+|(?:api[_-]?key|access[_-]?token|password|secret|cookie)"
    r"""["']?\s*[:=]\s*["']?[^\s,;]+|https?://\S+|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})"""
)


def safe_text(value: Any) -> str:
    text = _SECRET.sub("[redacted]", str(value))
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:MAX_LINE] + ("…（摘要）" if len(text) > MAX_LINE else "")


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, LOCAL_TZ).isoformat(timespec="seconds")


def query_window(name: str) -> Tuple[datetime, datetime]:
    hours = WINDOWS[name]
    end = datetime.now(LOCAL_TZ)
    return (end.replace(hour=0, minute=0, second=0, microsecond=0) if name == "today" else end - timedelta(hours=hours)), end


def report(command: str, lines: List[str], page: int, *, note: str = "") -> AdminActionResult:
    """每次只回一页，不刷屏；所有动态文本先清洗，再限制总长度。"""
    if len(lines) > PAGE_LINES * 100:
        lines = lines[:PAGE_LINES * 100]
        note = "结果超过上限，仅展示前400行；" + note
    if not lines:
        lines = ["无可用记录；不推断为没有发生。"]
    pages = max(1, (len(lines) + PAGE_LINES - 1) // PAGE_LINES)
    selected = lines[(page - 1) * PAGE_LINES:page * PAGE_LINES]
    if page > pages:
        selected = ["页码超出范围。"]
    footer = f"第 {page}/{pages} 页；同一命令追加 --page N 翻页（每次实时查询）"
    message = "\n".join([READ_ACTIONS[command][1], *[safe_text(line) for line in selected], safe_text(note), footer])
    return AdminActionResult(True, "succeeded", message, data={
        "page": page, "pages": pages, "source_note": note, "row_count": len(lines),
    })


class OwnerReadService:
    """处理器只接受 M0 已授权的不可变请求，不从昵称、模型或 person_id 取权限。"""

    def __init__(self) -> None:
        self._query_slots = asyncio.Semaphore(2)

    async def handle(self, request: AdminActionRequest) -> AdminActionResult:
        command = str(request.parameters["command"])
        if command == "tokens" and request.actor_identity.chat_type == "group" and request.parameters.get("private_details"):
            return AdminActionResult(False, "private_required", "费用金额、账单及账户详情仅限主人私聊查询，请私聊使用 /dfy tokens today。")
        if command not in READ_ACTIONS or request.action_id != f"owner_admin.read.{command}":
            raise ValueError("只读动作不匹配")
        # DB/文件/聚合全部在线程执行；线程内不调用路由，不触发热更新和配置写入。
        async with self._query_slots:
            if command in {"status", "health", "chats"}:
                request = replace(request, parameters={**request.parameters, "runtime_snapshot": self._runtime_snapshot()})
            lines, note = await asyncio.to_thread(self._query, command, request)
        return report(command, lines, int(request.parameters.get("page", 1)), note=note)

    @staticmethod
    def _runtime_snapshot() -> Dict[str, Any]:
        """在主循环复制内存状态，工作线程只读取快照，不遍历正在变动的字典。"""
        from src.chat.message_receive.chat_manager import chat_manager
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.common.runtime_loop import get_main_loop
        from src.platform_io.manager import get_platform_io_manager
        from src.plugin_runtime.integration import get_plugin_runtime_manager

        loop = get_main_loop()
        manager = get_plugin_runtime_manager()
        chats = {
            sid: {"running": chat._running, "state": chat._agent_state,
                  "pending": chat._get_pending_message_count(), "turn_queue": chat._internal_turn_queue.qsize()}
            for sid, chat in heartflow_manager.heartflow_chat_list.items()
        }
        return {"loop_running": loop is not None and loop.is_running(),
                "broker_started": get_platform_io_manager().is_started, "plugins_running": manager.is_running,
                "plugin_statuses": manager.get_plugin_load_statuses(), "loaded_chats": frozenset(chat_manager.sessions),
                "chat_runtimes": chats}

    def _query(self, command: str, request: AdminActionRequest) -> Tuple[List[str], str]:
        handlers = {
            "status": self._status, "health": self._health, "tokens": self._tokens, "stats": self._stats,
            "models": self._models, "errors": self._errors, "replay": self._replay, "persona": self._persona,
            "memory": self._memory, "learning": self._learning, "chats": self._chats,
        }
        try:
            return handlers[command](request)
        except (FileNotFoundError, sqlite3.OperationalError) as exc:
            # 只报告错误类型，不能把包含路径/查询参数的底层异常原文发到 QQ。
            return [f"数据源不可用（{type(exc).__name__}）；未生成替代统计。"], "请检查本地日志或 WebUI 数据源。"

    @staticmethod
    def _scope(request: AdminActionRequest) -> List[Any]:
        rows = read_data.known_chats(request.actor_identity.bot_account_id,
                                     group_id=str(request.parameters.get("group_id", "")))
        if len(rows) > MAX_SESSIONS:
            raise ValueError("聊天流超过查询上限；请指定群 ID 或使用 WebUI")
        return rows

    @staticmethod
    def _window(request: AdminActionRequest) -> Tuple[datetime, datetime, str]:
        start, end = query_window(str(request.parameters.get("window", "7d")))
        note = f"Asia/Shanghai {start.isoformat(timespec='seconds')} 至 {end.isoformat(timespec='seconds')}"
        return start, end, note

    def _status(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.config.config import MMC_VERSION
        snapshot = request.parameters["runtime_snapshot"]
        lines = [f"版本：{MMC_VERSION}；主进程 PID={os.getpid()}",
                 f"主程序事件循环：{'运行' if snapshot['loop_running'] else '状态不可用'}",
                 f"平台 Broker 已启动：{snapshot['broker_started']}；插件运行时已启动：{snapshot['plugins_running']}",
                 "QQ 实时连接状态：不可用；插件已加载不等于 QQ 已连通。",
                 *self._process_usage()]
        return lines, "当前进程观测；不使用历史账号注册时间冒充在线连接。"

    @staticmethod
    def _process_usage() -> List[str]:
        if os.name != "nt":
            return [f"CPU 累计时间={time.process_time():.2f}s；在线时长、内存：当前平台接口不可用"]
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                    "PeakPagefileUsage", "PrivateUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel.GetCurrentProcess()
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return ["CPU、内存、在线时长：Windows 查询失败"]
        def ticks(value: wintypes.FILETIME) -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime
        uptime = max(0.001, time.time() - (ticks(created) / 10000000 - 11644473600))
        cpu = (ticks(system) + ticks(user)) / 10000000
        memory = Counters()
        memory.cb = ctypes.sizeof(memory)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
        return [f"在线时长（当前进程存活）={uptime:.0f}s；CPU 生命期单核平均={100 * cpu / uptime:.2f}%",
                f"内存工作集={memory.WorkingSetSize / 1048576:.1f} MiB" if ok else "内存工作集：不可用"]

    def _health(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.decision_replay.recorder import store

        lines, note = self._status(request)
        lines.append("数据库：" + read_data.database_probe())
        statuses = request.parameters["runtime_snapshot"]["plugin_statuses"]
        lines.extend(f"插件 {name}：{state}（加载状态）" for name, state in sorted(statuses.items())[:100])
        lines.extend([f"回放队列={store.queue.qsize()}；累计丢弃={store.dropped}；写入错误={store.errors}",
                      "事件循环卡顿：现有看门狗只写日志，无可查询快照；其他任务健康无统一监控来源。"])
        runtimes = request.parameters["runtime_snapshot"]["chat_runtimes"]
        lines.append(f"已知聊天 Runtime={len(runtimes)}；运行标记={sum(item['running'] for item in runtimes.values())}；"
                     f"待处理消息={sum(item['pending'] for item in runtimes.values())}；"
                     f"内部轮次队列={sum(item['turn_queue'] for item in runtimes.values())}（快照，非健康保证）")
        return lines, note

    def _tokens(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.services.statistics_service import _get_model_statistics_sync, _get_summary_statistics_sync

        start, end, note = self._window(request)
        # ModelUsage 按宿主本地墙钟存储，与现有 WebUI 同区间、同查询服务。
        local_start, local_end = start.astimezone().replace(tzinfo=None), end.astimezone().replace(tzinfo=None)
        summary = _get_summary_statistics_sync(local_start, local_end)
        models = _get_model_statistics_sync(local_start, local_end, limit=400)
        if request.actor_identity.chat_type == "group":
            return self.public_view(summary, models), note + "；[start,end]；当前宿主整体记录，非本群估算；实时查询。"
        rate = "不可统计" if summary.cache_hit_rate is None else f"{summary.cache_hit_rate:.2%}"
        lines = [f"记录请求={summary.total_requests}；输入={summary.input_tokens}；输出={summary.output_tokens}",
                 f"Prompt Cache 命中={summary.cache_hit_tokens}；未命中={summary.cache_miss_tokens}；命中率={rate}",
                 f"本地记录估算费用={summary.total_cost:.6f}；真实账单：不可获取（非已结算金额）"]
        lines.append("缓存按 WebUI 归一化口径，缺少未命中数时可能按输入补全；不是逐请求账单。")
        lines.extend(f"模型 {row.model_name}：请求={row.request_count}；输入={row.input_tokens}；"
                     f"输出={row.output_tokens}；缓存命中={row.cache_hit_tokens}；估算费用={row.total_cost:.6f}" for row in models)
        return lines, note + "；[start,end]；WebUI 同源 ModelUsage；模型上限400项；全局宿主统计。费用币种未记录。"

    @staticmethod
    def public_view(summary: Any, models: List[Any]) -> List[str]:
        """A02公开字段投影；不传递私有对象给模型，也不发布自定义模型/供应商标签。"""
        rate = "不可统计" if summary.cache_hit_rate is None else f"{summary.cache_hit_rate:.2%}"
        lines = [
            f"记录请求={summary.total_requests}；输入={summary.input_tokens}；输出={summary.output_tokens}；总Token={summary.total_tokens}",
            f"缓存命中Token={summary.cache_hit_tokens}；未命中Token={summary.cache_miss_tokens}；命中率={rate}",
            "口径：仅已记录ModelUsage调用；未落库失败不计。缓存按启用缓存的输入归一化，命中率=命中/(命中+未命中)。",
            "缓存缺少未命中数时沿用WebUI按输入补全；无缓存样本时命中率不可统计。费用请私聊；分模型最多400项，以匿名编号展示。",
        ]
        if summary.total_requests == 0:
            lines.append("区间无已记录调用；不代表没有未记录的失败请求。")
        lines.extend(f"模型{index}（匿名）：请求={row.request_count}；输入={row.input_tokens}；输出={row.output_tokens}；总Token={row.total_tokens}"
                     for index, row in enumerate(models, 1))
        return lines

    def _stats(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.decision_replay.recorder import enabled, store
        from src.decision_replay.reply_metrics import statistics

        start, end, note = self._window(request)
        sessions = [row.session_id for row in self._scope(request)]
        if not sessions:
            return ["没有匹配本 Bot 账号的真实聊天流，无法统计。"], note
        result = statistics(store, read_only=True, max_events=MAX_QUERY_EVENTS,
                            start=start.timestamp(), end=end.timestamp())
        # 使用相同事件投影，逐会话过滤事实；不能把全宿主统计冒充指定群统计。
        facts = [row for row in result["records"] if row["session_id"] in sessions]
        counts = {key: sum(key in row["metrics"] for row in facts) for key in result["counts"]}
        more = read_data.replay_counts(store, start.timestamp(), end.timestamp(), sessions)
        def observed(value: int, available: bool) -> str:
            return str(value) + "条已记录观测" if available else "不可统计（无口径记录）"

        available = result["effective_since"] is not None
        lines = [f"触发检查={observed(more['gate_checks'], more['gate_checks'] > 0)}（gate.decided，非入站消息数）",
                 f"Planner 调用={observed(more['planner_requests'], more['planner_requests'] > 0)}（模型尝试，非逻辑轮数）",
                 f"Replyer 生成={observed(counts['generation_requests'], available)}；"
                 f"文本气泡成功数={observed(counts['text_messages'], available)}",
                 f"表情成功数={observed(counts['emoji_messages'], available)}；"
                 f"平台实际成功发送数={observed(counts['platform_messages'], available)}",
                 f"事件采集当前启用={enabled()}；回复统计起点={result['effective_since']}；仅保留事件，不代表生命周期总数。"]
        return lines, note + "；[start,end)；无事件表示无可用观测，不能证明未调用/未发送。"

    def _models(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.config.config import config_manager

        config = config_manager.get_model_config()
        models = {item.name: item for item in config.models}
        lines = []
        for task in type(config.model_task_config).model_fields:
            definition = config.model_task_config.model_dump()[task]
            if not isinstance(definition, dict) or "model_list" not in definition:
                continue
            for name in definition["model_list"]:
                item = models.get(name)
                lines.append(f"任务={task}；模型={name}；供应商={item.api_provider if item else '配置缺失'}；"
                             "状态=仅配置，未执行健康探测")
        return lines, "不输出 API Key、URL 或任何供应商凭据。"

    def _errors(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.decision_replay.recorder import store
        from src.maisaka.monitor.event_store import MAX_MONITOR_EVENT_AGE_HOURS, MAX_MONITOR_EVENT_RECORDS

        start, end, note = self._window(request)
        sessions = [row.session_id for row in self._scope(request)]
        rows = read_data.structured_errors(store, start.timestamp(), end.timestamp(), sessions)
        labels = {"model": "API/LLM 失败（不能进一步区分超时）", "planner": "Planner 异常",
                  "send": "消息发送失败", "replyer": "Replyer 失败", "memory": "记忆异常"}
        lines = [f"{format_time(row['hour'])} {labels.get(row['stage'], '其他结构化错误')}：{row['count']} 条"
                 for row in rows[:500]]
        monitor = read_data.monitor_errors(start.timestamp(), end.timestamp(), sessions)
        lines.extend(f"{format_time(row['hour'])} 独立监控 llm.error：{row['count']} 条（不与回放合计）" for row in monitor[:500])
        lines.append("LLM 超时细分、插件异常：没有完整统一分类来源；不可推断为零。")
        return lines, note + f"；仅保留结构化记录；监控≤{MAX_MONITOR_EVENT_AGE_HOURS}h/{MAX_MONITOR_EVENT_RECORDS}条；不等于完整错误日志。"

    def _replay(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.common.message_repository import find_messages
        from src.decision_replay.recorder import retention_days, store
        from src.maisaka.monitor.event_store import find_message_observations

        start, end, note = self._window(request)
        sessions = [row.session_id for row in self._scope(request)]
        requested_id = str(request.parameters.get("message_id", ""))
        turns = (read_data.message_replay(store, sessions=sessions, start=start.timestamp(), end=end.timestamp(),
                                         message_id=requested_id) if store.path.is_file() else [])
        lines = []
        if requested_id:
            observations = find_message_observations(sessions, requested_id, start=start.timestamp(), end=end.timestamp())
            lines.extend(f"监控 {item['event_type']}：原消息时间={format_time(item['timestamp'])}；"
                         f"正文摘要={safe_text(item['content'])}" for item in observations)
            if not observations:
                lines.append("未找到保留的关联消息监控：可能超期、超10000条、未记录或无匹配 ID。")
        for turn in turns[:10]:
            ids = turn["message_ids"]
            lines.append(f"会话={turn['session_id']}；消息ID={','.join(ids[:4])}；轮次={turn['logical_turn_id']}")
            originals = find_messages(session_id=turn["session_id"], message_id=requested_id or ids[0], limit=1) if ids else []
            if originals:
                original = originals[0]
                lines.append(f"原始消息记录时间={format_time(original.timestamp.timestamp())}；正文摘要={safe_text(original.processed_plain_text)}")
            else:
                lines.append("原始消息或接收时间：缺失；轮次起始时间不能代替消息接收时间。")
            for stage, label in (("gate", "发言判断"), ("planner", "Planner"), ("replyer", "Replyer"), ("send", "平台发送")):
                events = [event for event in turn["events"] if event["stage"] == stage]
                lines.append(label + "：" + ("；".join(f"{safe_text(e['event_type'])}/{safe_text(e['decision'])}"
                                                      f"（记录原因码={safe_text(e['reason_code'])}）"
                                                      for e in events[:6]) if events else "未记录，原因未知"))
            features = [e["detail"].get("features", {}) for e in turn["events"] if e["stage"] == "gate"]
            flags = [f["owner_verified"] for f in features if "owner_verified" in f]
            lines.append(f"门控主人特征记录={flags if flags else '缺失'}；Planner 逐消息身份是否注入：无可核验记录，不推断。")
            if turn["has_more"]:
                lines.append("事件超过100条，本摘要不完整；请在 WebUI 查看完整分页。")
        if len(turns) > 10:
            lines.append("匹配逻辑轮超过10个，仅返回最近10个。")
        if not turns:
            lines.append("未找到关联回放：可能未采集、超期、被清理或无关联字段，无法重建决策原因。")
        return lines, note + f"；查询上限7天；回放保留{retention_days()}天；监控重放不等于完整决策回放。"

    def _persona(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.config.config import global_config
        from .overrides import override_manager

        config = global_config.personality
        snapshot = override_manager.snapshot("", request.actor_identity.bot_account_id)
        return [f"当前基础 personality：{safe_text(config.personality)}",
                f"当前基础 behavior_style：{safe_text(config.behavior_style)}",
                f"当前基础 reply_style：{safe_text(config.reply_style)}",
                f"主人临时预设：persona={snapshot.persona or '继承'}；style={snapshot.style or '继承'}；"
                f"override_ids={snapshot.override_ids}；已在途模型请求保留同轮快照。"], "读取当前基础配置与统一有效覆盖；不代表每个请求的全部最终Prompt。"

    def _memory(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.config.config import global_config
        from src.services.tag_memory_store import TagMemoryStore

        directory = Path(global_config.chat.tag_memory.data_dir)
        if not directory.is_absolute():
            directory = Path(__file__).resolve().parents[3] / directory
        path = directory / "memory_index.db"
        if not path.is_file():
            return ["人物标签数据库不可用；没有生成替代记忆。"], "仅查询已认证主人自身的有效人物标签。"
        store = TagMemoryStore(path)
        lines = []
        keyword = str(request.parameters.get("keyword", ""))
        chats = self._scope(request)
        for chat in chats[:50]:
            tags = store.list_current_tags(chat.session_id, person_users=[request.actor_identity.user_id],
                                           limit=20, read_only=True)
            for item in tags:
                if not keyword or keyword.casefold() in (item["name"] + item["summary"]).casefold():
                    lines.append(f"{chat.group_name or chat.user_nickname or chat.session_id}：ID={item['id']} source={item['source']} created={format_time(item['created_at'])}：{item['summary']}")
        return lines, "仅本账号最近50个真实聊天流、每流最近20个本人有效标签；关键词只在此范围过滤。未知来源标签不会被归类为自主学习、推断或已确认；owner_explicit仅在所属会话检索。全局画像/其他记忆不在本接口范围。"

    def _learning(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        from src.config.config import global_config
        from src.decision_replay.recorder import store

        lines = [f"标签记忆配置启用={global_config.chat.tag_memory.enabled}",
                 f"行为学习配置启用={global_config.experimental.enable_behavior_learning}",
                 f"表达学习规则={len(global_config.expression.learning_list)} 条；"
                 f"其中 learn=True：{sum(item.learn for item in global_config.expression.learning_list)}",
                 f"黑话学习规则={len(global_config.jargon.learning_list)} 条；"
                 f"其中 learn=True：{sum(item.learn for item in global_config.jargon.learning_list)}"]
        sessions = [chat.session_id for chat in self._scope(request)]
        start, end, note = self._window(request)
        if store.path.is_file():
            with closing(store.connect_readonly()) as connection:
                rows = connection.execute(
                    "SELECT timestamp,decision FROM events WHERE event_type='memory.extracted' AND timestamp>=? "
                    "AND timestamp<? AND session_id IN (SELECT value FROM json_each(?)) ORDER BY timestamp DESC LIMIT 20",
                    (start.timestamp(), end.timestamp(), json.dumps(sessions)),
                ).fetchall()
        else:
            rows = []
            lines.append("标签提取活动：回放数据源不可用。")
        lines.extend(f"标签提取 {format_time(row['timestamp'])}：{row['decision']}" for row in rows)
        lines.append("最近其他学习活动：无统一可核实记录；一次任务成功不代表人格永久改变。")
        return lines, note + "；配置启用/规则不等于每个聊天流实际运行。"

    def _chats(self, request: AdminActionRequest) -> Tuple[List[str], str]:
        lines = []
        for chat in self._scope(request):
            name = chat.group_name or chat.user_nickname or (str(chat.user_id) + "的私聊")
            state = "内存已加载" if chat.session_id in request.parameters["runtime_snapshot"]["loaded_chats"] else "仅历史记录"
            runtime = request.parameters["runtime_snapshot"]["chat_runtimes"].get(chat.session_id)
            active = f"Runtime运行={runtime['running']}，Planner状态={runtime['state']}" if runtime else "无当前Runtime"
            lines.append(f"{name}；{chat.platform}；{'群聊' if chat.group_id else '私聊'}；{state}；"
                         f"{active}；"
                         f"目标={chat.group_id or chat.user_id}；session_id={chat.session_id}；"
                         f"最近记录活跃={chat.last_active_timestamp.isoformat(timespec='seconds') if chat.last_active_timestamp else '未知'}")
        return lines, "本 Bot 账号已知聊天流；无账号归属的旧流未纳入；Runtime标记/内存加载不等于群友在线。"
