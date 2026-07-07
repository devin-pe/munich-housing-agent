"""Public-transport commute time to the office anchor, with caching and filtering.

Uses the Google **Routes API** (v2 computeRoutes), TRANSIT mode, with the arrival
time set to the next weekday at the configured local time. Results are cached so
repeat listings cost no API calls.

  NOTE: the older Directions API is a *legacy* API Google no longer enables for new
  projects (REQUEST_DENIED / LegacyApiNotActivatedMapError). Enable "Routes API".

Resilience: a per-listing lookup that fails leaves commute_minutes=None and the
listing is KEPT but flagged (we never silently drop a listing because routing
hiccuped). Listings whose computed time exceeds the limit are dropped.

The TravelTimeProvider seam is kept so a new provider (or a test stub) can be
dropped in without touching the filter.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .cache import JsonCache
from .config import Config
from .models import Listing

logger = logging.getLogger("housing_agent")

BERLIN = ZoneInfo("Europe/Berlin")


def next_weekday_arrival(arrival_local: str, base: datetime | None = None) -> datetime:
    """Return the next weekday (Mon–Fri) at the given HH:MM in Europe/Berlin.

    `base` is injectable for testing; defaults to now. If today is a weekday and
    the target time is still ahead, today is used; otherwise the next weekday.
    """
    hh, mm = (int(x) for x in arrival_local.split(":"))
    now = base or datetime.now(BERLIN)
    candidate = datetime.combine(now.date(), dtime(hh, mm), tzinfo=BERLIN)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # Sat=5, Sun=6
        candidate += timedelta(days=1)
    return candidate


class TravelTimeProvider(ABC):
    name = "base"

    def __init__(self, config: Config):
        self.config = config
        self.timeout = config.runtime.request_timeout_seconds

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def travel_minutes(
        self, origin: tuple[float, float], dest: tuple[float, float], arrival: datetime
    ) -> int | None:
        """Minutes by public transit from origin to dest arriving by `arrival`,
        or None if it can't be determined."""
        raise NotImplementedError


class GoogleTransitProvider(TravelTimeProvider):
    """Google Routes API (v2 computeRoutes), TRANSIT mode. Enable "Routes API"
    in your Google Cloud project. Uses a field mask so we're billed only for
    the route duration."""
    name = "google"
    ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"

    @property
    def available(self) -> bool:
        return bool(self.config.secrets.google_maps_api_key)

    def travel_minutes(self, origin, dest, arrival) -> int | None:
        try:
            # Routes API wants an RFC3339 UTC timestamp; it must be in the future.
            arrival_utc = arrival.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = httpx.post(
                self.ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.config.secrets.google_maps_api_key,
                    "X-Goog-FieldMask": "routes.duration",
                },
                json={
                    "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
                    "destination": {"location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}},
                    "travelMode": "TRANSIT",
                    "arrivalTime": arrival_utc,
                },
                timeout=self.timeout,
            )
            if r.status_code != 200:
                logger.warning("Google Routes API HTTP %s: %s", r.status_code, r.text[:200])
                return None
            routes = r.json().get("routes") or []
            if not routes:
                return None  # no transit route found
            dur = str(routes[0].get("duration", "")).rstrip("s")  # comes back like "1234s"
            return round(int(dur) / 60) if dur.isdigit() else None
        except Exception as exc:
            logger.warning("Google Routes API failed: %s", exc)
            return None


class CommuteFilter:
    def __init__(self, config: Config, geocoder=None):
        self.config = config
        self.geocoder = geocoder
        self.provider: TravelTimeProvider = GoogleTransitProvider(config)
        self.cache = JsonCache(
            Path(config.runtime.data_dir) / "commute_cache.json",
            config.commute.cache_ttl_days,
        )
        self.anchor = self._resolve_anchor()
        self.arrival = next_weekday_arrival(config.search.arrival_time_local)

    def _resolve_anchor(self) -> tuple[float, float] | None:
        s = self.config.search
        if s.anchor_lat is not None and s.anchor_lng is not None:
            return (s.anchor_lat, s.anchor_lng)
        if self.geocoder:
            return self.geocoder.geocode(s.anchor_address)
        return None

    def _cache_key(self, origin: tuple[float, float]) -> str:
        a = self.anchor
        return (f"{self.provider.name}:{origin[0]:.5f},{origin[1]:.5f}"
                f"->{a[0]:.5f},{a[1]:.5f}@{self.arrival.date()}")

    def _minutes_for(self, listing: Listing) -> int | None:
        # Get coordinates: prefer the listing's own, else geocode its address.
        if listing.lat is None or listing.lng is None:
            if self.geocoder:
                coords = self.geocoder.geocode(listing.address_or_area)
                if coords:
                    listing.lat, listing.lng = coords
            if listing.lat is None or listing.lng is None:
                return None
        origin = (listing.lat, listing.lng)
        key = self._cache_key(origin)
        cached = self.cache.get(key)
        if cached is not None:
            return cached if cached >= 0 else None  # -1 sentinel = known-unroutable
        minutes = self.provider.travel_minutes(origin, self.anchor, self.arrival)
        self.cache.set(key, minutes if minutes is not None else -1)
        return minutes

    def annotate_and_filter(self, listings: list[Listing]) -> tuple[list[Listing], dict]:
        """Set commute_minutes on each listing and drop those over the limit.

        `kept` includes listings whose commute is unknown (commute_minutes=None) so
        routing hiccups never hide a listing.
        """
        limit = self.config.search.max_commute_minutes
        stats = {"total": len(listings), "kept": 0, "dropped_over_limit": 0, "unknown": 0}

        if self.anchor is None:
            logger.error("No anchor coordinates — skipping commute filter (keeping all).")
            stats["unknown"] = stats["kept"] = len(listings)
            return listings, stats

        if not self.provider.available:
            logger.error(
                "Google Maps API key missing — keeping all listings with commute "
                "unknown. Set GOOGLE_MAPS_API_KEY in .env (enable Routes API).")
            stats["unknown"] = stats["kept"] = len(listings)
            return listings, stats

        kept: list[Listing] = []
        for lg in listings:
            minutes = self._minutes_for(lg)
            lg.commute_minutes = minutes
            if minutes is None:
                stats["unknown"] += 1
                kept.append(lg)  # keep but flagged
            elif minutes <= limit:
                kept.append(lg)
            else:
                stats["dropped_over_limit"] += 1
        self.cache.save()
        stats["kept"] = len(kept)
        logger.info("Commute filter: %s", stats)
        return kept, stats
