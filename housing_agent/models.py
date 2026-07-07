"""The normalized Listing model shared by every scraper and the rest of the pipeline.

Every scraper MUST return objects of this shape so downstream code (filter,
commute, dedup, email) is source-agnostic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Literal, Optional

PriceType = Literal["warm", "kalt", "unknown"]


@dataclass
class Listing:
    # ── identity ─────────────────────────────────────────────────────────────
    source: str                      # e.g. "wunderflats"
    url: str                         # direct link to the listing
    listing_id: str                  # stable per-source id (falls back to url hash)

    # ── core details ─────────────────────────────────────────────────────────
    title: str
    price_eur: Optional[float]       # monthly price in EUR (see price_type)
    price_type: PriceType            # "warm" (incl. utilities) | "kalt" | "unknown"
    rooms: Optional[float]           # German Zimmer count (1 = studio, 2 = 1BR+living)
    furnished: Optional[bool]
    address_or_area: str             # human-readable location
    lat: Optional[float] = None
    lng: Optional[float] = None
    posted_date: Optional[str] = None   # ISO date if the source exposes it
    area_sqm: Optional[float] = None

    # ── enrichment (filled in later by the pipeline) ──────────────────────────
    price_is_estimated: bool = False    # True when warm was derived from kalt + NK
    warm_price_eur: Optional[float] = None   # normalized warm price used for filtering
    commute_minutes: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        """Stable key for the dedup store: source + id, or a URL hash fallback."""
        if self.listing_id:
            return f"{self.source}:{self.listing_id}"
        digest = hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:16]
        return f"{self.source}:url:{digest}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
