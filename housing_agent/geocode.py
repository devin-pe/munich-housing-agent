"""Geocoding — address string -> (lat, lng), cached.

Only needed for sources that don't already provide coordinates (Wunderflats does,
so this mostly fires for HousingAnywhere). Uses the Google Geocoding API; enable
"Geocoding API" in your Google Cloud project. Results are cached to disk.
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .cache import JsonCache
from .config import Config

logger = logging.getLogger("housing_agent")


class Geocoder:
    def __init__(self, config: Config):
        self.config = config
        self.cache = JsonCache(
            Path(config.runtime.data_dir) / "geocode_cache.json",
            config.commute.cache_ttl_days,
        )
        self.google_key = config.secrets.google_maps_api_key

    def geocode(self, address: str) -> tuple[float, float] | None:
        if not address:
            return None
        cached = self.cache.get(address)
        if cached:
            return tuple(cached)  # type: ignore[return-value]
        if not self.google_key:
            logger.warning("No Google Maps API key — cannot geocode %r", address)
            return None
        coords = self._geocode_google(address)
        if coords:
            self.cache.set(address, list(coords))
        return coords

    def _geocode_google(self, address: str) -> tuple[float, float] | None:
        try:
            r = httpx.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "region": self.config.commute.region,
                        "key": self.google_key},
                timeout=self.config.runtime.request_timeout_seconds,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                return (loc["lat"], loc["lng"])
            logger.warning("Google geocode for %r returned status=%s", address, data.get("status"))
        except Exception as exc:
            logger.warning("Google geocode failed for %r: %s", address, exc)
        return None

    def save(self) -> None:
        self.cache.save()
