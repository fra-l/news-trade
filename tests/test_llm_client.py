"""Unit tests for LLMClient protocol, AnthropicLLMClient, and LLMClientFactory."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from news_trade.config import Settings
from news_trade.services.llm_client import (
    AnthropicLLMClient,
    LLMClient,
    LLMClientFactory,
    LLMResponse,
    _build_client,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs) -> Settings:
    defaults = dict(
        anthropic_api_key="test-key",
        alpaca_api_key="test",
        alpaca_secret_key="test",
    )
    return Settings(**(defaults | kwargs))


def _make_anthropic_response(
    text: str = "hello", input_tokens: int = 10, output_tokens: int = 5
):
    """Build a minimal fake Anthropic Message object."""
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=[block], usage=usage)


def _make_tool_use_response(data: dict, input_tokens: int = 10, output_tokens: int = 5):
    """Build a fake Anthropic Message with a tool_use block."""
    block = SimpleNamespace(type="tool_use", input=data)
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=[block], usage=usage)


# ---------------------------------------------------------------------------
# LLMResponse model
# ---------------------------------------------------------------------------


class TestLLMResponse:
    def test_happy_path(self):
        r = LLMResponse(
            content="hi", model_id="claude-haiku-4-5-20251001", provider="anthropic"
        )
        assert r.content == "hi"
        assert r.model_id == "claude-haiku-4-5-20251001"
        assert r.provider == "anthropic"
        assert r.input_tokens == 0
        assert r.output_tokens == 0

    def test_with_usage(self):
        r = LLMResponse(
            content="x", model_id="m", provider="p", input_tokens=100, output_tokens=50
        )
        assert r.input_tokens == 100
        assert r.output_tokens == 50

    def test_serialization_round_trip(self):
        r = LLMResponse(
            content="abc", model_id="m", provider="p", input_tokens=1, output_tokens=2
        )
        assert LLMResponse.model_validate(r.model_dump()) == r


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_anthropic_client_satisfies_llm_client_protocol(self):
        client = AnthropicLLMClient(model="claude-haiku-4-5-20251001")
        assert isinstance(client, LLMClient)

    def test_model_id_property(self):
        client = AnthropicLLMClient(model="claude-sonnet-4-6")
        assert client.model_id == "claude-sonnet-4-6"

    def test_provider_property(self):
        client = AnthropicLLMClient(model="claude-haiku-4-5-20251001")
        assert client.provider == "anthropic"


# ---------------------------------------------------------------------------
# AnthropicLLMClient.invoke — plain text path
# ---------------------------------------------------------------------------


class TestAnthropicLLMClientInvoke:
    @pytest.fixture()
    def client(self):
        return AnthropicLLMClient(model="claude-haiku-4-5-20251001")

    @pytest.mark.asyncio
    async def test_plain_invoke_returns_llm_response(self, client):
        fake_response = _make_anthropic_response(
            "test output", input_tokens=20, output_tokens=8
        )
        with patch.object(
            client._client.messages, "create", new=AsyncMock(return_value=fake_response)
        ):
            result = await client.invoke("Hello")

        assert isinstance(result, LLMResponse)
        assert result.content == "test output"
        assert result.model_id == "claude-haiku-4-5-20251001"
        assert result.provider == "anthropic"
        assert result.input_tokens == 20
        assert result.output_tokens == 8

    @pytest.mark.asyncio
    async def test_system_prompt_passed_through(self, client):
        fake_response = _make_anthropic_response("ok")
        mock_create = AsyncMock(return_value=fake_response)
        with patch.object(client._client.messages, "create", new=mock_create):
            await client.invoke("prompt", system="You are helpful.")

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["system"] == "You are helpful."

    @pytest.mark.asyncio
    async def test_no_system_prompt_not_passed(self, client):
        fake_response = _make_anthropic_response("ok")
        mock_create = AsyncMock(return_value=fake_response)
        with patch.object(client._client.messages, "create", new=mock_create):
            await client.invoke("prompt")

        call_kwargs = mock_create.call_args.kwargs
        assert "system" not in call_kwargs

    @pytest.mark.asyncio
    async def test_structured_output_uses_tool_use(self, client):
        class MySchema(BaseModel):
            value: int

        fake_response = _make_tool_use_response({"value": 42})
        mock_create = AsyncMock(return_value=fake_response)
        with patch.object(client._client.messages, "create", new=mock_create):
            result = await client.invoke("prompt", response_schema=MySchema)

        assert '"value": 42' in result.content
        call_kwargs = mock_create.call_args.kwargs
        assert "tools" in call_kwargs
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "MySchema"}

    @pytest.mark.asyncio
    async def test_api_error_propagates(self, client):
        import anthropic

        mock_create = AsyncMock(
            side_effect=anthropic.APIConnectionError(request=MagicMock())
        )
        with (
            patch.object(client._client.messages, "create", new=mock_create),
            pytest.raises(anthropic.APIConnectionError),
        ):
            await client.invoke("prompt")


# ---------------------------------------------------------------------------
# _build_client
# ---------------------------------------------------------------------------


class TestBuildClient:
    def test_anthropic_returns_anthropic_client(self):
        client = _build_client("anthropic", "claude-haiku-4-5-20251001")
        assert isinstance(client, AnthropicLLMClient)
        assert client.model_id == "claude-haiku-4-5-20251001"

    def test_unsupported_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            _build_client("openai", "gpt-4o")


# ---------------------------------------------------------------------------
# LLMClientFactory
# ---------------------------------------------------------------------------


class TestLLMClientFactory:
    def test_quick_model_id_matches_config(self):
        s = _make_settings(llm_quick_model="claude-haiku-4-5-20251001")
        factory = LLMClientFactory(s)
        assert factory.quick.model_id == "claude-haiku-4-5-20251001"

    def test_deep_model_id_matches_config(self):
        s = _make_settings(llm_deep_model="claude-sonnet-4-6")
        factory = LLMClientFactory(s)
        assert factory.deep.model_id == "claude-sonnet-4-6"

    def test_quick_satisfies_protocol(self):
        s = _make_settings()
        factory = LLMClientFactory(s)
        assert isinstance(factory.quick, LLMClient)

    def test_deep_satisfies_protocol(self):
        s = _make_settings()
        factory = LLMClientFactory(s)
        assert isinstance(factory.deep, LLMClient)

    def test_unsupported_provider_raises(self):
        s = _make_settings(llm_provider="gemini")
        with pytest.raises(ValueError, match="Unsupported LLM provider"):
            LLMClientFactory(s)

    def test_quick_and_deep_are_separate_instances(self):
        s = _make_settings(
            llm_quick_model="claude-haiku-4-5-20251001",
            llm_deep_model="claude-sonnet-4-6",
        )
        factory = LLMClientFactory(s)
        assert factory.quick is not factory.deep
        assert factory.quick.model_id != factory.deep.model_id
