"""Async Redis-backed event bus for inter-agent messaging."""

from __future__ import annotations

import redis.asyncio as aioredis
from pydantic import BaseModel

from news_trade.config import Settings


class EventBus:
    """Async wrapper around Redis Pub/Sub for agent communication.

    Agents publish typed Pydantic model instances to named channels.
    Subscribers receive deserialized model instances.
    """

    def __init__(self, settings: Settings) -> None:
        self._url = settings.redis_url
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Open the async Redis connection."""
        self._redis = aioredis.from_url(self._url, decode_responses=True)

    async def publish(self, channel: str, message: BaseModel) -> None:
        """Publish a Pydantic model to a channel as JSON.

        Args:
            channel: Redis channel name (e.g. ``'news_events'``, ``'signals'``).
            message: Any Pydantic model instance.
        """
        assert self._redis is not None, "Call connect() first"
        await self._redis.publish(channel, message.model_dump_json())

    async def subscribe(self, *channels: str) -> aioredis.client.PubSub:
        """Return a PubSub instance subscribed to the given channels.

        Args:
            channels: Channel names to listen on.
        """
        assert self._redis is not None, "Call connect() first"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        return pubsub

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis:
            await self._redis.aclose()
