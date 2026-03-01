"""Base class for all agents."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from news_trade.config import Settings
    from news_trade.services.event_bus import EventBus


class BaseAgent(ABC):
    """Common interface shared by every agent in the system.

    Subclasses implement ``run`` with agent-specific logic. The base class
    provides a logger and references to shared infrastructure.
    """

    def __init__(self, settings: Settings, event_bus: EventBus) -> None:
        self.settings = settings
        self.event_bus = event_bus
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def run(self, state: dict) -> dict:
        """Execute the agent's main logic.

        Args:
            state: The current LangGraph PipelineState dict.

        Returns:
            A dict of state keys to update.
        """
        ...
