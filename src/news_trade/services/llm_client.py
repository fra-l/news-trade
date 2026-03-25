"""LLM client abstraction — provider-agnostic interface for LLM calls.

Defines the ``LLMClient`` Protocol and ``LLMClientFactory`` for routing calls to
the appropriate model tier (quick / deep).  Only ``AnthropicLLMClient`` is
implemented now; adding other providers requires one new class and one new
``case`` in ``_build_client`` — no agent changes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

import anthropic
from pydantic import BaseModel, Field

from news_trade.config import Settings

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class LLMResponse(BaseModel):
    """Structured response from any LLMClient implementation."""

    content: str
    model_id: str
    provider: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Single call-site abstraction.  All providers implement this interface."""

    async def invoke(
        self,
        prompt: str,
        system: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """Send *prompt* to the model and return a structured response.

        When *response_schema* is provided the implementation must return
        valid JSON in ``content`` that can be parsed into that schema.
        """
        ...

    @property
    def model_id(self) -> str:
        """Exact model string used — e.g. ``'claude-sonnet-4-6'``."""
        ...

    @property
    def provider(self) -> str:
        """Provider name — e.g. ``'anthropic'``."""
        ...


# ---------------------------------------------------------------------------
# Anthropic concrete implementation
# ---------------------------------------------------------------------------


def _schema_to_tool(schema: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model class to an Anthropic tool definition."""
    return {
        "name": schema.__name__,
        "description": f"Return a {schema.__name__} object.",
        "input_schema": schema.model_json_schema(),
    }


class AnthropicLLMClient:
    """Satisfies the ``LLMClient`` protocol using the Anthropic async API."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._client = anthropic.AsyncAnthropic()

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "anthropic"

    async def invoke(
        self,
        prompt: str,
        system: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        if response_schema is not None:
            tool = _schema_to_tool(response_schema)
            kwargs["tools"] = [tool]
            kwargs["tool_choice"] = {"type": "tool", "name": response_schema.__name__}

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            _logger.error("Anthropic API error (%s): %s", self._model, exc)
            raise

        usage = response.usage
        content = _extract_content(response, response_schema)

        return LLMResponse(
            content=content,
            model_id=self._model,
            provider="anthropic",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )


def _extract_content(
    response: anthropic.types.Message,
    response_schema: type[BaseModel] | None,
) -> str:
    """Pull text or tool-use JSON out of the Anthropic response object."""
    if response_schema is not None:
        for block in response.content:
            if block.type == "tool_use":
                return json.dumps(block.input)
        # Fallback: return empty JSON object so callers get a parseable string
        _logger.warning("No tool_use block found in structured response")
        return "{}"

    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_client(provider: str, model: str) -> LLMClient:
    match provider:
        case "anthropic":
            return AnthropicLLMClient(model)
        case _:
            raise ValueError(
                f"Unsupported LLM provider: '{provider}'. "
                "Add a concrete implementation and a case in _build_client."
            )


class LLMClientFactory:
    """Instantiates and vends quick/deep LLM clients from application settings."""

    def __init__(self, settings: Settings) -> None:
        self._quick = _build_client(settings.llm_provider, settings.llm_quick_model)
        self._deep = _build_client(settings.llm_provider, settings.llm_deep_model)

    @property
    def quick(self) -> LLMClient:
        """Cheap, fast model — classification, extraction, debate rounds."""
        return self._quick

    @property
    def deep(self) -> LLMClient:
        """Accurate model — confidence scoring, signal synthesis, debate verdict."""
        return self._deep
