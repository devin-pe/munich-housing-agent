"""Tiny JSON-file cache with per-entry TTL.

Used to memoise geocoding and transit-route lookups across daily runs so we make
as few (paid) API calls as possible. Not thread-safe — the agent is single-process.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("housing_agent")


class JsonCache:
    def __init__(self, path: str | Path, ttl_days: int):
        self.path = Path(path)
        self.ttl_seconds = max(0, ttl_days) * 86400
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Cache %s unreadable (%s) — starting fresh", self.path, exc)
                self._data = {}

    def get(self, key: str):
        entry = self._data.get(key)
        if not entry:
            return None
        if self.ttl_seconds and (time.time() - entry.get("ts", 0)) > self.ttl_seconds:
            return None  # expired
        return entry.get("value")

    def set(self, key: str, value) -> None:
        self._data[key] = {"value": value, "ts": time.time()}
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        self._dirty = False
