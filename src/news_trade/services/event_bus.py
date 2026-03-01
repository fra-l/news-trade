"""Redis-backed event bus for inter-agent messaging."""

from __future__ import annotations

from typing import Any

import redis

from news_trade.config import Settings


class EventBus:
    """Thin wrapper around Redis pub/sub for agent communication.

    Agents publish typed Pydantic model instances to named channels.
    Subscribers receive deserialized model instances.
    """

    def __init__(self, settings: Settings) -> None:
        self._client = redis.from_url(settings.redis_url, decode_responses=True)
        self._pubsub = self._client.pubsub()

    def publish(self, channel: str, payload: str) -> int:
        """Publish a JSON-serialized Pydantic model to a channel.

        Args:
            channel: Redis channel name (e.g. 'news_events', 'signals').
            payload: JSON string from ``model.model_dump_json()``.

        Returns:
            Number of subscribers that received the message.
        """
        return self._client.publish(channel, payload)

    def subscribe(self, *channels: str) -> None:
        """Subscribe to one or more channels.

        Args:
            channels: Channel names to listen on.
        """
        self._pubsub.subscribe(*channels)

    def listen(self) -> Any:
        """Yield messages from subscribed channels.

        Yields:
            Redis message dicts with 'channel' and 'data' keys.
        """
        return self._pubsub.listen()

    def close(self) -> None:
        """Close the Redis connection."""
        self._pubsub.close()
        self._client.close()
