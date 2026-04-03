"""Trading system agents.

Each agent is a self-contained module that communicates via typed Pydantic models.
"""

from news_trade.agents.execution import ExecutionAgent
from news_trade.agents.market_data import MarketDataAgent
from news_trade.agents.news_ingestor import NewsIngestorAgent
from news_trade.agents.orchestrator import OrchestratorAgent
from news_trade.agents.portfolio_fetcher import PortfolioFetcherAgent
from news_trade.agents.risk_manager import RiskManagerAgent
from news_trade.agents.sentiment_analyst import SentimentAnalystAgent
from news_trade.agents.signal_generator import SignalGeneratorAgent

__all__ = [
    "ExecutionAgent",
    "MarketDataAgent",
    "NewsIngestorAgent",
    "OrchestratorAgent",
    "PortfolioFetcherAgent",
    "RiskManagerAgent",
    "SentimentAnalystAgent",
    "SignalGeneratorAgent",
]
