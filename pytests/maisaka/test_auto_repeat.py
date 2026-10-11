"""自动复读的触发边界与会话隔离验证。"""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from src.services.auto_repeat_service import AutoRepeatDetector, AutoRepeatService


class AutoRepeatTest(TestCase):
    def setUp(self) -> None:
        self.detector = AutoRepeatDetector()
        self.sequence = 0

    def observe(self, text="复读", user="a", session="group", message_id=None):
        self.sequence += 1
        return self.detector.observe(
            session_id=session, message_id=message_id or str(self.sequence), user_id=user, text=text
        )

    def test_four_messages_from_multiple_users_trigger_once(self) -> None:
        for user in ("a", "b", "a"):
            self.assertFalse(self.observe(user=user))
        self.assertTrue(self.observe(user="b"))
        for _ in range(10):
            self.assertFalse(self.observe(user="c"))

    def test_one_user_spam_does_not_trigger(self) -> None:
        for _ in range(10):
            self.assertFalse(self.observe())

    def test_different_text_starts_new_run(self) -> None:
        for user in ("a", "b", "a"):
            self.assertFalse(self.observe(user=user))
        self.assertFalse(self.observe(text="另一个话题"))
        for user in ("a", "b", "a"):
            self.assertFalse(self.observe(user=user))
        self.assertTrue(self.observe(user="b"))

    def test_nontext_breaks_run(self) -> None:
        for text in (None, "", "  "):
            self.detector.clear()
            for user in ("a", "b", "a"):
                self.assertFalse(self.observe(user=user))
            self.assertFalse(self.observe(text=text))
            self.assertFalse(self.observe(user="b"))

    def test_sessions_are_isolated(self) -> None:
        for _ in range(3):
            self.assertFalse(self.observe(session="group-a", user="a"))
            self.assertFalse(self.observe(session="group-b", user="b"))
        self.assertTrue(self.observe(session="group-a", user="b"))
        self.assertFalse(self.observe(session="group-b", user="b"))

    def test_redelivery_is_not_counted_and_does_not_break_run(self) -> None:
        self.assertFalse(self.observe(message_id="first"))
        self.assertFalse(self.observe(user="b"))
        self.assertFalse(self.observe(user="a"))
        self.assertFalse(self.observe(text="旧消息重投", message_id="first"))
        self.assertTrue(self.observe(user="b"))

    def test_clear_discards_previous_run(self) -> None:
        for user in ("a", "b", "a"):
            self.assertFalse(self.observe(user=user))
        self.detector.clear()
        self.assertFalse(self.observe(user="b"))


class AutoRepeatServiceTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = AutoRepeatService()
        self.config = SimpleNamespace(chat=SimpleNamespace(auto_repeat_enabled=True))
        self.send = AsyncMock(return_value=True)
        self.text_type = type("TextComponent", (), {"__init__": lambda item, text: setattr(item, "text", text)})
        self.modules = {
            "src.common.data_models.message_component_data_model": SimpleNamespace(TextComponent=self.text_type),
            "src.config.config": SimpleNamespace(global_config=self.config),
            "src.services.bot_account_service": SimpleNamespace(is_bot_self=lambda platform, user: user == "bot"),
            "src.services.send_service": SimpleNamespace(auto_repeat_to_stream=self.send),
        }
        self.sequence = 0

    def message(self, *, user="a", group=True, components=None):
        self.sequence += 1
        return SimpleNamespace(
            message_id=str(self.sequence), session_id="group", platform="qq",
            message_info=SimpleNamespace(group_info=object() if group else None, user_info=SimpleNamespace(user_id=user)),
            raw_message=SimpleNamespace(components=components if components is not None else [self.text_type("原文")]),
            is_command=False, is_notify=False,
        )

    async def test_sends_original_text_once(self) -> None:
        with patch.dict("sys.modules", self.modules):
            for user in ("a", "b", "a", "b", "a"):
                await self.service.on_incoming_message(self.message(user=user))
        self.send.assert_awaited_once_with("原文", "group")

    async def test_private_self_and_disabled_are_ignored(self) -> None:
        with patch.dict("sys.modules", self.modules):
            for _ in range(5):
                await self.service.on_incoming_message(self.message(group=False))
                await self.service.on_incoming_message(self.message(user="bot"))
            self.config.chat.auto_repeat_enabled = False
            for user in ("a", "b", "a", "b"):
                await self.service.on_incoming_message(self.message(user=user))
        self.send.assert_not_awaited()

    async def test_media_breaks_text_repeat(self) -> None:
        with patch.dict("sys.modules", self.modules):
            for user in ("a", "b", "a"):
                await self.service.on_incoming_message(self.message(user=user))
            await self.service.on_incoming_message(self.message(components=[object()]))
            await self.service.on_incoming_message(self.message(user="b"))
        self.send.assert_not_awaited()

    async def test_send_failure_is_exposed(self) -> None:
        self.send.return_value = False
        with patch.dict("sys.modules", self.modules):
            for user in ("a", "b", "a"):
                await self.service.on_incoming_message(self.message(user=user))
            with self.assertRaises(RuntimeError):
                await self.service.on_incoming_message(self.message(user="b"))
