"""Dedup store — remembers which listings have already been emailed.

Backed by a small JSON file (data/seen.json) rather than SQLite so it can be
committed to the repo and reliably carried between GitHub Actions runs (a cache is
best-effort and was dropping state, causing duplicate digests). The file is tiny
(one entry per sent listing) and diff-friendly.

Keyed by Listing.dedup_key() = "source:listing_id" (or a URL hash fallback).
Newly-sent listings are recorded ONLY after a successful send, so a failed send
doesn't cause us to silently skip them next time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing

logger = logging.getLogger("housing_agent")


class SeenStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "seen.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("seen.json unreadable (%s) — starting fresh", exc)
                self._data = {}

    def filter_new(self, listings: list[Listing]) -> list[Listing]:
        """Return only listings we have not recorded before."""
        new = [lg for lg in listings if lg.dedup_key() not in self._data]
        logger.info("Dedup: %d/%d listings are new", len(new), len(listings))
        return new

    def mark_sent(self, listings: list[Listing]) -> None:
        """Record listings as seen and persist. Call AFTER a successful send."""
        now = datetime.now(timezone.utc).isoformat()
        for lg in listings:
            self._data[lg.dedup_key()] = {
                "source": lg.source,
                "url": lg.url,
                "title": lg.title,
                "warm_price": lg.warm_price_eur,
                "first_seen": now,
            }
        # Sorted keys for stable, review-friendly diffs when committed to git.
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        logger.info("Recorded %d listings as sent (%d total in store)",
                    len(listings), len(self._data))

    def count(self) -> int:
        return len(self._data)

    def close(self) -> None:
        pass
