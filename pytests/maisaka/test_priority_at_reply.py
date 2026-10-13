"""@ 独立回复的分支、并发、去重和失败暴露验证。"""

from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import asyncio

from src.common.data_models.message_component_data_model import AtComponent, ImageComponent, MessageSequence, TextComponent
from src.services.priority_at_reply_service import PriorityAtReplyService


class PriorityAtReplyTest(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = PriorityAtReplyService()
        self.config = SimpleNamespace(chat=SimpleNamespace(
            priority_at_reply_enabled=True, priority_at_reply_emoji_hashes=[]
        ))
        self.detect = Mock(return_value=(True, True, 0.0))
        self.logger = Mock()
        self.modules = {
            "src.chat.utils.utils": SimpleNamespace(is_mentioned_bot_in_message=self.detect),
            "src.config.config": SimpleNamespace(global_config=self.config),
            "src.services.bot_account_service": SimpleNamespace(is_bot_self=lambda platform, user: user == "bot"),
            "src.common.logger": SimpleNamespace(get_logger=lambda name: self.logger),
        }
        self.runtime = SimpleNamespace(_chat_history=["context"], chat_stream=object())

    def message(self, message_id="1", session_id="group", user="user"):
        return SimpleNamespace(
            session_id=session_id, message_id=message_id, platform="qq", is_at=False, is_mentioned=False,
            is_notify=False, is_command=False,
            raw_message=MessageSequence([AtComponent("bot")]),
            message_info=SimpleNamespace(additional_config={}, user_info=SimpleNamespace(user_id=user)),
        )

    async def test_at_claim_ignores_old_frequency_and_at_switch(self) -> None:
        self.config.chat.reply_timing = SimpleNamespace(talk_value=0, inevitable_at_reply=False)
        message = self.message()
        with patch.dict("sys.modules", self.modules):
            self.assertTrue(self.service.claim(message))
        self.assertTrue(message.is_at)
        self.assertTrue(message.message_info.additional_config["priority_at_handled"])

    async def test_name_mention_without_at_is_not_claimed(self) -> None:
        self.detect.return_value = (True, False, 0.0)
        with patch.dict("sys.modules", self.modules):
            self.assertFalse(self.service.claim(self.message()))

    async def test_disabled_self_command_and_notice_are_not_claimed(self) -> None:
        with patch.dict("sys.modules", self.modules):
            self.assertFalse(self.service.claim(self.message(user="bot")))
            message = self.message()
            message.is_command = True
            self.assertFalse(self.service.claim(message))
            message.is_command = False
            message.is_notify = True
            self.assertFalse(self.service.claim(message))
            self.config.chat.priority_at_reply_enabled = False
            self.assertFalse(self.service.claim(self.message()))

    async def test_half_probability_boundary(self) -> None:
        emoji = AsyncMock()
        text = AsyncMock()
        with patch.dict("sys.modules", self.modules), patch.object(
            self.service, "_send_emoji", emoji
        ), patch.object(self.service, "_send_text", text):
            for value in (0.0, 0.499999):
                with patch("src.services.priority_at_reply_service.random.random", return_value=value):
                    await self.service._reply(self.message(), self.runtime, [])
            for value in (0.5, 0.999999):
                with patch("src.services.priority_at_reply_service.random.random", return_value=value):
                    await self.service._reply(self.message(), self.runtime, [])
        self.assertEqual(emoji.await_count, 2)
        self.assertEqual(text.await_count, 2)

    async def test_only_single_bot_at_and_whitespace_can_use_emoji_branch(self) -> None:
        message = self.message()
        cases = [
            ([AtComponent("bot"), TextComponent(" \n")], True),
            ([AtComponent("bot"), TextComponent("在吗")], False),
            ([TextComponent("在吗"), AtComponent("bot")], False),
            ([AtComponent("bot"), AtComponent("other")], False),
            ([AtComponent("bot"), AtComponent("bot")], False),
            ([AtComponent("bot"), ImageComponent(binary_hash="image")], False),
            ([TextComponent("@bot")], False),
            ([], False),
        ]
        with patch.dict("sys.modules", self.modules):
            for components, expected in cases:
                with self.subTest(expected=expected, components=components):
                    message.raw_message = MessageSequence(components)
                    self.assertEqual(self.service._is_standalone_bot_at(message), expected)

    async def test_at_with_text_skips_random_and_uses_text_reply(self) -> None:
        message = self.message()
        message.raw_message = MessageSequence([AtComponent("bot"), TextComponent("解释一下")])
        emoji, text = AsyncMock(), AsyncMock()
        with patch.dict("sys.modules", self.modules), patch.object(
            self.service, "_send_emoji", emoji
        ), patch.object(self.service, "_send_text", text), patch(
            "src.services.priority_at_reply_service.random.random", side_effect=AssertionError("不应抽签")
        ):
            await self.service._reply(message, self.runtime, [])
        emoji.assert_not_awaited()
        text.assert_awaited_once_with(message, self.runtime, [])

    async def test_new_at_does_not_wait_for_previous_at(self) -> None:
        started = []
        release = asyncio.Event()

        async def reply(message, runtime, history):
            started.append(message.message_id)
            await release.wait()

        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message("first"), self.runtime)
            self.service.start_reply(self.message("second"), self.runtime)
            await asyncio.sleep(0)
            self.assertEqual(started, ["first", "second"])
            release.set()
            await asyncio.gather(*self.service._tasks.values())

    async def test_redelivery_is_deduplicated_during_and_after_send(self) -> None:
        reply = AsyncMock()
        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message(), self.runtime)
            self.service.start_reply(self.message(), self.runtime)
            await asyncio.gather(*self.service._tasks.values())
            self.service.start_reply(self.message(), self.runtime)
        reply.assert_awaited_once()

    async def test_deduplication_is_scoped_to_session(self) -> None:
        reply = AsyncMock()
        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message(session_id="a"), self.runtime)
            self.service.start_reply(self.message(session_id="b"), self.runtime)
            await asyncio.gather(*self.service._tasks.values())
        self.assertEqual(reply.await_count, 2)

    async def test_history_is_snapshotted(self) -> None:
        reply = AsyncMock()
        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message(), self.runtime)
            self.runtime._chat_history.append("later")
            await asyncio.gather(*self.service._tasks.values())
        self.assertEqual(reply.call_args.args[2], ["context"])

    async def test_failure_is_logged_without_switching_branch(self) -> None:
        reply = AsyncMock(side_effect=RuntimeError("send failed"))
        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message(), self.runtime)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.logger.error.assert_called_once()
        self.assertFalse(self.service._tasks)

    async def test_shutdown_cancels_inflight_tasks(self) -> None:
        async def reply(message, runtime, history):
            await asyncio.Event().wait()

        with patch.dict("sys.modules", self.modules), patch.object(self.service, "_reply", reply):
            self.service.start_reply(self.message(), self.runtime)
            await asyncio.sleep(0)
            tasks = list(self.service._tasks.values())
            await self.service.shutdown()
        self.assertTrue(tasks[0].cancelled())
        self.assertFalse(self.service._tasks)

    def emoji_modules(self, lookup):
        return {
            "src.config.config": SimpleNamespace(global_config=self.config),
            "src.common.data_models.message_component_data_model": SimpleNamespace(
                EmojiComponent=lambda **kwargs: kwargs, MessageSequence=list
            ),
            "src.common.utils.image_path": SimpleNamespace(
                resolve_stored_image_path=lambda path: SimpleNamespace(read_bytes=lambda: b"emoji")
            ),
            "src.emoji_system.emoji_manager": SimpleNamespace(
                emoji_manager=SimpleNamespace(get_emoji_by_hash=lookup)
            ),
            "src.services.send_service": SimpleNamespace(send_priority_at_reply=AsyncMock(return_value=True)),
        }

    async def test_empty_fixed_emoji_list_fails_explicitly(self) -> None:
        lookup = Mock()
        with patch.dict("sys.modules", self.emoji_modules(lookup)):
            with self.assertRaisesRegex(RuntimeError, "尚未配置"):
                await self.service._send_emoji(self.message())
        lookup.assert_not_called()

    async def test_emoji_branch_randomly_selects_only_distinct_configured_candidates(self) -> None:
        self.config.chat.priority_at_reply_emoji_hashes = ["first", "second", " first "]
        first = SimpleNamespace(file_hash="first", full_path="a")
        second = SimpleNamespace(file_hash="second", full_path="b")
        lookup = Mock(side_effect=lambda value: {"first": first, "second": second}[value])
        modules = self.emoji_modules(lookup)
        target = self.message()
        with patch.dict("sys.modules", modules), patch(
            "src.services.priority_at_reply_service.random.choice", return_value=second
        ) as choose:
            await self.service._send_emoji(target)
        choose.assert_called_once_with([first, second])
        self.assertEqual(lookup.call_count, 2)
        modules["src.services.send_service"].send_priority_at_reply.assert_awaited_once_with(
            [{"binary_hash": "second", "binary_data": b"emoji"}], target, set_reply=False
        )

    async def test_invalid_fixed_emoji_fails_without_using_other_emojis(self) -> None:
        self.config.chat.priority_at_reply_emoji_hashes = ["missing"]
        lookup = Mock(return_value=None)
        modules = self.emoji_modules(lookup)
        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                await self.service._send_emoji(self.message())
        modules["src.services.send_service"].send_priority_at_reply.assert_not_awaited()

    async def test_model_uses_existing_replyer_and_target(self) -> None:
        result = SimpleNamespace(completion=SimpleNamespace(response_text="回复内容"), error_message="", selected_emoji_tag="")
        generator = Mock()
        generator.generate_reply_with_context = AsyncMock(return_value=(True, result))
        factory = Mock(return_value=generator)
        sender = AsyncMock(return_value=True)
        modules = {
            "src.chat.replyer.maisaka_generator": SimpleNamespace(MaisakaReplyGenerator=factory),
            "src.common.data_models.message_component_data_model": SimpleNamespace(
                MessageSequence=list, TextComponent=lambda text: text
            ),
            "src.services.send_service": SimpleNamespace(send_priority_at_reply=sender),
        }
        target = self.message()
        with patch.dict("sys.modules", modules):
            await self.service._send_text(target, self.runtime, ["context"])
        factory.assert_called_once_with(chat_stream=self.runtime.chat_stream, request_type="priority_at.replyer")
        self.assertIs(generator.generate_reply_with_context.call_args.kwargs["reply_message"], target)
        sender.assert_awaited_once_with(["回复内容"], target)

    async def test_empty_model_response_does_not_silently_send(self) -> None:
        result = SimpleNamespace(completion=SimpleNamespace(response_text=""), error_message="empty")
        generator = SimpleNamespace(generate_reply_with_context=AsyncMock(return_value=(False, result)))
        sender = AsyncMock()
        modules = {
            "src.chat.replyer.maisaka_generator": SimpleNamespace(MaisakaReplyGenerator=lambda **kwargs: generator),
            "src.common.data_models.message_component_data_model": SimpleNamespace(
                MessageSequence=list, TextComponent=str
            ),
            "src.services.send_service": SimpleNamespace(send_priority_at_reply=sender),
        }
        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(RuntimeError, "生成失败"):
                await self.service._send_text(self.message(), self.runtime, [])
        sender.assert_not_awaited()
