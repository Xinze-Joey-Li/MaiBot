"""验证 Planner reply 定义实际发往 SDK，流式及非流式调用都保留为 FunctionCallItem。"""

from types import SimpleNamespace

import pytest

from src.config.model_configs import APIProvider, ModelInfo, ReasoningParseMode, ToolArgumentParseMode
from src.llm_models.model_client.base_client import ResponseRequest
from src.llm_models.model_client.openai_client import OpenaiClient
from src.llm_models.payload_content.context_item import ContextItemBuilder, FunctionCallItem
from src.llm_models.payload_content.tool_option import normalize_tool_options
from src.maisaka.builtin_tool.reply import get_tool_spec
from src.maisaka.chat_loop_service import ChatResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_reply_definition_and_native_call_survive_sdk_roundtrip(stream):
    client = object.__new__(OpenaiClient)
    client.api_provider = APIProvider(
        name="test", base_url="https://example.com/v1", auth_type="none", client_type="openai"
    )
    client.reasoning_parse_mode = ReasoningParseMode.NONE
    client.tool_argument_parse_mode = ToolArgumentParseMode.STRICT
    client.reasoning_key = "reasoning_content"
    client._omit_sdk_authorization = True
    sent = []
    tool = SimpleNamespace(
        index=0,
        id="native-call",
        type="function",
        function=SimpleNamespace(name="reply", arguments='{"msg_id":"real-target"}'),
    )

    async def create(**kwargs):
        sent.append(kwargs)
        if kwargs["stream"]:

            async def events():
                yield SimpleNamespace(
                    model="test-model",
                    usage=None,
                    choices=[
                        SimpleNamespace(finish_reason=None, delta=SimpleNamespace(content=None, tool_calls=[tool]))
                    ],
                )
                yield SimpleNamespace(
                    model="test-model",
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason="tool_calls", delta=SimpleNamespace(content=None, tool_calls=None)
                        )
                    ],
                )

            return events()
        return SimpleNamespace(
            model="test-model",
            usage=None,
            choices=[
                SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[tool]))
            ],
        )

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    request = ResponseRequest(
        model_info=ModelInfo(
            name="test-model", model_identifier="test-model", api_provider="test", force_stream_mode=stream
        ),
        context_items=[ContextItemBuilder().add_text_content('msg_id="real-target": 1+1等于几').build()],
        tool_options=normalize_tool_options([get_tool_spec().to_llm_definition()]),
    )
    result = await client.get_response(request)
    assert len(sent) == 1
    assert sent[0]["tools"][0]["function"]["name"] == "reply"
    assert "msg_id" in sent[0]["tools"][0]["function"]["parameters"]["properties"]
    assert any(isinstance(item, FunctionCallItem) for item in result.output_items)
    response = ChatResponse(
        output_items=result.output_items,
        request_messages=request.context_items,
        selected_history_count=1,
        tool_count=1,
        prompt_tokens=0,
        built_message_count=1,
        completion_tokens=0,
        total_tokens=0,
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].call_id == "native-call"
    assert response.tool_calls[0].func_name == "reply"
    assert response.tool_calls[0].args == {"msg_id": "real-target"}
