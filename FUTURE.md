# FUTURE.md — Research Notes & Enhancement Ideas

This file captures findings from architectural research and discussions.
Items here are **not planned work** — they are reference notes for future decision-making.

---

## News Ingestion Latency: Polling vs WebSocket

### Current Setup

The pipeline polls news every 30 seconds (`NEWS_POLL_INTERVAL_SEC`, default `30`).
For high-impact events (EARN_BEAT, M&A, FDA approval), prices can move 5–15% in the
first 10 seconds after publication — meaning the 30s gap represents real missed opportunity.

### WebSocket: Pros

- **Latency**: Push delivery within ~1s of publication vs up to 30s with polling.
- **Efficiency**: No wasted API calls on empty polls.
- **Simpler deduplication**: Each event is delivered exactly once; the current `event_id`
  dedup in `NewsIngestorAgent` against `NewsEventRow` exists largely because polling
  re-returns the same articles.

### WebSocket: Cons — especially for burst traffic

- **Pipeline is synchronous and expensive**: The current LangGraph pipeline is designed
  for one batch per cycle. During earnings season, 10 news items can arrive in 2 seconds.
  Running one pipeline per event risks concurrent LLM chains, Anthropic rate limits, and
  simultaneous Alpaca submissions that haven't seen each other's fills.
- **Back-pressure is unhandled**: If a pipeline cycle takes 8s (LLM + debate rounds) and
  5 more events arrive, the queue grows and stale signals get processed after prices have
  already corrected — worse than not trading.
- **Risk state consistency**: `RiskManagerAgent` deduplicates within a batch. With
  per-event pipelines, two simultaneous AAPL signals can both pass the concentration check
  before either order fills — silently double-sizing a position.
- **Reconnection complexity**: WebSocket connections drop. A gap-fill REST fallback is
  needed for events missed during downtime. The current poller handles this for free.
- **Provider support**: RSS (the default) has no WebSocket API.

### Recommended Hybrid Architecture

1. **WebSocket ingestion** — accumulate events into an `asyncio.Queue` as they arrive.
2. **Windowed batching** — drain the queue every 3–5s, OR when it reaches N events,
   whichever comes first. Bounds latency without losing batch-consistency guarantees.
3. **Pipeline unchanged** — `NewsIngestorAgent.run()` reads from the pre-filled queue
   instead of calling the provider live. LangGraph graph, risk checks, and dedup logic
   are unaffected.

This reduces latency from ~30s to ~3–5s with minimal architectural risk.

---

## Finnhub Free Tier: WebSocket News Availability

**Bottom line: real-time news via WebSocket is not available on the free tier.**

| Feature | Free Tier | Paid / Enterprise |
|---|---|---|
| WebSocket news streaming (press releases, breaking news) | Not available | Enterprise only |
| WebSocket price quotes (trades/bid-ask) | 50 symbols max | Unlimited symbols |
| REST company news polling | 60 req/min | Higher limits |

### Implications for this project

- A WebSocket news feed from Finnhub requires an **Enterprise subscription**.
- The free tier WebSocket only supports **real-time trade/price ticks**, not news articles.
- For free/low-cost news, practical options are:
  1. **RSS polling** (current default) — free, no rate limits, ~1–5 min latency.
  2. **Finnhub REST polling** — 60 req/min; with 5 watchlist tickers that allows one
     poll per ticker every ~5s if the full quota is used.
  3. **Benzinga** — has a real streaming WebSocket API for news, but is a paid provider.
- **Quick win**: tighten `NEWS_POLL_INTERVAL_SEC` to 10–15s for RSS/Finnhub REST
  without any code changes or provider upgrades.
