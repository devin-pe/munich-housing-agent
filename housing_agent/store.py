"""SQLite dedup store.

Keyed by `source:listing_id` (or a URL hash fallback — see Listing.dedup_key()).
Newly-sent listings are recorded ONLY after a successful email send, so a failed
send doesn't cause us to silently skip those listings next time.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Listing

logger = logging.getLogger("housing_agent")


class SeenStore:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "seen.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                dedup_key   TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                url         TEXT,
                title       TEXT,
                warm_price  REAL,
                first_seen  TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def filter_new(self, listings: list[Listing]) -> list[Listing]:
        """Return only listings we have not recorded before."""
        new: list[Listing] = []
        cur = self.conn.cursor()
        for lg in listings:
            row = cur.execute(
                "SELECT 1 FROM seen_listings WHERE dedup_key = ?", (lg.dedup_key(),)
            ).fetchone()
            if row is None:
                new.append(lg)
        logger.info("Dedup: %d/%d listings are new", len(new), len(listings))
        return new

    def mark_sent(self, listings: list[Listing]) -> None:
        """Record listings as seen. Call AFTER a successful email send."""
        now = datetime.now(timezone.utc).isoformat()
        self.conn.executemany(
            """INSERT OR IGNORE INTO seen_listings
               (dedup_key, source, url, title, warm_price, first_seen)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(lg.dedup_key(), lg.source, lg.url, lg.title, lg.warm_price_eur, now)
             for lg in listings],
        )
        self.conn.commit()
        logger.info("Recorded %d listings as sent", len(listings))

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]

    def close(self) -> None:
        self.conn.close()
