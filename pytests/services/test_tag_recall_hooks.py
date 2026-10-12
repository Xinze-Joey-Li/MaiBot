"""隔离执行实际 Planner/Replyer 方法，验证标签参考进入本轮模型上下文。"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import ast
import asyncio


def load_method(path, name, globals_dict):
    tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    method = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    exec(compile(module, path, "exec"), globals_dict)
    return globals_dict[name]


class TagRecallHooksTest(IsolatedAsyncioTestCase):
    async def test_actual_planner_injection_preserves_existing_memory_and_adds_tags(self):
        method = load_method("src/maisaka/reasoning_engine.py", "_build_planner_injected_user_messages", {
            "SessionMessage": Any, "asyncio": asyncio, "logger": Mock(),
            "heuristic_memory_injector": SimpleNamespace(build_injection_message=AsyncMock(return_value="原长期记忆")),
            "image_memory_injector": SimpleNamespace(build_injection_message=AsyncMock(return_value="原图片记忆")),
            "build_person_profile_injection_messages": AsyncMock(return_value=["原画像"]),
        })
        recall = AsyncMock(return_value="标签证据参考")
        with patch.dict("sys.modules", {"src.services.tag_memory_recall": SimpleNamespace(build_tag_memory_reference=recall)}):
            result = await method(SimpleNamespace(_runtime=SimpleNamespace(session_id="real-group", log_prefix="群")),
                                  profile_message=object(), source_messages=[], deferred_tools_reminder="工具提示")
        self.assertEqual(result, ["工具提示", "原长期记忆", "原画像", "原图片记忆", "标签证据参考"])
        recall.assert_awaited_once_with("real-group")

    async def test_actual_replyer_final_message_carries_tag_evidence_and_existing_requirements(self):
        method = load_method("src/chat/replyer/maisaka_generator_base.py", "_build_final_user_message", {
            "datetime": datetime, "List": List, "Dict": Dict, "Any": Any, "Optional": Optional,
            "SessionMessage": Any, "LLMContextMessage": Any, "DUPLICATE_TARGET_REPLY_REMINDER_ARG": "duplicate",
        })
        replyer = SimpleNamespace(_build_target_message_block=Mock(return_value="回复对象"),
                                  _build_reply_attachment_prompt=Mock(return_value=""),
                                  _build_reply_instruction=Mock(return_value="请自然回复"))
        result = method(replyer, [], None, reply_requirements="原回复要求\n标签证据参考")
        self.assertIn("原回复要求\n标签证据参考", result)
        self.assertIn("回复对象", result)
        self.assertIn("请自然回复", result)
