"""Trading system agents.

Each agent is a self-contained module that communicates via typed Pydantic models.
"""

from news_trade.agents.news_ingestor import NewsIngestorAgent
from news_trade.agents.market_data import MarketDataAgent
from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
from news_trade.agents.signal_generator import SignalGeneratorAgent
from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.orchestrator import OrchestratorAgent

__all__ = [
    "NewsIngestorAgent",
    "MarketDataAgent",
    "SentimentAnalystAgent",
    "SignalGeneratorAgent",
    "RiskManagerAgent",
    "ExecutionAgent",
    "OrchestratorAgent",
]
