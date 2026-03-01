"""Redis-backed event bus for inter-agent messaging."""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis
from pydantic import BaseModel


class RedisBus:
    """Thin async wrapper around Redis Pub/Sub for agent communication.

    Agents publish typed Pydantic messages to named channels.
    Other agents subscribe and receive deserialized model instances.
    """

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        """Open the Redis connection."""
        self._redis = redis.from_url(self._url, decode_responses=True)

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis:
            await self._redis.aclose()

    async def publish(self, channel: str, message: BaseModel) -> None:
        """Publish a Pydantic model to a channel as JSON."""
        assert self._redis is not None, "Call connect() first"
        await self._redis.publish(channel, message.model_dump_json())

    async def subscribe(self, *channels: str) -> redis.client.PubSub:
        """Return a PubSub instance subscribed to the given channels."""
        assert self._redis is not None, "Call connect() first"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        return pubsub
