"""BaseScraper: shared HTTP session, realistic headers, and polite throttling.

Design goals:
  * One scraper failing must never crash the run — the pipeline wraps `.scrape()`
    in a try/except and records failures for the digest footer. Scrapers should
    still avoid raising for routine "no results" cases.
  * Requests look like a real browser (headers) and are throttled with jitter so
    we stay well-behaved and reduce the chance of being blocked.
"""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod

import httpx

from ..config import Config, SourceConfig
from ..models import Listing

logger = logging.getLogger("housing_agent")

# A current, realistic desktop Chrome UA. Update if sites start rejecting it.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    # No "br": httpx decodes gzip/deflate out of the box but not Brotli (that needs
    # an extra package). Advertising only gzip/deflate guarantees decodable HTML.
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class BaseScraper(ABC):
    """Base class for all source scrapers.

    Subclasses set `name` and implement `scrape()`, returning normalized Listings.
    """

    name: str = "base"

    def __init__(self, config: Config, source_cfg: SourceConfig):
        self.config = config
        self.source_cfg = source_cfg
        self._last_request_ts: float = 0.0
        self._client = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=config.runtime.request_timeout_seconds,
            follow_redirects=True,
        )

    # ── polite HTTP helpers ──────────────────────────────────────────────────
    def _throttle(self) -> None:
        """Sleep so consecutive requests to this source respect the configured
        delay, plus a small random jitter to look less robotic."""
        delay = self.config.runtime.request_delay_seconds
        elapsed = time.monotonic() - self._last_request_ts
        wait = delay - elapsed + random.uniform(0, delay * 0.4)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response:
        """Throttled GET. Raises for HTTP errors so the caller can handle/skip."""
        self._throttle()
        logger.debug("GET %s", url)
        resp = self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── interface ────────────────────────────────────────────────────────────
    @abstractmethod
    def scrape(self) -> list[Listing]:
        """Return a list of normalized Listing objects (may be empty).

        Should apply only *source-native* filtering (e.g. city, obvious price
        caps to limit pages). Cross-source filtering (warm price, rooms,
        commute) is done centrally in filters.py / commute.py.
        """
        raise NotImplementedError
