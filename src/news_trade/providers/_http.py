"""Shared HTTP utility: GET with exponential-backoff retry on rate-limit responses."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_RETRYABLE = frozenset({429, 503, 504})
_MAX_RETRIES = 3
_BASE_DELAY_S = 1.0
_MAX_DELAY_S = 60.0


async def http_get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response:
    """HTTP GET with exponential backoff retry on 429 / 503 / 504.

    Retry schedule (default, max_retries=3):
      attempt 1 failure → sleep 1 s
      attempt 2 failure → sleep 2 s
      attempt 3 failure → sleep 4 s
      attempt 4 (final) → raise ``httpx.HTTPStatusError``

    Respects the ``Retry-After`` response header (integer seconds) when present.
    Non-retryable errors (4xx except 429, 5xx except 503/504) are raised immediately
    without consuming retry budget.
    """
    delay = _BASE_DELAY_S
    for attempt in range(max_retries + 1):
        resp = await client.get(url, params=params)
        if resp.status_code not in _RETRYABLE:
            resp.raise_for_status()
            return resp
        if attempt == max_retries:
            resp.raise_for_status()  # raises HTTPStatusError
        retry_after = resp.headers.get("Retry-After", "").strip()
        wait = float(retry_after) if retry_after.isdigit() else delay
        wait = min(wait, _MAX_DELAY_S)
        host = url.split("/")[2]  # hostname only — API keys stay out of logs
        _logger.warning(
            "HTTP %d — rate limited by %s (attempt %d/%d), retrying in %.1f s",
            resp.status_code,
            host,
            attempt + 1,
            max_retries,
            wait,
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2.0, _MAX_DELAY_S)
    raise RuntimeError("http_get_with_retry: unreachable")  # pragma: no cover
